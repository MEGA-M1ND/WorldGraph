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


# ======================================================================================
# The read API everything else is built on
# ======================================================================================


class TestEventQueryFilters:
    """`events()` backs the incident list, the dashboard and three AI tools.

    Its `since`, `category` and `limit` filters all survived mutation, as did the
    newest-first sort. A filter that silently does nothing returns *more* than asked for,
    which reads as a working answer.
    """

    @staticmethod
    def _seed(world, count: int = 6) -> list[WorldEvent]:
        world._events.clear()
        made = []
        for index in range(count):
            event = _physical_event(location=None, radius_km=0.0)
            event.id = f"seed-{index}"
            event.occurred_at = utcnow() - timedelta(hours=index)
            if index % 2:
                event.category = EventCategory.SECURITY_VULNERABILITY
            world._events[event.id] = event
            made.append(event)
        return made

    @pytest.mark.anyio
    async def test_events_come_back_newest_first(self, world):
        self._seed(world)
        ids = [e.id for e in world.events()]
        assert ids == ["seed-0", "seed-1", "seed-2", "seed-3", "seed-4", "seed-5"]

    @pytest.mark.anyio
    async def test_the_limit_takes_the_newest_not_an_arbitrary_slice(self, world):
        self._seed(world)
        assert [e.id for e in world.events(limit=2)] == ["seed-0", "seed-1"]

    @pytest.mark.anyio
    async def test_since_excludes_older_events_at_the_boundary(self, world):
        made = self._seed(world)
        cutoff = made[2].occurred_at
        ids = [e.id for e in world.events(since=cutoff)]
        # Inclusive: an event exactly at the cutoff is inside the window.
        assert ids == ["seed-0", "seed-1", "seed-2"]

    @pytest.mark.anyio
    async def test_the_category_filter_is_case_insensitive_and_exact(self, world):
        self._seed(world)
        ids = [e.id for e in world.events(category="security_vulnerability")]
        assert ids == ["seed-1", "seed-3", "seed-5"]
        assert [e.id for e in world.events(category="SECURITY_VULNERABILITY")] == ids
        # An unknown category returns nothing rather than everything.
        assert world.events(category="NOT_A_CATEGORY") == []

    @pytest.mark.anyio
    async def test_the_filters_compose(self, world):
        made = self._seed(world)
        ids = [
            e.id
            for e in world.events(category="SECURITY_VULNERABILITY", since=made[3].occurred_at)
        ]
        assert ids == ["seed-1", "seed-3"]


class TestTimelineQueryFilters:
    @pytest.mark.anyio
    async def test_the_timeline_is_newest_first_and_limited(self, world):
        world._timeline.clear()
        for index in range(10):
            world._record_timeline(stage="test", message=f"entry {index}")
        rows = world.timeline(limit=3)
        assert [r.message for r in rows] == ["entry 9", "entry 8", "entry 7"]

    @pytest.mark.anyio
    async def test_filtering_by_event_keeps_only_that_events_entries(self, world):
        world._timeline.clear()
        world._record_timeline(stage="test", message="mine", event_id="evt-a")
        world._record_timeline(stage="test", message="theirs", event_id="evt-b")
        world._record_timeline(stage="test", message="unattached")
        assert [r.message for r in world.timeline(event_id="evt-a")] == ["mine"]
        # No filter means everything, including the unattached entry.
        assert len(world.timeline()) == 3

    @pytest.mark.anyio
    async def test_an_unknown_event_id_yields_nothing_rather_than_everything(self, world):
        assert world.timeline(event_id="no-such-event") == []


