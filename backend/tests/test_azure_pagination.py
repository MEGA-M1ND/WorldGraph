"""Collection must be complete, or must say that it is not.

`fetch_resources` took the first page of a Resource Graph response and returned it as the
estate. Resource Graph caps a single response at 1000 rows regardless of what a KQL
``limit`` asks for, so the previous ``| limit 5000`` in the query did nothing except make
it *look* bounded: a 5000-resource subscription imported 1000 resources, and the graph
coverage report — the feature whose entire job is stating completeness honestly —
announced HIGH confidence over them.

A completeness feature that cannot detect its own incompleteness is worse than no
completeness feature, because it converts a gap into a reassurance.

**These tests do not verify the live connector.** They drive the paging loop with a fake
client. What Azure actually returns for `skip_token`, `total_records` and `result_truncated`
against a real subscription remains unverified — see `docs/REALITY_PASS_REPORT.md` §2.
"""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.azure_inventory import (
    MAX_RESOURCES,
    PAGE_SIZE,
    RESOURCE_GRAPH_QUERY,
    Collection,
    assess_coverage,
    fetch_resources,
)
from app.config import RunMode, Settings
from app.models.core import DataMode
from app.models.workspace import InventorySourceKind, Workspace, WorkspaceKind

SUBSCRIPTION = "/subscriptions/00000000-0000-0000-0000-000000000001"


def resource(index: int) -> dict:
    return {
        "id": f"{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.Web/sites/app-{index}",
        "name": f"app-{index}",
        "type": "microsoft.web/sites",
        "location": "westeurope",
        "resourceGroup": "rg",
        "tags": {},
        "properties": {"state": "Running"},
    }


class FakePage:
    """One Resource Graph response."""

    def __init__(self, data, *, skip_token=None, total_records=None, result_truncated=None):
        self.data = data
        self.skip_token = skip_token
        self.total_records = total_records
        self.result_truncated = result_truncated


