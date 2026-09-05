"""Reality Pass: WorldGraph must not invent what it was not told.

Two halves, and both matter:

* **Imported-estate honesty** — an estate with no business metadata must produce
  ``UNKNOWN``, never a plausible number. These pin the sixteen engine bugs found in
  ``docs/REALITY_PASS_AUDIT.md``.
* **AtlasPay is unchanged** — the demo declares every field, so its behaviour must be
  byte-identical. If making unknowns representable had cost the demo anything, that would
  be a regression, not a trade.
"""

from __future__ import annotations

import pytest

from app.analysis.blast_radius import calculate_blast_radius
from app.analysis.business_impact import (
    business_impact,
    infrastructure_availability,
    is_customer_facing,
    regional_capacity,
    rollup_region,
)
from app.analysis.material_risk import material_risks
from app.analysis.propagation import propagate
from app.analysis.response_plan import generate_response_plan
from app.analysis.risk import score_impact
from app.graph.world_graph import WorldGraph
from app.models.core import (
    CRITICALITY_WEIGHTS,
    BusinessProfile,
    Criticality,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    GeoPoint,
    HealthState,
    WorldEntity,
)
from app.simulation.engine import build_deltas, compare, new_scenario, override_for_health

LIVE = DataSourceInfo(source_id="import", source_name="Imported inventory", mode=DataMode.LIVE)


def imported(
    entity_id: str,
    entity_type: EntityType,
    *,
    region: str = "southeastasia",
    **kwargs,
) -> WorldEntity:
    """An entity as a cloud import actually produces one: topology, no business meaning."""
    return WorldEntity(
        id=entity_id,
        type=entity_type,
        name=entity_id,
        source=LIVE,
        location=GeoPoint(lat=1.35, lon=103.8),
        business=BusinessProfile(region=region),
        **kwargs,
    )


def hosted_in(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        id=f"{source}--HOSTED_IN--{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=DependencyType.HOSTED_IN,
    )


@pytest.fixture
def imported_graph() -> WorldGraph:
    """Four resources in one region — the shape an Azure subscription produces."""
    entities = [
        imported("region-southeastasia", EntityType.CLOUD_REGION),
        imported("aks-prod", EntityType.KUBERNETES_CLUSTER),
        imported("sql-prod", EntityType.DATABASE),
        imported("web-app", EntityType.APPLICATION),
        imported("api-svc", EntityType.MICROSERVICE),
    ]
    edges = [
        hosted_in("aks-prod", "region-southeastasia"),
        hosted_in("sql-prod", "region-southeastasia"),
        hosted_in("web-app", "region-southeastasia"),
        hosted_in("api-svc", "region-southeastasia"),
    ]
    return WorldGraph(entities, edges)


# ======================================================================================
# The domain model can now say "I don't know"
# ======================================================================================


class TestUnknownIsRepresentable:
    def test_business_fields_default_to_none_not_zero(self):
        """B3. Zero revenue is a measurement; unknown revenue is not."""
        profile = BusinessProfile()
        assert profile.traffic_share is None
        assert profile.revenue_per_hour is None
        assert profile.customer_count is None
        assert profile.redundancy is None
        assert profile.has_traffic is False
        assert profile.has_revenue is False
        assert profile.has_customers is False

    def test_capacity_keeps_a_concrete_default(self):
        """Capacity is physical, and the solver must have a value. Documented as such."""
        assert BusinessProfile().capacity == 1.0

    def test_single_point_of_failure_is_tri_state(self):
        assert BusinessProfile(redundancy=1).is_single_point_of_failure is True
        assert BusinessProfile(redundancy=3).is_single_point_of_failure is False
        assert BusinessProfile().is_single_point_of_failure is None

    def test_criticality_defaults_to_unknown(self):
        """B4. A criticality WorldGraph chose is a judgement it was not entitled to make."""
        assert imported("x", EntityType.DATABASE).criticality is Criticality.UNKNOWN

    def test_unknown_criticality_scores_nothing(self):
        assert CRITICALITY_WEIGHTS[Criticality.UNKNOWN] == 0.0

    def test_customer_facing_is_tri_state_and_type_is_not_evidence(self):
        """B5. Every imported App Service was silently promoted to customer-facing."""
        assert is_customer_facing(imported("app", EntityType.APPLICATION)) is None
        assert is_customer_facing(imported("svc", EntityType.BUSINESS_SERVICE)) is None
        # A declaration is honoured in either form.
        assert is_customer_facing(imported("a", EntityType.APPLICATION, customer_facing=True)) is True
        assert is_customer_facing(imported("b", EntityType.APPLICATION, customer_facing=False)) is False
        assert (
            is_customer_facing(
                imported("c", EntityType.APPLICATION, metadata={"customer_facing": True})
            )
            is True
        )

    def test_customer_region_type_is_its_own_declaration(self):
        assert is_customer_facing(imported("r", EntityType.CUSTOMER_REGION)) is True