class TestWhatCountsAsAnActiveIncident:
    """`_correlates` decides whether an event is counted against this estate at all.

    Three independent routes to True — a directly named entity, a geographic hit, a
    software match — and every one of their guards survived. An always-True version would
    count somebody else's outage as our incident; an always-False one would empty the
    dashboard.
    """

    @pytest.mark.anyio
    async def test_a_named_entity_that_exists_here_correlates(self, world):
        world.graph = WorldGraph([_asset("region-a")], [])
        assert world._correlates(_physical_event(location=None, radius_km=0.0, named=["region-a"]))

    @pytest.mark.anyio
    async def test_a_named_entity_from_another_estate_does_not(self, world):
        """The whole point of workspace isolation, in one predicate."""
        world.graph = WorldGraph([_asset("region-a")], [])
        assert not world._correlates(
            _physical_event(location=None, radius_km=0.0, named=["someone-elses-region"])
        )

    @pytest.mark.anyio
    async def test_a_geographic_hit_correlates_and_a_miss_does_not(self, world):
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph(
            [_asset("dc-1", entity_type=EntityType.DATACENTER, location=here)], []
        )
        assert world._correlates(_physical_event(location=here, radius_km=50.0))
        assert not world._correlates(
            _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=50.0)
        )

    @pytest.mark.anyio
    async def test_an_event_with_a_location_but_no_radius_does_not_correlate_by_geography(
        self, world
    ):
        """A zero radius is not "everywhere" — it is "no declared exposure area"."""
        here = GeoPoint(lat=1.30, lon=103.85)
        world.graph = WorldGraph(
            [_asset("dc-1", entity_type=EntityType.DATACENTER, location=here)], []
        )
        assert not world._correlates(_physical_event(location=here, radius_km=0.0))

    @pytest.mark.anyio
    async def test_a_cve_correlates_only_when_the_inventory_names_it(self, world):
        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        world.graph = WorldGraph([_asset("web", software=[vulnerable])], [])
        assert world._correlates(_cve_event())

        world.graph = WorldGraph([_asset("web", software=[])], [])
        assert not world._correlates(_cve_event())

    @pytest.mark.anyio
    async def test_an_event_with_no_route_at_all_does_not_correlate(self, world):
        """No named entity, no location, no software: nothing ties it to this estate."""
        world.graph = WorldGraph([_asset("web")], [])
        bare = _physical_event(location=None, radius_km=0.0)
        assert bare.metadata == {}
        assert not world._correlates(bare)


class TestTheAvailabilityMoveIsQuotedInFull:
    """"availability 100.00% → 90.45%", against whichever figure the estate supports.

    Re-running the mutation harness after the first pass left the percentages and the
    fallback branch standing: the simulate line's prefix and suffix were pinned, the number
    in the middle was not. Reporting an infrastructure figure under a customer heading is
    the same fabrication in a different sentence, so both halves are pinned here.
    """

    @staticmethod
    def _metrics(availability, infrastructure):
        from app.models.analysis import WorldSnapshotMetrics

        return WorldSnapshotMetrics(
            availability=availability, infrastructure_availability=infrastructure
        )

    def test_a_customer_figure_is_quoted_when_the_estate_has_one(self):
        from app.services.world_state import _availability_move

        assert _availability_move(
            self._metrics(1.0, 1.0), self._metrics(0.9045, 0.8)
        ) == "availability 100.00% → 90.45%"

    def test_without_one_the_infrastructure_figure_is_relabelled_not_borrowed(self):
        from app.services.world_state import _availability_move

        assert _availability_move(
            self._metrics(None, 1.0), self._metrics(None, 0.9045)
        ) == (
            "infrastructure availability 100.00% → 90.45% "
            "(customer-experienced availability UNKNOWN for this workspace)"
        )

    def test_one_missing_side_is_enough_to_fall_back(self):
        """A baseline with customers and a simulation without is not a comparison."""
        from app.services.world_state import _availability_move

        assert "infrastructure availability" in _availability_move(
            self._metrics(1.0, 1.0), self._metrics(None, 0.9)
        )
        assert "infrastructure availability" in _availability_move(
            self._metrics(None, 1.0), self._metrics(0.9, 0.9)
        )

    def test_two_decimals_because_that_is_where_availability_lives(self):
        from app.services.world_state import _availability_move

        move = _availability_move(self._metrics(0.9999, 1.0), self._metrics(0.999, 1.0))
        assert move == "availability 99.99% → 99.90%"


