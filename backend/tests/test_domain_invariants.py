"""The domain model's own guards, and the sentences a workspace shows an operator.

The last uncovered lines in `app/models/`. Small, but each is a rule the rest of the
system trusts without re-checking: an id that reaches a URL, a severity band at its
boundary, a freshness figure that must be `None` rather than zero, and the disclaimer
every workspace shows about what its data is.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.core import (
    RISK_BANDS,
    BusinessProfile,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    ExposureProfile,
    FeedState,
    FeedStatus,
    Severity,
    WorldEntity,
    severity_from_score,
    utcnow,
)
from app.models.workspace import (
    ImportSummary,
    InventorySourceKind,
    Workspace,
    WorkspaceKind,
    WorkspaceStatus,
)

SRC = DataSourceInfo(source_id="probe", source_name="Probe", mode=DataMode.REPLAY)


def _entity(entity_id: str, **kwargs) -> WorldEntity:
    defaults = {
        "type": EntityType.APPLICATION,
        "name": entity_id,
        "source": SRC,
        "business": BusinessProfile(region="westeurope"),
        "exposure": ExposureProfile(internet_facing=False, network_zone="z"),
    }
    defaults.update(kwargs)
    return WorldEntity(id=entity_id, **defaults)


class TestEntityIdsAreSlugs:
    """Ids reach URLs and share links. Anything else is an adapter bug, caught here."""

    @pytest.mark.parametrize(
        "entity_id",
        ["payments-api", "az.rg.sites.store", "a_b", "ns:name", "abc123"],
    )
    def test_a_legal_id_is_accepted(self, entity_id: str):
        assert _entity(entity_id).id == entity_id

    @pytest.mark.parametrize(
        "entity_id",
        [
            "has space",
            "has/slash",
            "has?query=1",
            "has#fragment",
            "has%20encoded",
            "has\\backslash",
            "../traversal",
        ],
    )
    def test_an_id_that_would_not_survive_a_url_is_rejected(self, entity_id: str):
        with pytest.raises(ValidationError) as error:
            _entity(entity_id)
        assert "alphanumerics" in str(error.value)


class TestSeverityBands:
    def test_the_bands_cover_the_whole_range_without_a_gap(self):
        for score in range(0, 101):
            assert severity_from_score(float(score)) in set(Severity)

    def test_a_score_at_the_ceiling_is_critical(self):
        """The `return CRITICAL` fallthrough: 100 is not inside a half-open band."""
        assert severity_from_score(100.0) is Severity.CRITICAL

    def test_scores_outside_the_range_are_clamped_rather_than_rejected(self):
        assert severity_from_score(1000.0) is Severity.CRITICAL
        assert severity_from_score(-50.0) is severity_from_score(0.0)

    def test_the_bands_are_ordered_and_contiguous(self):
        """Contiguity is what makes the `return CRITICAL` fallthrough unreachable.

        That line stays uncovered on purpose: it is the guard for a band table with a
        hole in it, and this test is the reason there is no hole. Contriving a call that
        reaches it would mean breaking the table the guard exists to protect.
        """
        from itertools import pairwise

        lows = [low for low, _high, _band in RISK_BANDS]
        assert lows == sorted(lows)
        for (_low, high, _band), (next_low, _nh, _nb) in pairwise(RISK_BANDS):
            assert high == next_low, "a gap between bands would have no severity at all"
        # And the last band's ceiling sits above the clamp, so 100.0 lands inside it.
        assert RISK_BANDS[-1][1] > 100.0


class TestEdgeSemantics:
    @pytest.mark.parametrize(
        ("edge_type", "propagates"),
        [
            (DependencyType.DEPENDS_ON, True),
            (DependencyType.HOSTED_IN, True),
            (DependencyType.CONNECTS_TO, True),
            (DependencyType.SUPPLIED_BY, True),
            # `SERVES` points at customers and `REPLICATES_TO` at a standby. Losing
            # either does not degrade the source, and treating them as if it did would
            # make every estate look like a single point of failure.
            (DependencyType.SERVES, False),
            (DependencyType.REPLICATES_TO, False),
        ],
    )
    def test_only_some_edges_carry_failure_backwards(self, edge_type, propagates):
        edge = DependencyEdge(
            id=f"a--{edge_type.value}--b",
            source_entity_id="a",
            target_entity_id="b",
            type=edge_type,
        )
        assert edge.propagates_failure is propagates


class TestFeedFreshness:
    def test_a_feed_that_never_succeeded_has_no_age(self):
        """`None`, not `0.0` — zero seconds would read as a feed that just refreshed."""
        status = FeedStatus(
            adapter_id="a",
            adapter_name="A",
            state=FeedState.LOADING,
            mode=DataMode.LIVE,
            record_count=0,
        )
        assert status.freshness_seconds() is None

    def test_an_age_is_seconds_since_the_last_success(self):
        from datetime import timedelta

        now = utcnow()
        status = FeedStatus(
            adapter_id="a",
            adapter_name="A",
            state=FeedState.LIVE,
            mode=DataMode.LIVE,
            record_count=1,
            last_success_at=now - timedelta(seconds=90),
        )
        assert status.freshness_seconds(now=now) == pytest.approx(90.0)

    def test_a_clock_that_went_backwards_floors_at_zero_rather_than_going_negative(self):
        from datetime import timedelta

        now = utcnow()
        status = FeedStatus(
            adapter_id="a",
            adapter_name="A",
            state=FeedState.LIVE,
            mode=DataMode.LIVE,
            record_count=1,
            last_success_at=now + timedelta(seconds=5),
        )
        assert status.freshness_seconds(now=now) == 0.0


class TestWhatAWorkspaceTellsAnOperator:
    @staticmethod
    def _workspace(kind: WorkspaceKind, source: InventorySourceKind, mode: DataMode) -> Workspace:
        return Workspace(
            id="ws",
            name="Workspace",
            kind=kind,
            source=source,
            status=WorkspaceStatus.NOT_LOADED,
            mode=mode,
            organization="org",
        )

    def test_a_demo_workspace_says_it_is_a_demo(self):
        workspace = self._workspace(
            WorkspaceKind.DEMO, InventorySourceKind.FIXTURE, DataMode.SYNTHETIC
        )
        assert workspace.is_real is False
        disclaimer = workspace.data_disclaimer()
        assert disclaimer
        assert "read-only inventory" not in disclaimer

    def test_a_snapshot_says_it_is_not_a_live_view(self):
        """Calling a recording live is the dishonesty the provenance model prevents."""
        workspace = self._workspace(
            WorkspaceKind.REAL, InventorySourceKind.SNAPSHOT, DataMode.REPLAY
        )
        assert workspace.is_real is True
        assert workspace.data_disclaimer() == (
            "This workspace is a saved inventory snapshot, not a live view. "
            "WorldGraph reads inventory only and has changed nothing."
        )

    def test_a_live_subscription_says_it_cannot_write(self):
        """§4, in the sentence the operator actually reads."""
        workspace = self._workspace(
            WorkspaceKind.REAL, InventorySourceKind.AZURE, DataMode.LIVE
        )
        assert workspace.data_disclaimer() == (
            "This workspace is imported read-only inventory. WorldGraph has changed "
            "nothing and cannot: it holds no write permission and executes no action."
        )


class TestImportSummaryArithmetic:
    @staticmethod
    def _summary(**kwargs) -> ImportSummary:
        base = {
            "workspace_id": "ws",
            "source": InventorySourceKind.AZURE,
            "subscription_label": "sub",
            "resources_discovered": 0,
            "resources_supported": 0,
            "resources_unsupported": 0,
            "entities_created": 0,
            "explicit_edges": 0,
            "declared_edges": 0,
            "regions": 0,
            "mode": DataMode.LIVE,
        }
        base.update(kwargs)
        return ImportSummary(**base)

    def test_an_empty_import_reports_zero_rather_than_dividing_by_zero(self):
        assert self._summary().support_rate == 0.0

    def test_the_support_rate_is_supported_over_discovered(self):
        summary = self._summary(resources_discovered=200, resources_supported=50)
        assert summary.support_rate == 0.25

    def test_a_fully_supported_import_reports_one(self):
        summary = self._summary(resources_discovered=10, resources_supported=10)
        assert summary.support_rate == 1.0
