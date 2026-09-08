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


class TestConfidenceNamesTheTraversalsOwnLimits:
    """Truncation and non-convergence are findings about the *answer*, not the estate.

    Both were uncovered. An analysis that hit its depth budget, or whose solver ran out of
    iterations, has to say so: the impact it reports is a lower bound, and a reader who
    does not know that will treat it as the whole picture.
    """

    @staticmethod
    def _confidence(*, truncated: bool, converged: bool = True):
        from app.analysis.risk import assess_confidence

        graph = WorldGraph([_entity("a"), _entity("b")], [])
        state = propagate(graph)
        if not converged:
            state.converged = False
        return assess_confidence(
            graph, state, origin_ids=["a"], event=None, truncated=truncated, proximity=0.0
        )

    def test_a_truncated_traversal_says_the_impact_may_be_wider(self):
        confidence = self._confidence(truncated=True)
        assert any(
            "traversal was truncated; impact may be wider" in item
            for item in confidence.uncertainties
        )

    def test_an_untruncated_traversal_makes_no_such_claim(self):
        confidence = self._confidence(truncated=False)
        assert not any("truncated" in item for item in confidence.uncertainties)

    def test_a_solver_that_did_not_converge_says_so(self):
        confidence = self._confidence(truncated=False, converged=False)
        assert any(
            "did not fully converge" in item for item in confidence.uncertainties
        )

    def test_both_limits_lower_the_score_rather_than_only_being_narrated(self):
        """A caveat that does not move the number is decoration."""
        clean = self._confidence(truncated=False)
        truncated = self._confidence(truncated=True)
        stalled = self._confidence(truncated=False, converged=False)
        assert truncated.score < clean.score
        assert stalled.score < clean.score

    def test_a_synthetic_estate_is_labelled_synthetic(self):
        from app.analysis.risk import _estate_provenance_notes

        synthetic = _entity("demo", source=DataSourceInfo(
            source_id="fx", source_name="Fixture", mode=DataMode.SYNTHETIC
        ))
        notes = _estate_provenance_notes(WorldGraph([synthetic], []))
        assert any("synthetic demonstration data" in note for note in notes)
        # One mode only, so nothing about mixing.
        assert not any("mixes" in note for note in notes)

    def test_a_simulated_record_is_labelled_simulated(self):
        from app.analysis.risk import _estate_provenance_notes

        simulated = _entity("what-if", source=DataSourceInfo(
            source_id="sim", source_name="Simulation", mode=DataMode.SIMULATED
        ))
        notes = _estate_provenance_notes(WorldGraph([simulated], []))
        assert any("simulated world state" in note for note in notes)


class TestThePlanCarriesTheAnalysisItsOwnCaveats:
    """"Anything the impact model could not compute is an assumption the reader is making
    implicitly, so it belongs in the plan rather than only in the analysis panel."

    The truncation assumption and the entities-missing-from-the-graph guards were all
    uncovered. A plan is what somebody acts on; a caveat that stayed behind in the
    analysis panel is a caveat nobody read.
    """

    @staticmethod
    def _result(**overrides):
        from app.models.analysis import (
            BlastRadiusResult,
            BusinessImpact,
            Confidence,
            RiskScore,
        )

        base = {
            "id": "blast-1",
            "origin_kind": "ENTITY",
            "origin_ids": ["a"],
            "origin_label": "a",
            "severity": Severity.HIGH,
            "risk": RiskScore(score=50.0, severity=Severity.HIGH, contributions=[]),
            "business_impact": BusinessImpact(
                infrastructure_availability=0.5,
                critical_services_impacted=1,
                customer_regions_impacted=0,
                unknown_reasons=["Revenue exposure is unknown: no revenue metadata."],
            ),
            "confidence": Confidence(score=0.7),
        }
        base.update(overrides)
        return BlastRadiusResult(**base)

    def test_the_impact_models_unknowns_become_the_plans_assumptions(self):
        from app.analysis.response_plan import generate_response_plan

        graph = WorldGraph([_entity("a")], [])
        plan = generate_response_plan(graph, self._result())
        assert "Revenue exposure is unknown: no revenue metadata." in plan.assumptions

    def test_a_truncated_analysis_says_so_in_the_plan_not_only_in_the_analysis(self):
        from app.analysis.response_plan import generate_response_plan

        graph = WorldGraph([_entity("a")], [])
        clean = generate_response_plan(graph, self._result())
        truncated = generate_response_plan(graph, self._result(truncated=True))
        assert not any("traversal was truncated" in a.lower() for a in clean.assumptions)
        assert any(
            a == "Dependency traversal was truncated; entities beyond the traversal bound are "
            "not represented in this plan."
            for a in truncated.assumptions
        )

    def test_a_plan_over_nothing_still_recommends_watching_rather_than_nothing(self):
        from app.analysis.response_plan import generate_response_plan

        graph = WorldGraph([_entity("a")], [])
        plan = generate_response_plan(graph, self._result())
        assert plan.actions
        assert all(action.executed is False for action in plan.actions)