class TestTheDashboardCountsWhatItSaysItCounts:
    @pytest.mark.anyio
    async def test_infrastructure_assets_exclude_the_things_that_are_not_infrastructure(
        self, world
    ):
        """An organization node and a customer region are not assets an operator runs."""
        counted = world.dashboard()["infrastructure_assets"]
        excluded = {"ORGANIZATION", "CUSTOMER_REGION", "SECURITY_FINDING", "WORLD_EVENT"}
        expected = sum(1 for e in world.entities() if e.type.value not in excluded)
        assert counted == expected
        assert counted < len(world.entities()), "the demo estate has an organization node"

    @pytest.mark.anyio
    async def test_critical_services_counts_workloads_not_sites(self, world):
        counted = world.dashboard()["critical_services"]
        wanted = {"BUSINESS_SERVICE", "APPLICATION", "MICROSERVICE", "DATABASE"}
        expected = sum(
            1
            for e in world.entities()
            if e.criticality.value == "CRITICAL" and e.type.value in wanted
        )
        assert counted == expected
        # A datacenter can be CRITICAL without being a service.
        assert counted < sum(1 for e in world.entities() if e.criticality.value == "CRITICAL")


class TestTheQueryDefaults:
    @pytest.mark.anyio
    async def test_the_timeline_default_limit_is_a_hundred(self, world):
        world._timeline.clear()
        for index in range(140):
            world._record_timeline(stage="test", message=f"entry {index}")
        assert len(world.timeline()) == 100
        assert len(world.timeline(limit=140)) == 140

    @pytest.mark.anyio
    async def test_material_risks_read_a_bounded_window_of_events(self, world):
        """`events(limit=50)`. Unbounded, one noisy feed would dominate the risk list."""
        import app.services.world_state as module

        seen: dict[str, int] = {}
        original = module.material_risks

        def spy(graph, events):
            seen["count"] = len(events)
            return original(graph, events)

        module.material_risks = spy
        try:
            for index in range(80):
                event = _cve_event(f"CVE-2024-{index:04d}")
                event.id = f"noise-{index}"
                world._events[event.id] = event
            world.material_risks()
        finally:
            module.material_risks = original
        assert seen["count"] == 50


class TestCorrelationByProductNameAlone:
    @pytest.mark.anyio
    async def test_an_event_with_product_names_and_no_cve_still_correlates(self, world):
        """A KEV entry can name a product before a CVE id is assigned to the estate."""
        world.graph = WorldGraph(
            [_asset("web", software=[SoftwareComponent(name="nginx", version="1.0")])], []
        )
        event = _cve_event()
        event.metadata = {"product_names": ["nginx"]}
        assert world._correlates(event)

        event.metadata = {"product_names": ["postgres"]}
        assert not world._correlates(event)

    @pytest.mark.anyio
    async def test_a_malformed_product_list_does_not_correlate_by_accident(self, world):
        world.graph = WorldGraph(
            [_asset("web", software=[SoftwareComponent(name="nginx", version="1.0")])], []
        )
        event = _cve_event()
        event.metadata = {"product_names": "nginx"}  # a string, not a list
        assert not world._correlates(event)


class TestChangesSinceOrdersEventsNewestFirst:
    @pytest.mark.anyio
    async def test_the_newest_event_leads(self, world):
        world._events.clear()
        for index in range(5):
            event = _cve_event(f"CVE-2024-{index:04d}")
            event.id = f"seq-{index}"
            event.occurred_at = utcnow() - timedelta(minutes=index)
            event.source.ingested_at = utcnow()
            world._events[event.id] = event
        events = world.changes_since(timedelta(hours=1))["new_events"]
        assert [e.id for e in events] == ["seq-0", "seq-1", "seq-2", "seq-3", "seq-4"]


class TestTheNoImpactExplanationNamesWhatItLookedAt:
    """A negative result has to say what kind of negative it is.

    The located and unlocated branches, and the "Inventory searched:" line beneath them,
    all survived the second pass. An event with no location that matched nothing is a
    different finding from a facility survey that came back clean, and the two sentences
    could have collapsed into one.
    """

    @staticmethod
    def _explanations(event, coverage=None):
        from app.services.world_state import _no_impact_explanations

        return _no_impact_explanations(event, None, coverage)

    def test_a_located_event_reports_the_radius_and_the_software_search(self):
        event = _physical_event(location=GeoPoint(lat=1.3, lon=103.8), radius_km=50.0)
        lines = self._explanations(event)
        assert lines[0] == "Magnitude 6.4 offshore does not correlate with any asset in this workspace."
        assert lines[1] == (
            "No facility lies inside the modelled exposure radius and no asset runs "
            "affected software."
        )

    def test_an_unlocated_event_says_it_had_no_location_to_search_by(self):
        event = _physical_event(location=None, radius_km=0.0)
        lines = self._explanations(event)
        assert lines[1] == "This event carries no location and matched no software inventory."

    def test_the_inventory_line_is_added_only_when_there_is_inventory_to_report(self):
        from app.models.analysis import InventoryCoverage

        event = _physical_event(location=None, radius_km=0.0)
        assert len(self._explanations(event)) == 2
        coverage = InventoryCoverage(assessable_entities=10, entities_with_inventory=7)
        with_coverage = self._explanations(event, coverage)
        assert len(with_coverage) == 3
        assert with_coverage[2] == f"Inventory searched: {coverage.describe()}."