# ======================================================================================
# The engines stop inventing
# ======================================================================================


class TestNoInventedBusinessImpact:
    def test_availability_is_unknown_without_customer_regions(self, imported_graph: WorldGraph):
        """B1 — the worst finding. This used to return 1.0 for a fully degraded estate."""
        state = propagate(
            imported_graph, initial_availability={"region-southeastasia": 0.0}
        )
        impact = business_impact(imported_graph, state)

        assert impact.availability is None, "must not claim a customer-experienced figure"
        assert impact.traffic_impact is None
        assert any("customer regions" in reason for reason in impact.unknown_reasons)

    def test_infrastructure_availability_is_still_computed(self, imported_graph: WorldGraph):
        """The graph alone supports this figure, so it must not be withheld."""
        state = propagate(
            imported_graph, initial_availability={"region-southeastasia": 0.0}
        )
        impact = business_impact(imported_graph, state)
        assert impact.infrastructure_availability < 0.3, "the estate really is down"

    def test_revenue_and_customers_are_unknown_not_zero(self, imported_graph: WorldGraph):
        """B2. `0` reads as 'we checked, nothing'. The truth is 'we have no idea'."""
        state = propagate(
            imported_graph, initial_availability={"region-southeastasia": 0.0}
        )
        impact = business_impact(imported_graph, state)

        assert impact.revenue_at_risk_per_hour is None
        assert impact.customers_affected is None
        assert any("Revenue exposure is unknown" in r for r in impact.unknown_reasons)
        assert any("no revenue metadata" in r for r in impact.unknown_reasons)

    def test_unknown_reasons_explain_rather_than_merely_flag(self, imported_graph: WorldGraph):
        """A gap the operator cannot act on is only marginally better than a lie."""
        impact = business_impact(imported_graph, propagate(imported_graph))
        assert impact.unknown_reasons
        for reason in impact.unknown_reasons:
            assert len(reason) > 40, f"not an explanation: {reason!r}"
            assert "unknown" in reason.lower() or "partial" in reason.lower()

    def test_countable_facts_are_never_unknown(self, imported_graph: WorldGraph):
        """These need no business metadata, so withholding them would be its own dishonesty."""
        state = propagate(
            imported_graph, initial_availability={"region-southeastasia": 0.0}
        )
        impact = business_impact(imported_graph, state)
        assert impact.impacted_entity_count == 5
        assert impact.critical_services_impacted == 0  # nothing is declared CRITICAL
        assert impact.customer_regions_impacted == 0


class TestNoInventedRisk:
    def test_undeclared_criticality_does_not_score(self, imported_graph: WorldGraph):
        """B4. `+12 asset_criticality` used to fire on a criticality nobody set."""
        state = propagate(imported_graph, initial_availability={"region-southeastasia": 0.0})
        risk = score_impact(imported_graph, state, origin_ids=["region-southeastasia"])
        assert "asset_criticality" not in {c.code for c in risk.contributions}

    def test_undeclared_customer_facing_does_not_score(self, imported_graph: WorldGraph):
        """B5. `+25 customer_facing` used to fire on entity type alone."""
        state = propagate(imported_graph, initial_availability={"region-southeastasia": 0.0})
        risk = score_impact(imported_graph, state, origin_ids=["region-southeastasia"])
        assert "customer_facing" not in {c.code for c in risk.contributions}

    def test_undeclared_redundancy_does_not_score_as_a_spof(self, imported_graph: WorldGraph):
        """An undeclared redundancy is a coverage gap, not a finding."""
        state = propagate(imported_graph, initial_availability={"region-southeastasia": 0.0})
        risk = score_impact(imported_graph, state, origin_ids=["region-southeastasia"])
        assert "single_point_of_failure" not in {c.code for c in risk.contributions}

    def test_a_declared_estate_still_scores(self, atlaspay_graph: WorldGraph):
        """The point is not to score nothing — it is to score only on evidence."""
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        risk = score_impact(atlaspay_graph, state, origin_ids=["cloud-region-singapore"])
        codes = {c.code for c in risk.contributions}
        assert "asset_criticality" in codes
        assert "customer_facing" in codes

    def test_confidence_does_not_call_a_live_estate_synthetic(self, imported_graph: WorldGraph):
        """B7. A falsehood in the one field whose entire job is honesty."""
        from app.analysis.risk import assess_confidence

        state = propagate(imported_graph, initial_availability={"region-southeastasia": 0.0})
        confidence = assess_confidence(
            imported_graph,
            state,
            event=None,
            origin_ids=["region-southeastasia"],
            truncated=False,
        )
        joined = " ".join(confidence.uncertainties).lower()
        assert "atlaspay" not in joined
        assert "synthetic demo" not in joined
        # It should instead name the real gap.
        assert "criticality" in joined

    def test_confidence_still_names_synthetic_data_when_it_is_synthetic(
        self, atlaspay_graph: WorldGraph
    ):
        from app.analysis.risk import assess_confidence

        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        confidence = assess_confidence(
            atlaspay_graph,
            state,
            event=None,
            origin_ids=["cloud-region-singapore"],
            truncated=False,
        )
        assert any("synthetic" in item for item in confidence.uncertainties)


