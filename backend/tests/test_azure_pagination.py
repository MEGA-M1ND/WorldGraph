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


def token_of(request) -> str | None:
    """Read the continuation token from a request, whichever shape it is.

    `_request_factory` builds a real `QueryRequest` when the optional Azure SDK is
    installed and a plain dict when it is not, and the token sits in a different place in
    each: `request.options.skip_token` versus `request["skip_token"]`.

    This used to be `request.get("skip_token") if isinstance(request, dict) else None`,
    which silently reported **no token at all** for the real SDK shape. CI runs without the
    optional SDK, so it only ever exercised the dict — and with the SDK installed, the one
    test that proves the loop threads its token at all passed while asserting nothing.
    """
    if isinstance(request, dict):
        return request.get("skip_token")
    options = getattr(request, "options", None)
    return getattr(options, "skip_token", None)


class FakeClient:
    """A Resource Graph client that hands back prepared pages, recording the tokens it saw."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.tokens_seen: list[str | None] = []
        self.requests_seen: list[object] = []
        self.calls = 0

    def resources(self, request):
        self.calls += 1
        self.requests_seen.append(request)
        self.tokens_seen.append(token_of(request))
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


class TestTheRequestShapeItself:
    """Which request shape the loop built, and that the token is readable from it.

    These exist because the token assertion above was shape-blind. `FakeClient` reported
    `None` for anything that was not a dict, so with the optional SDK installed the paging
    tests kept passing while checking nothing about the token — and CI, which runs without
    the SDK, never met that case at all.
    """

    @staticmethod
    def _sdk_installed() -> bool:
        try:
            import azure.mgmt.resourcegraph.models  # noqa: F401
        except ImportError:
            return False
        return True

    def test_the_token_is_readable_from_whichever_shape_was_built(self):
        """The property that matters, and it holds either way."""
        from app.adapters.azure_inventory import _request_factory

        request = _request_factory()("sub", skip_token="tok-42")
        assert token_of(request) == "tok-42"

    def test_a_request_with_no_token_reads_back_as_none(self):
        from app.adapters.azure_inventory import _request_factory

        assert token_of(_request_factory()("sub", skip_token=None)) is None

    def test_the_shape_matches_whether_the_sdk_is_present(self):
        """Names which path this environment actually exercised, rather than assuming."""
        from app.adapters.azure_inventory import PAGE_SIZE, _request_factory

        request = _request_factory()("sub", skip_token="tok")
        if self._sdk_installed():
            assert type(request).__name__ == "QueryRequest"
            assert request.options.top == PAGE_SIZE
            assert request.options.skip_token == "tok"
        else:
            assert isinstance(request, dict), "the fallback must be a plain dict"
            assert request["skip_token"] == "tok"
            assert request["top"] == PAGE_SIZE

    def test_the_loop_threads_its_token_through_the_real_shape(self):
        """The original assertion, now proved against whatever shape was built."""
        pages = [
            FakePage([resource(0)], skip_token="token-one"),
            FakePage([resource(1)], skip_token=None),
        ]
        _result, client = collect(pages)
        assert client.tokens_seen == [None, "token-one"]
        # And the recorded requests are the shape this environment actually builds.
        assert all(token_of(r) == t for r, t in zip(client.requests_seen, client.tokens_seen))


class TestEachTruncationReasonIsItsOwnReason:
    """Four different ways a read stops short, and they are not interchangeable.

    Every reason string survived mutation, so all four could have collapsed onto one — and
    the reason is what an operator acts on. "Azure said it truncated" and "WorldGraph
    stopped at its own ceiling" call for opposite responses: one is a query to narrow, the
    other is a limit to raise.
    """

    def test_azure_reporting_truncation_says_azure_said_so(self):
        result, _client = collect(
            [FakePage([resource(0)], skip_token=None, result_truncated="true")]
        )
        assert result.truncation_reason == "Azure reported the result set as truncated."

    def test_a_boolean_truncation_flag_is_honoured_too(self):
        """The real SDK returns an enum, most fakes a string, some a bool. All three."""
        for flag in ("true", "True", True):
            result, _client = collect(
                [FakePage([resource(0)], skip_token=None, result_truncated=flag)]
            )
            assert result.complete is False, flag

    def test_an_unset_or_false_flag_is_not_truncation(self):
        for flag in (None, False, "false"):
            result, _client = collect(
                [FakePage([resource(0)], skip_token=None, result_truncated=flag)]
            )
            assert result.complete is True, flag
            assert result.truncation_reason == ""

    def test_the_worldgraph_ceiling_names_worldgraph(self):
        pages = [
            FakePage([resource(i) for i in range(PAGE_SIZE)], skip_token=f"t{n}")
            for n in range(MAX_RESOURCES // PAGE_SIZE + 2)
        ]
        result, _client = collect(pages)
        assert result.truncation_reason == (
            f"Collection stopped at WorldGraph's ceiling of {MAX_RESOURCES:,} "
            "resources; this subscription holds more."
        )

    def test_the_page_budget_is_a_distinct_reason_from_the_row_ceiling(self):
        """A source that pages forever with small pages exhausts the loop, not the ceiling."""
        result, client = collect([FakePage([resource(0)], skip_token="always")])
        assert result.complete is False
        assert result.retrieved < MAX_RESOURCES
        assert result.truncation_reason == "Collection stopped after the maximum number of pages."
        assert client.calls == MAX_RESOURCES // PAGE_SIZE + 1

    def test_the_count_mismatch_reason_quotes_both_figures(self):
        result, _client = collect(
            [FakePage([resource(i) for i in range(1000)], skip_token=None, total_records=5000)]
        )
        assert result.truncation_reason == (
            "Azure reports 5,000 resources; 1,000 were retrieved."
        )

    def test_an_earlier_reason_is_not_overwritten_by_the_count_check(self):
        """`reason = reason or …`. The first cause is the one that explains the stop."""
        result, _client = collect(
            [
                FakePage(
                    [resource(0)], skip_token=None, total_records=5000, result_truncated="true"
                )
            ]
        )
        assert result.complete is False
        assert result.truncation_reason == "Azure reported the result set as truncated."

    def test_a_complete_read_carries_no_reason_at_all(self):
        result, _client = collect(
            [FakePage([resource(i) for i in range(5)], skip_token=None, total_records=5)]
        )
        assert result.complete is True
        assert result.truncation_reason == ""


class TestASdkFailureLeaksNothing:
    def test_only_the_exception_type_escapes(self):
        from app.adapters.base import AdapterError

        class Exploding:
            def resources(self, request):
                raise RuntimeError(
                    "GET https://management.azure.com/subscriptions/"
                    "00000000-1111-2222-3333-444444444444/resources "
                    "failed: Authorization Bearer eyJ0eXAiOiJKV1Qi"
                )

        settings = Settings(run_mode=RunMode.OFFLINE, database_path=":memory:")
        with pytest.raises(AdapterError) as raised:
            asyncio.run(
                fetch_resources(settings, live_workspace(), client_factory=Exploding)
            )
        message = str(raised.value)
        assert "RuntimeError" in message
        for leak in ("management.azure.com", "00000000-1111", "Bearer", "eyJ0eXAiOiJKV1Qi"):
            assert leak not in message
        # And it tells the operator what to actually do about it.
        assert "az login" in message


class TestTheCoverageLevelThresholds:
    """`level()` turns a ratio into a word an operator reads as a verdict.

    0.9, 0.4 and "greater than zero" all survived. A drifted threshold reports HIGH over
    an estate that is half undeclared, which is precisely the misreading §8 exists to stop.
    """

    @staticmethod
    def _level(count: int, total: int) -> str:
        """Driven through `assess_coverage`, not through a copy of the threshold table."""
        from app.adapters.azure_inventory import normalize_resource

        workspace = live_workspace()
        resources = []
        for index in range(total):
            row = resource(index)
            if index < count:
                row["tags"] = {"worldgraph.criticality": "CRITICAL"}
            resources.append(row)
        entities = [normalize_resource(r, workspace)[0] for r in resources]
        entities = [e for e in entities if e is not None]
        rows = assess_coverage(entities, [], {})
        return next(r for r in rows if r.dimension == "criticality").level

    @pytest.mark.parametrize(
        ("count", "total", "expected"),
        [
            (10, 10, "HIGH"),      # 1.00
            (9, 10, "HIGH"),       # 0.90 — the boundary is inclusive
            (89, 100, "PARTIAL"),  # 0.89 — one below it
            (4, 10, "PARTIAL"),    # 0.40 — inclusive here too
            (39, 100, "LOW"),      # 0.39
            (1, 100, "LOW"),       # anything above nothing is LOW, never NONE
            (0, 10, "NONE"),       # nothing declared is NONE, never LOW
        ],
    )
    def test_the_bands_sit_where_they_claim_to(self, count, total, expected):
        assert self._level(count, total) == expected

    def test_an_empty_estate_reports_none_rather_than_dividing_by_zero(self):
        rows = assess_coverage([], [], {})
        assert {r.level for r in rows} == {"NONE"}


class TestTheCollectionRowWordsItsOwnUncertainty:
    @staticmethod
    def _collection_row(collection):
        rows = assess_coverage([], [], {}, collection=collection)
        return next(r for r in rows if r.dimension == "collection")

    def test_a_complete_read_states_both_figures(self):
        row = self._collection_row(
            Collection(resources=[resource(0)], reported_total=1, complete=True)
        )
        assert row.level == "HIGH"
        assert row.detail == "1 of 1 resources retrieved"
        assert row.remedy == ""

    def test_a_complete_read_with_no_reported_total_omits_the_denominator(self):
        """It must not invent one, and must not print "1 of None"."""
        row = self._collection_row(
            Collection(resources=[resource(0)], reported_total=None, complete=True)
        )
        assert row.detail == "1 resources retrieved"
        assert "None" not in row.detail

    def test_an_incomplete_read_with_an_unknown_total_says_unknown(self):
        row = self._collection_row(
            Collection(resources=[resource(0)], reported_total=None, complete=False)
        )
        assert row.level == "LOW"
        assert row.detail == "INCOMPLETE — 1 of an unknown number of resources retrieved"
        # With no reason from the source, the remedy still warns off every figure below.
        assert row.remedy == (
            "Collection stopped short. Every figure below describes only what was "
            "retrieved, not the estate."
        )

    def test_a_source_supplied_reason_replaces_the_generic_remedy(self):
        row = self._collection_row(
            Collection(
                resources=[resource(0)],
                reported_total=5000,
                complete=False,
                truncation_reason="Azure reported the result set as truncated.",
            )
        )
        assert row.remedy == "Azure reported the result set as truncated."
        assert row.detail == "INCOMPLETE — 1 of 5000 resources retrieved"
