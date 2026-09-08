"""What the analysis pipeline records about its own work.

Mutation testing found `WorldState`'s bookkeeping almost entirely unprotected: the
timeline messages, the metadata attached to them, the 512-character truncation, the
in-memory timeline bound, the recency window in `changes_since`, and — most importantly —
the `proximity=1.0 if internet_facing else 0.0` decision that sets how hard a CVE hits.
Every one of those could be inverted, blanked or rounded away and 700+ tests stayed green.

These are not cosmetic. The timeline is the product's audit trail: it is what a responder
reads to see *why* an analysis reached its severity, and it is the only place WorldGraph
states that a generated plan was not executed. If it can drift silently, the audit trail
is decoration.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.graph.world_graph import WorldGraph
from app.models.analysis import VulnerabilityAssessment
from app.models.core import (
    BusinessProfile,
    DataMode,
    DataSourceInfo,
    EntityType,
    EventCategory,
    ExposureProfile,
    GeoPoint,
    Severity,
    SoftwareComponent,
    WorldEntity,
    WorldEvent,
    utcnow,
)

SRC = DataSourceInfo(source_id="probe", source_name="Probe", mode=DataMode.REPLAY)


def _asset(
    entity_id: str,
    *,
    software: list[SoftwareComponent] | None = None,
    internet_facing: bool = False,
    entity_type: EntityType = EntityType.APPLICATION,
    location: GeoPoint | None = None,
) -> WorldEntity:
    return WorldEntity(
        id=entity_id,
        type=entity_type,
        name=entity_id,
        source=SRC,
        location=location,
        business=BusinessProfile(region="westeurope"),
        exposure=ExposureProfile(internet_facing=internet_facing, network_zone="z"),
        software=software or [],
    )


def _cve_event(cve: str = "CVE-2024-0001", severity: Severity = Severity.CRITICAL) -> WorldEvent:
    return WorldEvent(
        id=f"evt-{cve}",
        category=EventCategory.SECURITY_VULNERABILITY,
        title=f"Critical RCE ({cve})",
        severity=severity,
        source=SRC,
        metadata={"cve_id": cve, "product_names": ["nginx"]},
        occurred_at=utcnow(),
    )


def _physical_event(
    *,
    location: GeoPoint | None,
    radius_km: float,
    named: list[str] | None = None,
    severity: Severity = Severity.HIGH,
) -> WorldEvent:
    return WorldEvent(
        id="evt-physical",
        category=EventCategory.EARTHQUAKE,
        title="Magnitude 6.4 offshore",
        severity=severity,
        source=SRC,
        location=location,
        exposure_radius_km=radius_km,
        directly_named_entity_ids=named or [],
        occurred_at=utcnow(),
    )


def _stages(world, event_id: str) -> list[str]:
    """Oldest first — `timeline()` hands back newest first, for the UI."""
    return [e.stage for e in reversed(world.timeline(event_id=event_id))]


def _message(world, event_id: str, stage: str) -> str:
    hits = [e for e in world.timeline(event_id=event_id) if e.stage == stage]
    assert hits, f"no timeline entry for stage {stage!r}"
    return hits[-1].message


# ======================================================================================
# Reachability drives the severity of a CVE, not just its wording
# ======================================================================================


class TestReachabilityDrivesProximity:
    """`proximity=1.0 if internet_facing else 0.0` is a risk decision, not a label.

    The same CVE on the same software must score harder when something can reach the
    vulnerable asset than when nothing can. Both constants survived mutation, meaning the
    entire reachability distinction was recorded in prose and nowhere in the numbers.
    """

    @staticmethod
    def _run(world, *, internet_facing: bool):
        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        world.graph = WorldGraph(
            [_asset("web", software=[vulnerable], internet_facing=internet_facing)], []
        )
        event = _cve_event()
        world._events[event.id] = event
        return world.analyze_event(event.id)

    @staticmethod
    def _proximity_points(result) -> float | None:
        """The `event_proximity` contribution is where the 1.0/0.0 lands in the score."""
        hits = [c for c in result.risk.contributions if c.code == "event_proximity"]
        return hits[0].points if hits else None

    @pytest.mark.anyio
    async def test_a_reachable_asset_is_analysed_at_full_proximity(self, world):
        result = self._run(world, internet_facing=True)
        assert result.assessment is VulnerabilityAssessment.CONFIRMED_AFFECTED
        # 15.0 is the whole `event_proximity` weight — i.e. proximity was exactly 1.0.
        assert self._proximity_points(result) == 15.0

    @pytest.mark.anyio
    async def test_an_unreachable_asset_scores_no_proximity_at_all(self, world):
        result = self._run(world, internet_facing=False)
        # Still confirmed — the asset genuinely runs the vulnerable version.
        assert result.assessment is VulnerabilityAssessment.CONFIRMED_AFFECTED
        # The contribution is absent, not zero: nothing can reach it, so proximity is 0.0
        # and `risk.py` omits the line rather than printing "0% of the exposure radius".
        assert self._proximity_points(result) is None

    @pytest.mark.anyio
    async def test_reachability_is_the_only_difference_and_it_moves_the_score(self, world):
        reachable = self._run(world, internet_facing=True)
        unreachable = self._run(world, internet_facing=False)
        # If these ever converge, the internet-facing branch has stopped mattering and
        # "reachable exposure" is a label over an identical computation.
        assert reachable.risk.score > unreachable.risk.score

    @pytest.mark.anyio
    async def test_the_origin_is_labelled_as_reachable_exposure(self, world):
        result = self._run(world, internet_facing=True)
        assert result.origin_label == "CVE-2024-0001 — reachable exposure"

    @pytest.mark.anyio
    async def test_an_unnamed_cve_falls_back_to_the_event_title(self, world):
        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        world.graph = WorldGraph([_asset("web", software=[vulnerable])], [])
        event = _cve_event()
        event.metadata["cve_id"] = ""
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        assert result.origin_label == "Critical RCE (CVE-2024-0001) — reachable exposure"


class TestTheConfirmedCountIsStatedExactly:
    """The correlate line is the responder's first read on a CVE: how many, how exposed."""

    @pytest.mark.anyio
    async def test_confirmed_and_internet_facing_are_counted_separately(self, world):
        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        world.graph = WorldGraph(
            [
                _asset("web", software=[vulnerable], internet_facing=True),
                _asset("api", software=[vulnerable], internet_facing=False),
            ],
            [],
        )
        event = _cve_event()
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert (
            _message(world, event.id, "correlate")
            == "CVE-2024-0001 confirmed on 2 assets (1 internet-facing)."
        )

    @pytest.mark.anyio
    async def test_name_only_matches_are_reported_as_unverified(self, world):
        """A product-name collision must be visible in the count, not folded into it."""
        world.graph = WorldGraph(
            [
                _asset(
                    "web",
                    software=[SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])],
                    internet_facing=True,
                ),
                _asset("other", software=[SoftwareComponent(name="nginx", version="9.9", cve_ids=[])]),
            ],
            [],
        )
        event = _cve_event()
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert (
            _message(world, event.id, "correlate")
            == "CVE-2024-0001 confirmed on 1 assets (1 internet-facing), 1 unverified by product name."
        )