class TestTheDemoHeadlineCountsAreDerived:
    """"Derived, not hardcoded, so they cannot drift from the fixture."

    The function that does the deriving was never called by a test, which is how a
    "cannot drift" claim quietly stops being true.
    """

    def test_the_counts_match_a_count_of_the_fixture_itself(self):
        from app.fixtures.atlaspay import build_atlaspay, headline_counts

        entities, _edges = build_atlaspay()
        counts = headline_counts()
        wanted = {
            EntityType.BUSINESS_SERVICE,
            EntityType.APPLICATION,
            EntityType.MICROSERVICE,
            EntityType.DATABASE,
        }
        expected = sum(
            1
            for e in entities
            if e.criticality.value == "CRITICAL" and e.type in wanted
        )
        assert counts["critical_services"] == expected > 0

    def test_infrastructure_counts_the_types_an_operator_runs(self):
        """An inclusion list, written out — an organization node is not an asset."""
        from app.fixtures.atlaspay import build_atlaspay, headline_counts

        entities, _edges = build_atlaspay()
        counted = {
            EntityType.KUBERNETES_CLUSTER,
            EntityType.DATABASE,
            EntityType.CLOUD_REGION,
            EntityType.DATACENTER,
            EntityType.OFFICE,
            EntityType.SUPPLIER,
            EntityType.FACTORY,
            EntityType.NETWORK_NODE,
            EntityType.EXTERNAL_API,
            EntityType.MICROSERVICE,
        }
        counts = headline_counts()
        assert counts["infrastructure_assets"] == sum(
            1 for e in entities if e.type in counted
        )
        assert counts["infrastructure_assets"] < len(entities)
        for absent in (EntityType.ORGANIZATION, EntityType.CUSTOMER_REGION):
            assert absent not in counted