class TestConcentrationRiskWithoutTraffic:
    def test_single_region_concentration_is_visible_without_traffic_metadata(
        self, imported_graph: WorldGraph
    ):
        """B15. The most valuable real-world finding used to be structurally invisible."""
        risks = material_risks(imported_graph, [])
        concentration = [r for r in risks if r.id.startswith("risk-concentration-")]
        assert concentration, "an all-in-one-region estate must produce a concentration risk"

        risk = concentration[0]
        codes = {c.code for c in risk.contributions}
        assert "workload_concentration" in codes
        # And it must say what the score does not rest on.
        assert "no_traffic_metadata" in codes
        assert "traffic_concentration" not in codes

    def test_count_based_concentration_needs_enough_workloads(self):
        """"60% of your estate is in one region" describing two resources is noise."""
        graph = WorldGraph(
            [
                imported("region-a", EntityType.CLOUD_REGION),
                imported("vm-1", EntityType.KUBERNETES_CLUSTER),
            ],
            [hosted_in("vm-1", "region-a")],
        )
        assert [r for r in material_risks(graph, []) if "concentration" in r.id] == []

    def test_traffic_based_concentration_still_wins_when_declared(
        self, atlaspay_graph: WorldGraph
    ):
        risks = [r for r in material_risks(atlaspay_graph, []) if "concentration" in r.id]
        assert risks
        codes = {c.code for risk in risks for c in risk.contributions}
        assert "traffic_concentration" in codes
        assert "no_traffic_metadata" not in codes