# ======================================================================================
# Correlation branches: found nearby / named directly / nothing
# ======================================================================================


class TestTheCorrelationBranchesAreDistinguishable:
    """Three different findings must read as three different findings.

    "Nothing correlates" and "the source named these assets" are opposite conclusions.
    Every constant in these three messages survived mutation, so they could all have
    collapsed onto the same string.
    """

    @pytest.mark.anyio
    async def test_assets_inside_the_radius_are_counted_with_the_radius(self, world):
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph(
            [
                _asset("dc-1", entity_type=EntityType.DATACENTER, location=here),
                _asset("supplier-1", entity_type=EntityType.SUPPLIER, location=here),
            ],
            [],
        )
        event = _physical_event(location=here, radius_km=50.0)
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert (
            _message(world, event.id, "correlate")
            == "2 assets inside the 50 km exposure radius (1 facilities, 1 suppliers)."
        )

    @pytest.mark.anyio
    async def test_the_metadata_reports_the_nearest_asset_not_an_arbitrary_one(self, world):
        near = GeoPoint(lat=1.30, lon=103.85)
        far = GeoPoint(lat=1.60, lon=103.85)  # ~33 km north
        world.graph = WorldGraph(
            [
                _asset("far-dc", entity_type=EntityType.DATACENTER, location=far),
                _asset("near-dc", entity_type=EntityType.DATACENTER, location=near),
            ],
            [],
        )
        event = _physical_event(location=near, radius_km=100.0)
        world._events[event.id] = event
        world.analyze_event(event.id)
        entry = [e for e in world.timeline(event_id=event.id) if e.stage == "correlate"][-1]
        # 0.0, not "some small number": the closest asset is at the epicentre. A mutant
        # that reported the furthest match would read ~33.
        assert entry.metadata["closest_km"] == 0.0
        assert entry.metadata["critical_facilities"] == 2

    @pytest.mark.anyio
    async def test_a_source_naming_assets_directly_says_so(self, world):
        """No geography at all — a status page naming a region is still a correlation."""
        world.graph = WorldGraph([_asset("region-a"), _asset("region-b")], [])
        event = _physical_event(location=None, radius_km=0.0, named=["region-a", "region-b"])
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert _message(world, event.id, "correlate") == "2 assets named directly by the source."

    @pytest.mark.anyio
    async def test_no_correlation_is_stated_as_no_correlation(self, world):
        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert (
            _message(world, event.id, "correlate")
            == "No asset in this workspace correlates with this event."
        )

    @pytest.mark.anyio
    async def test_an_uncorrelated_event_yields_an_explicitly_empty_analysis(self, world):
        """`_empty_analysis` must be zero and LOW — not a fabricated origin."""
        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        assert result.origin_ids == []
        assert result.origin_label == "Magnitude 6.4 offshore"
        assert result.severity is Severity.LOW
        assert result.risk.score == 0.0
        assert result.risk.contributions == []