class TestMaterialRiskFilters:
    """The cards the left-hand rail shows, and the things it deliberately does not.

    Three filters were uncovered: the entity types that are never a single point of
    failure worth naming, the criticality floor beneath which one is not material, and
    the duplicate-CVE guard. Each one exists to stop the rail filling with noise, which
    is the failure mode that makes an operator ignore it.
    """

    @staticmethod
    def _hub_estate(hub_type: EntityType, hub_criticality) -> WorldGraph:
        """One hub with six dependents and no redundancy."""
        from app.models.core import Criticality

        # `redundancy=1` is a *declared* absence of redundancy. `None` would be a
        # coverage gap, and reporting that as a single point of failure would assert
        # something nobody said (REALITY_PASS_AUDIT B3).
        hub = _entity(
            "hub",
            type=hub_type,
            criticality=hub_criticality,
            business=BusinessProfile(region="westeurope", redundancy=1),
        )
        leaves = [_entity(f"leaf{i}", criticality=Criticality.HIGH) for i in range(6)]
        edges = [
            DependencyEdge(
                id=f"leaf{i}--DEPENDS_ON--hub",
                source_entity_id=f"leaf{i}",
                target_entity_id="hub",
                type=DependencyType.DEPENDS_ON,
                criticality=1.0,
            )
            for i in range(6)
        ]
        return WorldGraph([hub, *leaves], edges)

    def test_a_critical_hub_is_reported_as_a_single_point_of_failure(self):
        from app.analysis.material_risk import material_risks
        from app.models.core import Criticality

        risks = material_risks(self._hub_estate(EntityType.DATABASE, Criticality.CRITICAL), [])
        assert any("hub" in " ".join(r.focus_entity_ids) for r in risks)

    def test_an_undeclared_redundancy_is_a_coverage_gap_not_a_finding(self):
        """`None` means nobody said. Calling it a single point of failure invents one."""
        from app.analysis.material_risk import material_risks
        from app.models.core import Criticality

        undeclared = _entity(
            "hub", type=EntityType.DATABASE, criticality=Criticality.CRITICAL
        )
        leaves = [_entity(f"leaf{i}", criticality=Criticality.HIGH) for i in range(6)]
        edges = [
            DependencyEdge(
                id=f"leaf{i}--DEPENDS_ON--hub",
                source_entity_id=f"leaf{i}",
                target_entity_id="hub",
                type=DependencyType.DEPENDS_ON,
                criticality=1.0,
            )
            for i in range(6)
        ]
        assert undeclared.business.is_single_point_of_failure is None
        risks = material_risks(WorldGraph([undeclared, *leaves], edges), [])
        assert not any("hub" in " ".join(r.focus_entity_ids) for r in risks)

    @pytest.mark.parametrize(
        "hub_type", [EntityType.ORGANIZATION, EntityType.NETWORK_NODE]
    )
    def test_some_entity_types_are_never_named_as_a_single_point_of_failure(self, hub_type):
        """The organization node depends on everything by construction; so does a router.

        Naming either is true and useless, and a rail of useless cards is an ignored rail.
        """
        from app.analysis.material_risk import material_risks
        from app.models.core import Criticality

        risks = material_risks(self._hub_estate(hub_type, Criticality.CRITICAL), [])
        assert not any("hub" in " ".join(r.focus_entity_ids) for r in risks)

    @pytest.mark.parametrize("criticality_name", ["MEDIUM", "LOW", "UNKNOWN"])
    def test_a_hub_below_the_criticality_floor_is_not_material(self, criticality_name):
        """"Material" is the word in the name. An uncritical hub failing is not."""
        from app.analysis.material_risk import material_risks
        from app.models.core import Criticality

        estate = self._hub_estate(EntityType.DATABASE, Criticality(criticality_name))
        risks = material_risks(estate, [])
        assert not any("hub" in " ".join(r.focus_entity_ids) for r in risks)

    def test_the_same_cve_reported_twice_produces_one_card_not_two(self):
        """Two feeds can carry the same advisory. The rail must not double it."""
        from app.analysis.correlation import match_vulnerable_assets
        from app.analysis.material_risk import material_risks
        from app.models.core import SoftwareComponent

        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        graph = WorldGraph([_entity("web", software=[vulnerable])], [])
        assert match_vulnerable_assets(graph, cve_id="CVE-2024-0001", product_names=[])

        def event(event_id: str) -> WorldEvent:
            return WorldEvent(
                id=event_id,
                category=EventCategory.SECURITY_VULNERABILITY,
                title="Critical RCE",
                severity=Severity.CRITICAL,
                source=SRC,
                metadata={"cve_id": "CVE-2024-0001", "product_names": ["nginx"]},
                occurred_at=utcnow(),
            )

        once = material_risks(graph, [event("kev:1")])
        twice = material_risks(graph, [event("kev:1"), event("nvd:1")])
        assert len(twice) == len(once)

    def test_a_known_ransomware_campaign_raises_the_score(self):
        """CISA KEV flags these, and it is the difference between "patch" and "now"."""
        from app.analysis.material_risk import material_risks
        from app.models.core import SoftwareComponent

        vulnerable = SoftwareComponent(name="nginx", version="1.0", cve_ids=["CVE-2024-0001"])
        graph = WorldGraph([_entity("web", software=[vulnerable])], [])

        def event(*, ransomware: bool) -> WorldEvent:
            metadata = {"cve_id": "CVE-2024-0001", "product_names": ["nginx"]}
            if ransomware:
                metadata["known_ransomware_use"] = True
            return WorldEvent(
                id="kev:1",
                category=EventCategory.SECURITY_VULNERABILITY,
                title="Critical RCE",
                severity=Severity.CRITICAL,
                source=SRC,
                metadata=metadata,
                occurred_at=utcnow(),
            )

        plain = material_risks(graph, [event(ransomware=False)])[0]
        flagged = material_risks(graph, [event(ransomware=True)])[0]
        assert flagged.score > plain.score
        assert any(c.code == "known_ransomware" for c in flagged.contributions)
        assert not any(c.code == "known_ransomware" for c in plain.contributions)