class TestTheConfidenceInANegative:
    def test_a_genuine_negative_discounts_the_feed_it_came_from(self):
        """`confidence * 0.9`. A negative is never more certain than its source."""
        from app.services.world_state import _no_impact_confidence

        event = _physical_event(location=None, radius_km=0.0)
        event.source.confidence = 1.0
        assert _no_impact_confidence(event, None, None).score == 0.9
        event.source.confidence = 0.5
        assert _no_impact_confidence(event, None, None).score == 0.45

    def test_the_score_is_rounded_to_two_places(self):
        from app.services.world_state import _no_impact_confidence

        event = _physical_event(location=None, radius_km=0.0)
        event.source.confidence = 0.777
        assert _no_impact_confidence(event, None, None).score == 0.7


class TestGeneratedIdentifiersHaveAShape:
    """The `ovr-` lesson, applied to the two remaining id prefixes.

    `startswith("blast-")` also passes for `"blast-X…"`. Both of these reach URLs and the
    repository's primary keys, so the prefix and the length are the contract.
    """

    @pytest.mark.anyio
    async def test_an_analysis_id_is_a_prefixed_twelve_character_slug(self, world):
        import re

        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)
        assert re.fullmatch(r"blast-[0-9a-f]{12}", result.id), result.id

    @pytest.mark.anyio
    async def test_a_timeline_id_is_a_prefixed_twelve_character_slug(self, world):
        import re

        entry = world._record_timeline(stage="test", message="probe")
        assert re.fullmatch(r"tl-[0-9a-f]{12}", entry.id), entry.id

    @pytest.mark.anyio
    async def test_two_timeline_entries_never_share_an_id(self, world):
        ids = {world._record_timeline(stage="t", message=str(i)).id for i in range(50)}
        assert len(ids) == 50


class TestLookupsFallBackToTheRepository:
    """An analysis outlives the in-memory cache. Losing that makes a share link dead.

    `self._analyses.get(id) or self.repository.load_analysis(id)` — both halves survived,
    so the fallback could have been removed with nothing failing.
    """

    @pytest.mark.anyio
    async def test_an_analysis_evicted_from_memory_is_still_retrievable(self, world):
        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=GeoPoint(lat=-45.0, lon=170.0), radius_km=5.0)
        world._events[event.id] = event
        result = world.analyze_event(event.id)

        world._analyses.clear()
        recovered = world.analysis(result.id)
        assert recovered is not None
        assert recovered.id == result.id

    @pytest.mark.anyio
    async def test_an_unknown_analysis_id_is_none_not_an_invention(self, world):
        assert world.analysis("blast-does-not-exist") is None

    @pytest.mark.anyio
    async def test_a_scenario_evicted_from_memory_is_still_retrievable(self, world):
        from app.models.analysis import SimulationScenario

        scenario = SimulationScenario(id="scn-probe", name="Probe")
        world.put_scenario(scenario)
        world._scenarios.clear()
        recovered = world.scenario("scn-probe")
        assert recovered is not None
        assert recovered.name == "Probe"

    @pytest.mark.anyio
    async def test_scenarios_are_listed_newest_first(self, world):
        from app.models.analysis import SimulationScenario

        for index in range(3):
            world.put_scenario(
                SimulationScenario(
                    id=f"scn-{index}",
                    name=f"Probe {index}",
                    updated_at=utcnow() - timedelta(minutes=index),
                )
            )
        assert [s.id for s in world.scenarios()] == ["scn-0", "scn-1", "scn-2"]

    @pytest.mark.anyio
    async def test_a_dropped_scenario_is_gone_from_both_places(self, world):
        from app.models.analysis import SimulationScenario

        world.put_scenario(SimulationScenario(id="scn-probe", name="Probe"))
        world.drop_scenario("scn-probe")
        assert world.scenario("scn-probe") is None