# ======================================================================================
# The audit trail every analysis leaves behind
# ======================================================================================


class TestEveryAnalysisLeavesTheSameAuditTrail:
    @pytest.mark.anyio
    async def test_the_pipeline_stages_are_recorded_in_order(self, world):
        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert _stages(world, event.id) == ["normalize", "correlate", "analyze", "risk"]

    @pytest.mark.anyio
    async def test_the_analyze_line_counts_direct_and_indirect_impact(self, world):
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph([_asset("dc-1", entity_type=EntityType.DATACENTER, location=here)], [])
        event = _physical_event(location=here, radius_km=50.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        message = _message(world, event.id, "analyze")
        assert message.startswith(
            f"Dependency analysis complete — {len(result.direct_impact)} direct, "
            f"{len(result.indirect_impact)} indirect, "
        )
        assert message.endswith(" ms.")
        # The counts must be the result's own, not zeros that happen to match.
        assert len(result.direct_impact) == 1

    @pytest.mark.anyio
    async def test_the_risk_line_quotes_the_score_and_severity_it_stored(self, world):
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph([_asset("dc-1", entity_type=EntityType.DATACENTER, location=here)], [])
        event = _physical_event(location=here, radius_km=50.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        assert (
            _message(world, event.id, "risk")
            == f"Risk scored {result.risk.score:.0f}/100 → {result.severity.value}."
        )
        entry = [e for e in world.timeline(event_id=event.id) if e.stage == "risk"][-1]
        assert entry.severity is result.severity
        assert entry.metadata["contributions"] == [
            c.model_dump() for c in result.risk.contributions
        ]

    @pytest.mark.anyio
    async def test_recent_analyses_are_newest_first_and_bounded(self, world):
        world.graph = WorldGraph([_asset("elsewhere")], [])
        for index in range(25):
            event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
            event.id = f"evt-{index}"
            world._events[event.id] = event
            world.analyze_event(event.id)
        assert len(world.recent_analyses()) == 20
        assert len(world.recent_analyses(limit=3)) == 3
        stamps = [r.computed_at for r in world.recent_analyses()]
        assert stamps == sorted(stamps, reverse=True)


class TestAPlanIsRecordedAsRecommendedNotExecuted:
    """WorldGraph must never log that it did something to infrastructure.

    Reality Pass §27: the AI must never tell the user "I failed over the cluster". The
    "none executed" clause is the timeline's half of that promise, and it survived
    mutation.
    """

    @pytest.mark.anyio
    async def test_the_plan_line_says_none_executed(self, world):
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph([_asset("dc-1", entity_type=EntityType.DATACENTER, location=here)], [])
        event = _physical_event(location=here, radius_km=50.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        plan = world.build_plan(result)
        entry = [e for e in world.timeline() if e.stage == "plan"][-1]
        assert entry.message == (
            f"Response plan generated — {len(plan.actions)} recommended actions, none executed."
        )
        assert entry.metadata == {"plan_id": plan.id, "analysis_id": result.id}
        assert entry.severity is result.severity


class TestSimulationIsRecordedAgainstItsScenario:
    @pytest.mark.anyio
    async def test_the_simulate_entry_carries_the_scenario_id_and_duration(self, world):
        from app.models.analysis import OverrideKind, SimulationOverride, SimulationScenario
        from app.models.core import HealthState

        target = world.entities()[0]
        scenario = SimulationScenario(
            id="scn-probe",
            name="Probe",
            overrides=[
                SimulationOverride(
                    id="ov-1",
                    kind=OverrideKind.ENTITY_HEALTH,
                    target_id=target.id,
                    health=HealthState.DOWN,
                )
            ],
        )
        comparison = world.compare_scenario(scenario)
        entry = [e for e in world.timeline() if e.stage == "simulate"][-1]
        assert entry.metadata["scenario_id"] == scenario.id
        assert entry.metadata["duration_ms"] == comparison.duration_ms
        assert entry.message.startswith(f"Simulation '{scenario.name}' — ")
        assert entry.message.endswith(f"risk {comparison.simulated.material_risk.value}.")
        assert entry.severity is comparison.simulated.material_risk


# ======================================================================================
# The timeline's own limits
# ======================================================================================


class TestTheTimelineHasBoundsAndKeepsThem:
    @pytest.mark.anyio
    async def test_a_long_message_is_truncated_to_512_characters(self, world):
        entry = world._record_timeline(stage="test", message="x" * 900)
        assert len(entry.message) == 512
        assert entry.message == "x" * 512

    @pytest.mark.anyio
    async def test_a_short_message_is_left_alone(self, world):
        entry = world._record_timeline(stage="test", message="short")
        assert entry.message == "short"

    @pytest.mark.anyio
    async def test_the_in_memory_timeline_stops_growing(self, world):
        from app.services.world_state import TIMELINE_MEMORY_LIMIT

        for index in range(TIMELINE_MEMORY_LIMIT + 40):
            world._record_timeline(stage="test", message=f"entry {index}")
        assert len(world._timeline) == TIMELINE_MEMORY_LIMIT
        # The *newest* are kept: dropping the tail instead of the head would freeze the
        # audit trail at whatever happened first.
        assert world._timeline[-1].message == f"entry {TIMELINE_MEMORY_LIMIT + 39}"

    @pytest.mark.anyio
    async def test_ai_tool_use_is_recorded_under_its_own_stage(self, world):
        """So a reader can tell a model's action apart from the deterministic pipeline."""
        world.record_ai_timeline("called blast_radius", event_id="evt-1")
        entry = world._timeline[-1]
        assert entry.stage == "ai"
        assert entry.message == "called blast_radius"
        assert entry.event_id == "evt-1"


class TestChangesSinceRespectsItsWindow:
    """"What changed in the last hour?" must mean the last hour."""

    @staticmethod
    def _age(world, minutes: int) -> None:
        for entry in world._timeline:
            entry.at = entry.at - timedelta(minutes=minutes)
        for event in world._events.values():
            event.source.ingested_at = event.source.ingested_at - timedelta(minutes=minutes)

    @pytest.mark.anyio
    async def test_the_window_is_reported_in_minutes(self, world):
        assert world.changes_since(timedelta(hours=2))["window_minutes"] == 120
        assert world.changes_since(timedelta(seconds=90))["window_minutes"] == 1

    @pytest.mark.anyio
    async def test_everything_older_than_the_window_is_excluded(self, world):
        # The replay events ship with fixed historical timestamps, so ingest one now.
        fresh = _cve_event("CVE-2024-9999")
        fresh.source.ingested_at = utcnow()
        world._events[fresh.id] = fresh
        world._record_timeline(stage="test", message="recent")
        assert world.changes_since(timedelta(hours=1))["timeline"]
        assert world.changes_since(timedelta(hours=1))["new_events"]

        self._age(world, minutes=120)
        aged = world.changes_since(timedelta(hours=1))
        assert aged["timeline"] == []
        assert aged["new_events"] == []
        # A wider window finds them again — they were filtered, not deleted.
        assert world.changes_since(timedelta(hours=4))["timeline"]

    @pytest.mark.anyio
    async def test_results_are_newest_first_and_capped(self, world):
        for index in range(40):
            world._record_timeline(stage="test", message=f"entry {index}")
        changes = world.changes_since(timedelta(hours=1))
        entries = changes["timeline"]
        assert len(entries) == 30
        assert [e.at for e in entries] == sorted((e.at for e in entries), reverse=True)
        assert len(changes["new_events"]) <= 20