class TestRegionRollupIsDerived:
    """B6. A fixture lookup table inside a generic engine."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("southeastasia", "APAC"),
            ("eastasia", "APAC"),
            ("centralindia", "APAC"),
            ("southindia", "APAC"),
            ("japaneast", "APAC"),
            ("australiaeast", "APAC"),
            ("westeurope", "EMEA"),
            ("northeurope", "EMEA"),
            ("uksouth", "EMEA"),
            ("francecentral", "EMEA"),
            ("eastus", "AMER"),
            ("westus2", "AMER"),
            ("brazilsouth", "AMER"),
            ("canadacentral", "AMER"),
            # The fixture's own labels keep working.
            ("SINGAPORE", "APAC"),
            ("FRANKFURT", "EMEA"),
            ("VIRGINIA", "AMER"),
        ],
    )
    def test_cloud_regions_group_without_a_lookup_table(self, label: str, expected: str):
        assert rollup_region(label) == expected

    def test_an_unrecognised_region_keeps_its_own_name(self):
        """Reported honestly rather than guessed into a continent."""
        assert rollup_region("atlantis-central") == "ATLANTIS-CENTRAL"

    def test_imported_capacity_groups_into_an_operating_region(
        self, imported_graph: WorldGraph
    ):
        capacity = regional_capacity(imported_graph, propagate(imported_graph))
        assert "APAC" in capacity
        assert "SOUTHEASTASIA" not in capacity


class TestSimulationRendersUnknown:
    def test_comparison_table_prints_unknown_not_a_number(self, imported_graph: WorldGraph):
        scenario = new_scenario("region loss")
        scenario.overrides.append(
            override_for_health("region-southeastasia", HealthState.DOWN)
        )
        comparison = compare(imported_graph, scenario)
        rows = {row.key: row for row in comparison.deltas}

        assert rows["customers_affected"].simulated == "UNKNOWN"
        assert rows["revenue_at_risk"].simulated == "UNKNOWN"
        # The customer-availability row is omitted entirely rather than shown as 100%.
        assert "availability" not in rows
        # Infrastructure availability is computable and must be shown.
        assert rows["infrastructure_availability"].simulated != "UNKNOWN"
        assert rows["infrastructure_availability"].direction == "worse"

    def test_unknown_never_reads_as_an_improvement(self):
        """Colouring a missing measurement green is the worst possible reading of it."""
        from app.models.analysis import WorldSnapshotMetrics

        baseline = WorldSnapshotMetrics(infrastructure_availability=1.0, customers_affected=100)
        simulated = WorldSnapshotMetrics(infrastructure_availability=0.5, customers_affected=None)
        rows = {row.key: row for row in build_deltas(baseline, simulated)}
        assert rows["customers_affected"].direction == "same"


class TestResponsePlanWithoutMetadata:
    def test_no_invented_capacity_percentage(self, imported_graph: WorldGraph):
        """A percentage pulled from nowhere is worse than an instruction to go measure."""
        peer = imported("aks-dr", EntityType.KUBERNETES_CLUSTER, region="eastasia")
        imported_graph.add_entity(peer)
        result = calculate_blast_radius(imported_graph, origin_ids=["aks-prod"])
        plan = generate_response_plan(imported_graph, result)

        for action in plan.actions:
            assert "capacity by" not in action.action, (
                f"invented an uplift figure with no traffic data: {action.action!r}"
            )

    def test_unknown_reasons_reach_the_plan_assumptions(self, imported_graph: WorldGraph):
        """A missing input is an assumption the reader makes implicitly. Surface it."""
        result = calculate_blast_radius(imported_graph, origin_ids=["region-southeastasia"])
        plan = generate_response_plan(imported_graph, result)
        joined = " ".join(plan.assumptions).lower()
        assert "revenue exposure is unknown" in joined

    def test_plan_does_not_claim_synthetic_data_for_a_live_estate(
        self, imported_graph: WorldGraph
    ):
        """B8."""
        result = calculate_blast_radius(imported_graph, origin_ids=["region-southeastasia"])
        plan = generate_response_plan(imported_graph, result)
        joined = " ".join(plan.assumptions).lower()
        assert "atlaspay" not in joined
        assert "read-only" in joined


class TestInfrastructureAvailability:
    def test_healthy_estate_reads_healthy(self, imported_graph: WorldGraph):
        for entity in imported_graph.entities:
            entity.health = HealthState.HEALTHY
        assert infrastructure_availability(
            imported_graph, propagate(imported_graph)
        ) == pytest.approx(1.0)

    def test_degraded_estate_reads_degraded(self, imported_graph: WorldGraph):
        state = propagate(imported_graph, initial_availability={"region-southeastasia": 0.0})
        assert infrastructure_availability(imported_graph, state) < 0.3


# ======================================================================================
# AtlasPay is unchanged
# ======================================================================================


class TestAtlasPayRegression:
    """The demo declares every field, so nothing here may move.

    These are exact-value assertions on purpose. A tolerance would let the very drift the
    Reality Pass was meant to avoid slip through unnoticed.
    """

    def test_hero_risk_score_is_unchanged(self, atlaspay_graph: WorldGraph):
        from app.adapters.fixtures import taiwan_earthquake
        from app.analysis.correlation import correlate_event

        event = taiwan_earthquake()
        pinned, _matches, proximity = correlate_event(atlaspay_graph, event)
        result = calculate_blast_radius(
            atlaspay_graph,
            origin_ids=sorted(pinned),
            origin_kind="EVENT",
            initial_availability=pinned,
            event=event,
            proximity=proximity,
        )
        assert result.severity.value == "HIGH"
        assert result.risk.score == pytest.approx(68.7, abs=0.05)

    def test_hero_business_impact_is_unchanged(self, atlaspay_graph: WorldGraph):
        state = propagate(
            atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0}
        )
        impact = business_impact(atlaspay_graph, state)
        assert impact.availability == pytest.approx(0.90451, abs=1e-5)
        assert impact.customers_affected == 41_500
        assert impact.revenue_at_risk_per_hour == pytest.approx(381_563.02, abs=0.01)
        # A fully declared estate has nothing to report as unknown.
        assert impact.unknown_reasons == []

    def test_hero_simulation_table_is_unchanged(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("Taiwan supplier unavailable")
        scenario.overrides.append(
            override_for_health("supplier-taiwan-hardware", HealthState.DOWN)
        )
        comparison = compare(atlaspay_graph, scenario)
        rows = {row.key: row for row in comparison.deltas}

        assert rows["availability"].baseline == "100.00%"
        assert rows["availability"].simulated == "90.45%"
        assert rows["capacity.APAC"].simulated == "69%"
        assert rows["customers_affected"].simulated == "41,500"
        assert rows["revenue_at_risk"].simulated == "$382K"
        assert rows["material_risk"].baseline == "LOW"
        assert rows["material_risk"].simulated == "HIGH"

    def test_atlaspay_still_declares_everything(self, atlaspay_graph: WorldGraph):
        """If the fixture stopped declaring, the regression tests above would go quiet."""
        regions = atlaspay_graph.entities_of_type(EntityType.CUSTOMER_REGION)
        assert regions
        for region in regions:
            assert region.business.has_traffic
            assert region.business.has_revenue
            assert region.business.has_customers

    def test_atlaspay_criticality_is_declared_everywhere(self, atlaspay_graph: WorldGraph):
        undeclared = [
            e.id for e in atlaspay_graph.entities if e.criticality is Criticality.UNKNOWN
        ]
        assert undeclared == []