class TestStartupAndEventLookup:
    @pytest.mark.anyio
    async def test_an_unknown_event_raises_rather_than_analysing_something_else(self, world):
        with pytest.raises(KeyError) as error:
            world.analyze_event("no-such-event")
        assert "unknown event 'no-such-event'" in str(error.value)

    @pytest.mark.anyio
    async def test_the_normalize_line_names_the_feed_the_event_came_from(self, world):
        world.graph = WorldGraph([_asset("elsewhere")], [])
        event = _physical_event(location=None, radius_km=0.0)
        world._events[event.id] = event
        world.analyze_event(event.id)
        assert _message(world, event.id, "normalize") == (
            "Magnitude 6.4 offshore normalized from Probe."
        )

    @pytest.mark.anyio
    async def test_startup_records_what_was_loaded(self, world):
        entry = next(e for e in world.timeline() if e.stage == "ingest")
        assert entry.message == (
            f"{world.workspace.name} online in {world.settings.run_mode.value} mode — "
            f"{len(world.graph)} entities, {len(world._events)} events."
        )


class TestStartupSurvivesABrokenFeed:
    """A feed that cannot start must not stop the application.

    The `try/except` around `adapter.start()` was uncovered. Without it — or with it
    swallowing the wrong thing — one unreachable upstream at boot takes the whole product
    down, which is the opposite of what a resilience tool should do about a partial
    outage.
    """

    @pytest.mark.anyio
    async def test_an_adapter_that_fails_to_start_is_logged_and_stepped_over(self, world, caplog):
        import logging

        from app.adapters.fixtures import ReplayEventAdapter

        class Broken(ReplayEventAdapter):
            id = "broken-feed"

            async def start(self):
                raise RuntimeError("upstream refused the connection")

        fresh = type(world)(world.settings, world.repository)
        fresh._adapters = [Broken()]
        fresh._register_adapters = lambda: None
        with caplog.at_level(logging.WARNING, logger="worldgraph.state"):
            await fresh.startup()
        try:
            assert "adapter_start_failed" in caplog.text
            assert "broken-feed" in caplog.text
            # The world still came up.
            assert len(fresh.graph) > 0
            assert any(e.stage == "ingest" for e in fresh.timeline())
        finally:
            await fresh.shutdown()

    @pytest.mark.anyio
    async def test_starting_twice_does_not_ingest_twice(self, world):
        """`if self._started: return`. A second startup would duplicate every feed record."""
        before_events = len(world._events)
        before_entities = len(world.graph)
        await world.startup()
        assert len(world._events) == before_events
        assert len(world.graph) == before_entities

    @pytest.mark.anyio
    async def test_live_mode_registers_the_live_feeds_and_demo_mode_does_not(self, world):
        from app.adapters.kev import CisaKevAdapter
        from app.adapters.usgs import UsgsEarthquakeAdapter
        from app.config import RunMode

        demo_ids = {type(a) for a in world._adapters}
        assert UsgsEarthquakeAdapter not in demo_ids
        assert CisaKevAdapter not in demo_ids

        live = type(world)(
            world.settings.model_copy(update={"run_mode": RunMode.LIVE}), world.repository
        )
        live._register_adapters()
        live_types = {type(a) for a in live._adapters}
        assert UsgsEarthquakeAdapter in live_types
        assert CisaKevAdapter in live_types

    @pytest.mark.anyio
    async def test_entities_an_adapter_carries_are_ingested_into_the_graph(self, world):
        """A feed may bring entities as well as events — a supplier, a facility."""
        from app.adapters.fixtures import ReplayEventAdapter

        class WithEntity(ReplayEventAdapter):
            id = "entity-feed"

            def get_entities(self):
                return [_asset("feed-brought-this")]

        world._ingest_from(WithEntity())
        assert world.graph.entity("feed-brought-this") is not None
