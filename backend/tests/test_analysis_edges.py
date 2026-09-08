"""The analysis core's edge cases, which coverage found nobody was running.

Every module here is above 93 %, so these are not gaps in the main path — they are the
branches that only fire on an estate the AtlasPay fixture is not. Most of them are the
honesty branches: what WorldGraph says when a region declares no customers, when a
traversal is truncated, when an event is replayed rather than live, when a graph is empty.

That is where an imported estate lives. The Reality Pass thesis is that WorldGraph must
say what it does not know, and the code that does the saying only runs on data the demo
does not contain.
"""

from __future__ import annotations

import pytest

from app.analysis.business_impact import (
    business_impact,
    rollup_region,
    worst_material_risk,
)
from app.analysis.correlation import (
    SpatialMatch,
    correlate_event,
    health_for_availability,
    security_pins,
)
from app.analysis.propagation import propagate
from app.graph.world_graph import WorldGraph
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


# ======================================================================================
# The graph itself
# ======================================================================================


class TestGraphLookupsOnThingsThatAreNotThere:
    def test_requiring_an_absent_entity_raises_rather_than_returning_none(self):
        """Callers that use `require_entity` have already decided absence is a bug."""
        graph = WorldGraph([_entity("a")], [])
        assert graph.require_entity("a").id == "a"
        with pytest.raises(KeyError) as error:
            graph.require_entity("ghost")
        assert "unknown entity 'ghost'" in str(error.value)

    def test_edges_of_an_unknown_node_are_empty_not_an_error(self):
        """The traversal asks about ids it has not confirmed; empty is the right answer."""
        graph = WorldGraph([_entity("a")], [])
        assert graph.dependencies_of("ghost") == []
        assert graph.dependents_of("ghost") == []

    def test_iterating_yields_every_entity_exactly_once(self):
        graph = WorldGraph([_entity("a"), _entity("b")], [])
        assert sorted(e.id for e in graph.iter_entities()) == ["a", "b"]



# ======================================================================================
# Business impact on an estate that declares nothing
# ======================================================================================


class TestBusinessImpactOnAnUndeclaredEstate:
    def test_an_empty_graph_yields_unknown_rather_than_perfect_health(self):
        """100 % availability over nothing is the most misleading number available."""
        graph = WorldGraph([], [])
        impact = business_impact(graph, propagate(graph))
        assert impact.availability is None
        assert impact.customers_affected is None
        assert impact.revenue_at_risk_per_hour is None

    def test_customer_regions_with_no_traffic_share_are_named_as_unknown(self):
        """Regions exist, so the reason is different from "no regions at all"."""
        graph = WorldGraph(
            [
                _entity(
                    "apac",
                    type=EntityType.CUSTOMER_REGION,
                    business=BusinessProfile(region="apac"),
                ),
                _entity("app"),
            ],
            [],
        )
        impact = business_impact(graph, propagate(graph))
        assert impact.availability is None
        reasons = " ".join(impact.unknown_reasons)
        assert "customer regions exist but" in reasons

    def test_a_partial_customer_count_is_declared_partial(self):
        """§24: some regions declare a population and some do not. Say which."""
        graph = WorldGraph(
            [
                _entity(
                    "apac",
                    type=EntityType.CUSTOMER_REGION,
                    business=BusinessProfile(
                        region="apac", traffic_share=0.5, customer_count=1000
                    ),
                ),
                _entity(
                    "emea",
                    type=EntityType.CUSTOMER_REGION,
                    business=BusinessProfile(region="emea", traffic_share=0.5),
                ),
                _entity("app", health=HealthState.DOWN),
            ],
            [
                DependencyEdge(
                    id=f"app--SERVES--{region}",
                    source_entity_id="app",
                    target_entity_id=region,
                    type=DependencyType.SERVES,
                )
                for region in ("apac", "emea")
            ],
        )
        impact = business_impact(graph, propagate(graph))
        reasons = " ".join(impact.unknown_reasons)
        assert "Customer count is partial" in reasons

    def test_an_unlabelled_region_rolls_up_to_nothing_rather_than_to_a_guess(self):
        """An empty label must not be bucketed into a continent it was never given."""
        assert rollup_region("") == ""

    def test_a_recognised_region_is_grouped_across_spellings(self):
        """Separators and case are stripped before matching, so these are one bucket."""
        assert rollup_region("ap-southeast-1 (Singapore)") == "APAC"
        assert rollup_region("Singapore") == "APAC"
        assert rollup_region("HONG KONG") == "APAC"
        assert rollup_region("eu-central-1 Frankfurt") == "EMEA"

    def test_an_unrecognised_region_keeps_its_own_name_rather_than_a_guess(self):
        """Bucketing an unknown label into a continent would invent a fact about it.

        It is uppercased but otherwise verbatim: `ap-southeast-1` alone matches no rule
        and stays `AP-SOUTHEAST-1`, which is the honest answer rather than a guessed one.
        """
        assert rollup_region("mars-central-1") == "MARS-CENTRAL-1"
        assert rollup_region("ap-southeast-1") == "AP-SOUTHEAST-1"

    def test_the_worst_of_several_bands_is_the_worst_one(self):
        assert worst_material_risk(Severity.LOW, Severity.CRITICAL, Severity.HIGH) is (
            Severity.CRITICAL
        )
        assert worst_material_risk(Severity.INFO, Severity.LOW) is Severity.LOW

    def test_with_no_bands_at_all_it_returns_the_floor(self):
        """`default=LOW`. An empty call must not return the highest band."""
        assert worst_material_risk() is Severity.LOW