class FakeClient:
    """A Resource Graph client that hands back prepared pages, recording the tokens it saw."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.tokens_seen: list[str | None] = []
        self.calls = 0

    def resources(self, request):
        self.calls += 1
        token = request.get("skip_token") if isinstance(request, dict) else None
        self.tokens_seen.append(token)
        return self._pages[min(self.calls - 1, len(self._pages) - 1)]


def live_workspace() -> Workspace:
    return Workspace(
        id="azure-live",
        name="Azure — Live",
        kind=WorkspaceKind.REAL,
        source=InventorySourceKind.AZURE,
        mode=DataMode.LIVE,
        organization="00000000-0000-0000-0000-000000000001",
    )


def collect(pages) -> tuple[Collection, FakeClient]:
    client = FakeClient(pages)
    settings = Settings(run_mode=RunMode.OFFLINE, database_path=":memory:")
    result = asyncio.run(
        fetch_resources(settings, live_workspace(), client_factory=lambda: client)
    )
    return result, client


# ======================================================================================
# Paging
# ======================================================================================


class TestPaging:
    def test_a_single_page_is_complete(self):
        result, client = collect([FakePage([resource(i) for i in range(5)], total_records=5)])
        assert result.retrieved == 5
        assert result.complete is True
        assert result.truncation_reason == ""
        assert client.calls == 1

    def test_every_page_is_followed(self):
        """The defect: only the first page was ever read."""
        pages = [
            FakePage([resource(i) for i in range(1000)], skip_token="t1", total_records=2500),
            FakePage([resource(i) for i in range(1000, 2000)], skip_token="t2", total_records=2500),
            FakePage([resource(i) for i in range(2000, 2500)], skip_token=None, total_records=2500),
        ]
        result, client = collect(pages)

        assert result.retrieved == 2500
        assert result.complete is True
        assert client.calls == 3

    def test_the_continuation_token_is_actually_sent_back(self):
        pages = [
            FakePage([resource(0)], skip_token="token-one"),
            FakePage([resource(1)], skip_token=None),
        ]
        _result, client = collect(pages)
        # A loop that fetched twice without threading the token would just re-read page 1.
        assert client.tokens_seen == [None, "token-one"]

    def test_an_empty_page_ends_collection(self):
        pages = [FakePage([resource(0)], skip_token="t1"), FakePage([], skip_token="t2")]
        result, client = collect(pages)
        assert result.retrieved == 1
        assert client.calls == 2


# ======================================================================================
# Honesty about what was missed
# ======================================================================================


class TestIncompleteCollectionIsReported:
    def test_fewer_rows_than_azure_reports_is_incomplete(self):
        """The strongest signal: Azure said 5000 exist and we hold 1000."""
        result, _client = collect(
            [FakePage([resource(i) for i in range(1000)], skip_token=None, total_records=5000)]
        )
        assert result.retrieved == 1000
        assert result.reported_total == 5000
        assert result.complete is False
        assert "5,000" in result.truncation_reason
        assert "1,000" in result.truncation_reason

    def test_an_explicit_truncation_flag_is_honoured(self):
        result, _client = collect(
            [FakePage([resource(0)], skip_token=None, result_truncated="true")]
        )
        assert result.complete is False
        assert "truncated" in result.truncation_reason.lower()

    def test_the_ceiling_is_reported_rather_than_applied_silently(self):
        pages = [
            FakePage([resource(i) for i in range(PAGE_SIZE)], skip_token=f"t{n}")
            for n in range(MAX_RESOURCES // PAGE_SIZE + 2)
        ]
        result, _client = collect(pages)
        assert result.complete is False
        assert result.retrieved >= MAX_RESOURCES
        assert result.truncation_reason

    def test_a_source_that_never_stops_does_not_hang(self):
        """A skip_token that is always present must not spin forever."""
        forever = [FakePage([resource(0)], skip_token="always")]
        result, client = collect(forever)
        assert result.complete is False
        assert client.calls <= MAX_RESOURCES // PAGE_SIZE + 1

    def test_the_query_no_longer_pretends_to_bound_itself(self):
        """`| limit 5000` did nothing; the ceiling belongs where it can be reported."""
        assert "limit" not in RESOURCE_GRAPH_QUERY.lower()


class TestSnapshotsAreCompleteByConstruction:
    def test_a_snapshot_is_the_whole_estate(self, tmp_path):
        import json

        snapshot = tmp_path / "s.json"
        snapshot.write_text(json.dumps({"resources": [resource(0), resource(1)]}))
        settings = Settings(
            run_mode=RunMode.OFFLINE,
            database_path=":memory:",
            azure_snapshot_path=str(snapshot),
        )
        workspace = Workspace(
            id="azure-snapshot",
            name="Azure — Snapshot",
            kind=WorkspaceKind.REAL,
            source=InventorySourceKind.SNAPSHOT,
            mode=DataMode.REPLAY,
            organization="snap",
        )
        result = asyncio.run(fetch_resources(settings, workspace))
        assert result.complete is True
        assert result.retrieved == 2


# ======================================================================================
# Coverage must not claim more than was collected
# ======================================================================================


class TestCoverageRespectsCollection:
    @staticmethod
    def _coverage(collection):
        from app.adapters.azure_inventory import normalize_resource

        workspace = live_workspace()
        entities = [normalize_resource(r, workspace)[0] for r in collection.resources]
        entities = [e for e in entities if e is not None]
        return assess_coverage(entities, [], {}, collection=collection)

    def test_incomplete_collection_leads_the_report(self):
        collection = Collection(
            resources=[resource(0)],
            reported_total=5000,
            complete=False,
            truncation_reason="Azure reports 5,000 resources; 1 was retrieved.",
        )
        rows = self._coverage(collection)
        assert rows[0].dimension == "collection"
        assert rows[0].level == "LOW"
        assert "INCOMPLETE" in rows[0].detail
        assert rows[0].remedy

    def test_complete_collection_says_so(self):
        collection = Collection(resources=[resource(0)], reported_total=1, complete=True)
        rows = self._coverage(collection)
        assert rows[0].dimension == "collection"
        assert rows[0].level == "HIGH"

    def test_a_partial_estate_can_never_report_high_completeness_overall(self):
        """The specific misreading: 'HIGH — 1 of 1 placed in a region' over 1 of 5000."""
        collection = Collection(
            resources=[resource(0)], reported_total=5000, complete=False
        )
        rows = self._coverage(collection)
        collection_row = next(r for r in rows if r.dimension == "collection")
        assert collection_row.level != "HIGH"

    def test_coverage_still_works_without_a_collection(self):
        """Callers that have no collection context must not break."""
        rows = assess_coverage([], [], {})
        assert rows
        assert all(r.dimension != "collection" for r in rows)


@pytest.mark.parametrize("size", [1, PAGE_SIZE - 1, PAGE_SIZE])
def test_a_final_short_page_ends_collection_cleanly(size):
    pages = [
        FakePage([resource(i) for i in range(PAGE_SIZE)], skip_token="t1", total_records=PAGE_SIZE + size),
        FakePage([resource(i) for i in range(size)], skip_token=None, total_records=PAGE_SIZE + size),
    ]
    result, _client = collect(pages)
    assert result.retrieved == PAGE_SIZE + size
    assert result.complete is True
