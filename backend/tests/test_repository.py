"""What survives a restart.

Coverage measurement found whole methods of `SqliteRepository` that **no test ever
calls** — `load_events`, `recent_analyses`, `list_scenarios`, `delete_scenario`,
`save_plan`, `load_plan`, `load_timeline`, and the transaction rollback. Only the estate
methods were exercised, through the re-import tests.

That is a stronger finding than a surviving mutant: not "no test can tell the difference",
but "no test runs this line". The repository is what makes a share link work after a
restart and what holds the audit trail the timeline claims to be; if it silently loses or
corrupts a record, nothing in the suite notices.

Every test here writes through one connection and reads back through **a second
repository over the same file**, because an in-memory round-trip can pass on a cache that
was never persisted at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.analysis import (
    BlastRadiusResult,
    BusinessImpact,
    Confidence,
    OverrideKind,
    ResponseAction,
    ResponsePlan,
    RiskScore,
    SimulationOverride,
    SimulationScenario,
    TimelineEntry,
    Urgency,
)
from app.models.core import (
    BusinessProfile,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    EventCategory,
    ExposureProfile,
    GeoPoint,
    HealthState,
    Severity,
    WorldEntity,
    WorldEvent,
    utcnow,
)
from app.storage.repository import SqliteRepository, dumps

SRC = DataSourceInfo(source_id="probe", source_name="Probe", mode=DataMode.REPLAY)


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "worldgraph.db")


def _reopen(path: str) -> SqliteRepository:
    """A second repository over the same file — the only honest durability check."""
    return SqliteRepository(path)


def entity(entity_id: str, **kwargs) -> WorldEntity:
    defaults = {
        "type": EntityType.APPLICATION,
        "name": entity_id,
        "source": SRC,
        "business": BusinessProfile(region="westeurope"),
        "exposure": ExposureProfile(internet_facing=False, network_zone="z"),
    }
    defaults.update(kwargs)
    return WorldEntity(id=entity_id, **defaults)


def event(event_id: str, *, severity=Severity.HIGH, hours_ago: float = 0.0) -> WorldEvent:
    return WorldEvent(
        id=event_id,
        category=EventCategory.EARTHQUAKE,
        title=f"Event {event_id}",
        severity=severity,
        source=SRC,
        location=GeoPoint(lat=1.3, lon=103.8),
        exposure_radius_km=50.0,
        occurred_at=utcnow() - timedelta(hours=hours_ago),
    )


def analysis(analysis_id: str, *, hours_ago: float = 0.0) -> BlastRadiusResult:
    return BlastRadiusResult(
        id=analysis_id,
        origin_kind="EVENT",
        origin_ids=["a"],
        origin_label="probe",
        severity=Severity.HIGH,
        risk=RiskScore(score=42.0, severity=Severity.HIGH, contributions=[]),
        business_impact=BusinessImpact(
            infrastructure_availability=1.0,
            critical_services_impacted=0,
            customer_regions_impacted=0,
        ),
        confidence=Confidence(score=0.7),
        computed_at=utcnow() - timedelta(hours=hours_ago),
    )


# ======================================================================================
# The estate
# ======================================================================================


class TestTheEstateSurvivesAReopen:
    def test_entities_and_edges_round_trip_through_the_file(self, store_path):
        repo = SqliteRepository(store_path)
        entities = [entity("a"), entity("b", type=EntityType.DATABASE)]
        edges = [
            DependencyEdge(
                id="a--DEPENDS_ON--b",
                source_entity_id="a",
                target_entity_id="b",
                type=DependencyType.DEPENDS_ON,
            )
        ]
        repo.save_entities(entities)
        repo.save_edges(edges)
        repo.close()

        reopened = _reopen(store_path)
        try:
            loaded = {e.id: e for e in reopened.load_entities()}
            assert set(loaded) == {"a", "b"}
            assert loaded["b"].type is EntityType.DATABASE
            assert [e.id for e in reopened.load_edges()] == ["a--DEPENDS_ON--b"]
        finally:
            reopened.close()

    def test_saving_the_same_id_twice_updates_rather_than_duplicates(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_entities([entity("a", name="before")])
        repo.save_entities([entity("a", name="after")])
        loaded = repo.load_entities()
        assert len(loaded) == 1
        assert loaded[0].name == "after"
        repo.close()

    def test_a_nullable_field_survives_as_null_not_as_a_default(self, store_path):
        """The tri-state that the whole Reality Pass turns on."""
        repo = SqliteRepository(store_path)
        repo.save_entities(
            [entity("a", business=BusinessProfile(region="westeurope"), location=None)]
        )
        repo.close()

        reopened = _reopen(store_path)
        try:
            loaded = reopened.load_entities()[0]
            assert loaded.location is None
            assert loaded.business.traffic_share is None
            assert loaded.business.revenue_per_hour is None
            assert loaded.customer_facing is None
        finally:
            reopened.close()


# ======================================================================================
# Events
# ======================================================================================


class TestEventPersistence:
    def test_events_come_back_newest_first(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_events([event("old", hours_ago=5), event("new", hours_ago=0)])
        repo.close()

        reopened = _reopen(store_path)
        try:
            assert [e.id for e in reopened.load_events()] == ["new", "old"]
        finally:
            reopened.close()

    def test_the_limit_takes_the_newest(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_events([event(f"e{i}", hours_ago=i) for i in range(5)])
        assert [e.id for e in repo.load_events(limit=2)] == ["e0", "e1"]
        repo.close()

    def test_since_filters_by_occurrence_not_by_insertion(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_events([event("old", hours_ago=5), event("new", hours_ago=0)])
        cutoff = utcnow() - timedelta(hours=2)
        assert [e.id for e in repo.load_events(since=cutoff)] == ["new"]
        repo.close()

    def test_an_event_with_no_location_persists_as_one(self, store_path):
        repo = SqliteRepository(store_path)
        quiet = event("quiet")
        quiet.location = None
        quiet.exposure_radius_km = 0.0
        repo.save_events([quiet])
        repo.close()

        reopened = _reopen(store_path)
        try:
            assert reopened.load_events()[0].location is None
        finally:
            reopened.close()


# ======================================================================================
# Analyses, scenarios, plans
# ======================================================================================


class TestAnalysisPersistence:
    def test_an_analysis_round_trips_with_its_score(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_analysis(analysis("blast-1"), event_id="evt-1")
        repo.close()

        reopened = _reopen(store_path)
        try:
            loaded = reopened.load_analysis("blast-1")
            assert loaded is not None
            assert loaded.risk.score == 42.0
            assert loaded.severity is Severity.HIGH
        finally:
            reopened.close()

    def test_an_unknown_analysis_is_none_rather_than_an_error(self, store_path):
        repo = SqliteRepository(store_path)
        assert repo.load_analysis("blast-nope") is None
        repo.close()

    def test_recent_analyses_are_newest_first_and_bounded(self, store_path):
        repo = SqliteRepository(store_path)
        for index in range(25):
            repo.save_analysis(analysis(f"blast-{index:02d}", hours_ago=index))
        assert len(repo.recent_analyses()) == 20
        assert [r.id for r in repo.recent_analyses(limit=3)] == [
            "blast-00", "blast-01", "blast-02"
        ]
        repo.close()


class TestScenarioPersistence:
    @staticmethod
    def _scenario(scenario_id: str, *, minutes_ago: float = 0.0) -> SimulationScenario:
        return SimulationScenario(
            id=scenario_id,
            name=f"Scenario {scenario_id}",
            overrides=[
                SimulationOverride(
                    id="ov-1",
                    kind=OverrideKind.ENTITY_HEALTH,
                    target_id="a",
                    health=HealthState.DOWN,
                )
            ],
            updated_at=utcnow() - timedelta(minutes=minutes_ago),
        )

    def test_a_scenario_round_trips_with_its_overrides(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_scenario(self._scenario("scn-1"))
        repo.close()

        reopened = _reopen(store_path)
        try:
            loaded = reopened.load_scenario("scn-1")
            assert loaded is not None
            assert len(loaded.overrides) == 1
            assert loaded.overrides[0].health is HealthState.DOWN
            # A stored scenario is still SIMULATED, never promoted to LIVE.
            assert loaded.mode is DataMode.SIMULATED
        finally:
            reopened.close()

    def test_scenarios_are_listed_newest_first(self, store_path):
        repo = SqliteRepository(store_path)
        for index in range(3):
            repo.save_scenario(self._scenario(f"scn-{index}", minutes_ago=index))
        assert [s.id for s in repo.list_scenarios()] == ["scn-0", "scn-1", "scn-2"]
        repo.close()

    def test_deleting_a_scenario_removes_it_and_leaves_the_rest(self, store_path):
        repo = SqliteRepository(store_path)
        repo.save_scenario(self._scenario("scn-1"))
        repo.save_scenario(self._scenario("scn-2"))
        repo.delete_scenario("scn-1")
        assert repo.load_scenario("scn-1") is None
        assert [s.id for s in repo.list_scenarios()] == ["scn-2"]
        repo.close()

    def test_deleting_an_absent_scenario_is_not_an_error(self, store_path):
        repo = SqliteRepository(store_path)
        repo.delete_scenario("scn-never-existed")
        assert repo.list_scenarios() == []
        repo.close()


class TestPlanPersistence:
    @staticmethod
    def _plan() -> ResponsePlan:
        return ResponsePlan(
            id="plan-1",
            summary="probe",
            actions=[
                ResponseAction(
                    action="Fail over",
                    rationale="because",
                    urgency=Urgency.NOW,
                    confidence=0.8,
                    requires_approval=True,
                )
            ],
        )

    def test_a_plan_round_trips_with_its_approval_flag(self, store_path):
        """`requires_approval` is the field that keeps a recommendation a recommendation."""
        repo = SqliteRepository(store_path)
        repo.save_plan(self._plan(), analysis_id="blast-1")
        repo.close()

        reopened = _reopen(store_path)
        try:
            loaded = reopened.load_plan("plan-1")
            assert loaded is not None
            assert loaded.actions[0].requires_approval is True
            assert loaded.actions[0].urgency is Urgency.NOW
            assert loaded.actions[0].rationale == "because"
            # `executed` is Literal[False] and must survive a round trip as False.
            assert loaded.actions[0].executed is False
        finally:
            reopened.close()

    def test_an_unknown_plan_is_none(self, store_path):
        repo = SqliteRepository(store_path)
        assert repo.load_plan("plan-nope") is None
        repo.close()


class TestTimelinePersistence:
    @staticmethod
    def _entry(entry_id: str, *, stage: str = "analyze", event_id: str | None = None,
               minutes_ago: float = 0.0) -> TimelineEntry:
        return TimelineEntry(
            id=entry_id,
            at=utcnow() - timedelta(minutes=minutes_ago),
            stage=stage,
            message=f"message {entry_id}",
            severity=Severity.INFO,
            event_id=event_id,
        )

    def test_the_durable_timeline_outlives_the_in_memory_one(self, store_path):
        """`WorldState` keeps 500 entries; the repository is the record that persists."""
        repo = SqliteRepository(store_path)
        repo.append_timeline([self._entry(f"tl-{i:03d}", minutes_ago=i) for i in range(10)])
        repo.close()

        reopened = _reopen(store_path)
        try:
            rows = reopened.load_timeline()
            assert len(rows) == 10
            assert [r.id for r in rows] == [f"tl-{i:03d}" for i in range(10)]
        finally:
            reopened.close()

    def test_filtering_by_event_returns_only_that_events_entries(self, store_path):
        repo = SqliteRepository(store_path)
        repo.append_timeline(
            [
                self._entry("tl-a", event_id="evt-1"),
                self._entry("tl-b", event_id="evt-2"),
                self._entry("tl-c", event_id=None),
            ]
        )
        assert [r.id for r in repo.load_timeline(event_id="evt-1")] == ["tl-a"]
        assert len(repo.load_timeline()) == 3
        repo.close()

    def test_the_limit_takes_the_newest(self, store_path):
        repo = SqliteRepository(store_path)
        repo.append_timeline([self._entry(f"tl-{i:03d}", minutes_ago=i) for i in range(40)])
        rows = repo.load_timeline(limit=5)
        assert [r.id for r in rows] == [f"tl-{i:03d}" for i in range(5)]
        repo.close()

    def test_appending_nothing_is_not_an_error(self, store_path):
        repo = SqliteRepository(store_path)
        repo.append_timeline([])
        assert repo.load_timeline() == []
        repo.close()


# ======================================================================================
# The transaction itself
# ======================================================================================


class TestWritesAreAtomic:
    def test_a_failed_write_leaves_nothing_behind(self, store_path):
        """The rollback path had never been executed by any test.

        A half-applied estate is worse than a failed import: the graph would answer
        questions from a partial world without knowing it was partial.
        """
        repo = SqliteRepository(store_path)
        repo.save_entities([entity("a")])

        with pytest.raises(RuntimeError), repo._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO entities "
                "(id, type, name, criticality, health, lat, lon, mode, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("b", "APPLICATION", "b", "UNKNOWN", "UNKNOWN", None, None,
                 "REPLAY", utcnow().isoformat(), "{}"),
            )
            raise RuntimeError("something went wrong mid-transaction")

        # The row written inside the failed block is gone; the earlier one is not.
        assert [e.id for e in repo.load_entities()] == ["a"]
        repo.close()

    def test_a_reopened_store_is_not_reset_by_the_schema_script(self, store_path):
        """`CREATE TABLE IF NOT EXISTS` — a plain CREATE would wipe the file on startup."""
        repo = SqliteRepository(store_path)
        repo.save_entities([entity("a")])
        repo.close()

        for _ in range(3):
            reopened = _reopen(store_path)
            assert [e.id for e in reopened.load_entities()] == ["a"]
            reopened.close()


class TestStableSerialization:
    """`dumps` exists so storage owns its own serialization. Stable means byte-identical."""

    @staticmethod
    def _fixed(entity_id: str):
        stamp = utcnow()
        return entity(entity_id, updated_at=stamp), stamp

    def test_two_equal_models_serialise_byte_for_byte_identically(self):
        """Otherwise every stored diff is noise and no two runs can be compared."""
        first, stamp = self._fixed("a")
        second = entity("a", updated_at=stamp)
        assert dumps(first) == dumps(second)

    def test_the_output_is_compact(self):
        payload = dumps(self._fixed("a")[0])
        assert ", " not in payload
        assert '": ' not in payload

    def test_the_keys_are_sorted_so_field_order_cannot_leak_in(self):
        import json

        payload = dumps(self._fixed("a")[0])
        keys = list(json.loads(payload))
        assert keys == sorted(keys)
        assert "id" in keys and "business" in keys