# ======================================================================================
# Correlation edges
# ======================================================================================


class TestCorrelationEdges:
    def test_a_spatial_match_describes_itself_with_distance_and_bearing(self):
        match = SpatialMatch(
            entity=_entity("dc", type=EntityType.DATACENTER),
            distance_km=42.4,
            bearing_deg=45.0,
            proximity=0.5,
        )
        assert match.direction == "NE"
        assert match.describe() == "dc — 42 km NE of the event"

    def test_an_event_naming_an_entity_from_another_estate_pins_nothing(self):
        """Workspace isolation, at the correlation layer."""
        graph = WorldGraph([_entity("ours")], [])
        event = WorldEvent(
            id="evt-1",
            category=EventCategory.CLOUD_INCIDENT,
            title="Region impaired",
            severity=Severity.CRITICAL,
            source=SRC,
            directly_named_entity_ids=["theirs", "ours"],
            occurred_at=utcnow(),
        )
        pinned, matches, peak = correlate_event(graph, event)
        assert set(pinned) == {"ours"}
        assert matches == []
        assert peak == 1.0

    def test_a_compromise_pins_only_the_named_asset(self):
        """WorldGraph does not assume lateral movement succeeds.

        "Everything downstream is owned" is a materially different claim from "here is
        what this asset can reach", and only the second one is supported by a graph.
        """
        assert security_pins([], compromised_id="web") == {"web": 0.0}
        assert security_pins([], compromised_id="web", availability=0.3) == {"web": 0.3}

    def test_health_for_availability_reexports_the_same_bands(self):
        from app.models.core import health_from_value

        for value in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert health_for_availability(value) is health_from_value(value)


# ======================================================================================
# Confidence on data that is not live
# ======================================================================================


class TestConfidenceReflectsWhereTheDataCameFrom:
    @staticmethod
    def _estate() -> WorldGraph:
        return WorldGraph(
            [_entity("a", location=GeoPoint(lat=1.3, lon=103.8)), _entity("b")],
            [
                DependencyEdge(
                    id="a--DEPENDS_ON--b",
                    source_entity_id="a",
                    target_entity_id="b",
                    type=DependencyType.DEPENDS_ON,
                )
            ],
        )

    @staticmethod
    def _event(mode: DataMode, *, located: bool = True) -> WorldEvent:
        return WorldEvent(
            id="evt-1",
            category=EventCategory.EARTHQUAKE,
            title="Quake",
            severity=Severity.HIGH,
            source=DataSourceInfo(
                source_id="usgs", source_name="USGS", mode=mode, confidence=1.0
            ),
            location=GeoPoint(lat=1.3, lon=103.8) if located else None,
            exposure_radius_km=100.0 if located else 0.0,
            occurred_at=utcnow(),
        )

    def _confidence(self, event, *, proximity: float):
        from app.analysis.risk import assess_confidence

        graph = self._estate()
        return assess_confidence(
            graph,
            propagate(graph),
            origin_ids=["a"],
            event=event,
            truncated=False,
            proximity=proximity,
        )

    def test_a_replayed_event_is_labelled_as_replayed_not_as_live(self):
        confidence = self._confidence(self._event(DataMode.REPLAY), proximity=1.0)
        joined = " ".join(confidence.strong_evidence)
        assert "replayed from a recorded USGS fixture" in joined
        assert "reported live" not in joined

    def test_a_live_event_says_so(self):
        confidence = self._confidence(self._event(DataMode.LIVE), proximity=1.0)
        assert any("reported live by USGS" in item for item in confidence.strong_evidence)

    def test_a_located_event_with_nothing_inside_its_radius_records_the_miss(self):
        """Proximity zero on a located event is a finding, not an absence of one."""
        confidence = self._confidence(self._event(DataMode.LIVE), proximity=0.0)
        assert any(
            "no facility lies inside the modelled event radius" in item
            for item in confidence.uncertainties
        )

    def test_an_unlocated_event_makes_no_claim_about_radii_at_all(self):
        confidence = self._confidence(
            self._event(DataMode.LIVE, located=False), proximity=0.0
        )
        assert not any("event radius" in item for item in confidence.uncertainties)

    def test_a_supplier_origin_names_the_status_it_cannot_see(self):
        from app.analysis.risk import assess_confidence

        graph = WorldGraph([_entity("supplier-x", type=EntityType.SUPPLIER)], [])
        confidence = assess_confidence(
            graph,
            propagate(graph),
            origin_ids=["supplier-x"],
            event=None,
            truncated=False,
            proximity=0.0,
        )
        assert any(
            "current operating status of supplier-x is unavailable" in item
            for item in confidence.uncertainties
        )

    def test_a_mixed_provenance_estate_says_it_is_mixed(self):
        """§ provenance: a live estate carrying replayed records must not read as all-live."""
        from app.analysis.risk import _estate_provenance_notes as provenance_notes

        live = _entity("live-one", source=DataSourceInfo(
            source_id="a", source_name="A", mode=DataMode.LIVE
        ))
        synthetic = _entity("demo-one", source=DataSourceInfo(
            source_id="b", source_name="B", mode=DataMode.SYNTHETIC
        ))
        notes = provenance_notes(WorldGraph([live, synthetic], []))
        joined = " ".join(notes)
        assert "synthetic demonstration data" in joined
        assert "mixes live inventory with non-live records" in joined

    def test_a_uniformly_live_estate_carries_no_mixing_note(self):
        from app.analysis.risk import _estate_provenance_notes as provenance_notes

        live = _entity("live-one", source=DataSourceInfo(
            source_id="a", source_name="A", mode=DataMode.LIVE
        ))
        notes = provenance_notes(WorldGraph([live], []))
        assert not any("mixes live inventory" in note for note in notes)


class TestImpactDirectionAndCycleBounds:
    """Which way failure flows along each edge type, and the bound on cycle enumeration."""

    @staticmethod
    def _edge(source: str, target: str, edge_type: DependencyType) -> DependencyEdge:
        return DependencyEdge(
            id=f"{source}--{edge_type.value}--{target}",
            source_entity_id=source,
            target_entity_id=target,
            type=edge_type,
        )

    def test_a_replica_failing_does_not_take_down_its_primary(self):
        """That asymmetry is the whole point of a replica.

        `primary REPLICATES_TO standby` — the standby degrading must not propagate back.
        """
        graph = WorldGraph(
            [_entity("primary"), _entity("standby")],
            [self._edge("primary", "standby", DependencyType.REPLICATES_TO)],
        )
        assert graph._failure_successors("standby", None) == []
        # And the primary's own dependents are unaffected by the replica edge.
        assert graph._failure_successors("primary", None) == []

    def test_a_dependent_is_impacted_when_what_it_needs_fails(self):
        graph = WorldGraph(
            [_entity("app"), _entity("db")],
            [self._edge("app", "db", DependencyType.DEPENDS_ON)],
        )
        assert graph._failure_successors("db", None) == [("app", DependencyType.DEPENDS_ON)]

    def test_a_served_customer_region_is_impacted_by_the_service(self):
        """SERVES points forward: the region suffers when the service does."""
        graph = WorldGraph(
            [_entity("svc"), _entity("apac", type=EntityType.CUSTOMER_REGION)],
            [self._edge("svc", "apac", DependencyType.SERVES)],
        )
        assert graph._failure_successors("svc", None) == [("apac", DependencyType.SERVES)]

    def test_an_edge_type_filter_narrows_both_directions(self):
        graph = WorldGraph(
            [_entity("app"), _entity("db"), _entity("apac", type=EntityType.CUSTOMER_REGION)],
            [
                self._edge("app", "db", DependencyType.DEPENDS_ON),
                self._edge("app", "apac", DependencyType.SERVES),
            ],
        )
        assert graph._failure_successors("app", frozenset({DependencyType.SERVES})) == [
            ("apac", DependencyType.SERVES)
        ]
        assert graph._failure_successors("db", frozenset({DependencyType.SERVES})) == []
        assert graph._failure_successors("db", frozenset({DependencyType.DEPENDS_ON})) == [
            ("app", DependencyType.DEPENDS_ON)
        ]

    def test_cycle_enumeration_is_bounded(self):
        """`simple_cycles` is exponential in the worst case; a UI needs the first handful."""
        entities = [_entity(f"n{i}") for i in range(8)]
        edges = [
            self._edge(f"n{i}", f"n{(i + step) % 8}", DependencyType.DEPENDS_ON)
            for step in (1, 2, 3)
            for i in range(8)
        ]
        graph = WorldGraph(entities, edges)
        assert len(graph.cycles(limit=3)) == 3
        assert len(graph.cycles(limit=1)) == 1
        # The bound is a ceiling, not a target: an acyclic graph returns nothing.
        acyclic = WorldGraph(
            [_entity("a"), _entity("b")],
            [self._edge("a", "b", DependencyType.DEPENDS_ON)],
        )
        assert acyclic.cycles() == []


class TestTheBlastRadiusExplanation:
    """Explanations say what could not be computed in the same breath as what could."""

    @staticmethod
    def _estate() -> WorldGraph:
        return WorldGraph(
            [
                _entity("app", health=HealthState.DOWN),
                _entity("db"),
                _entity("region", type=EntityType.CLOUD_REGION),
            ],
            [
                DependencyEdge(
                    id="app--DEPENDS_ON--db",
                    source_entity_id="app",
                    target_entity_id="db",
                    type=DependencyType.DEPENDS_ON,
                    criticality=1.0,
                ),
                DependencyEdge(
                    id="db--HOSTED_IN--region",
                    source_entity_id="db",
                    target_entity_id="region",
                    type=DependencyType.HOSTED_IN,
                    criticality=1.0,
                ),
            ],
        )

    def test_a_pinned_origin_cannot_be_healed_by_its_own_dependencies(self):
        """"Singapore is DOWN" must not quietly become "Singapore is mostly up".

        A pinned entity is a boundary condition, so the solver never attributes a
        dominant cause to it. **That makes `_explain`'s "largest single dependency loss"
        branch unreachable through `calculate_blast_radius`**, which pins every origin —
        the line can never appear in an analysis. Recorded here rather than worked around:
        the invariant below is real and worth pinning, and the dead branch is a finding
        for the author to decide about, not something a test should paper over.
        """
        from app.analysis.blast_radius import calculate_blast_radius

        graph = self._estate()
        state = propagate(graph, initial_availability={"region": 0.0})
        # Unpinned, `app` does have a dominant cause: it is down because `db` is.
        assert state.dominant_cause["app"] == ("db", 1.0)

        # Pinned as an origin, it has none — and so the explanation never fires.
        pinned_state = propagate(graph, initial_availability={"region": 0.0, "app": 0.0})
        assert "app" not in pinned_state.dominant_cause

        result = calculate_blast_radius(
            graph, origin_ids=["region", "app"], origin_kind="ENTITY"
        )
        assert not any("largest single dependency loss" in line for line in result.explanations)

    def test_an_unknown_origin_is_refused_rather_than_analysed_as_nothing(self):
        """The guard that makes "No origin entity resolved." unreachable.

        That line stays uncovered on purpose: `origins` is non-empty by the time
        explanations are built, because this raise is what guarantees it. Reaching the
        line would mean removing the guard.
        """
        from app.analysis.blast_radius import calculate_blast_radius

        graph = WorldGraph([_entity("a")], [])
        for origins in ([], ["ghost"], ["ghost", "phantom"]):
            with pytest.raises(KeyError) as error:
                calculate_blast_radius(graph, origin_ids=origins, origin_kind="ENTITY")
            assert "no known origin entities" in str(error.value)

    def test_every_explanation_carries_the_modelled_estimate_marker(self):
        from app.analysis.blast_radius import calculate_blast_radius

        result = calculate_blast_radius(
            self._estate(), origin_ids=["region"], origin_kind="ENTITY"
        )
        assert any("MODELLED ESTIMATE" in line for line in result.explanations)
