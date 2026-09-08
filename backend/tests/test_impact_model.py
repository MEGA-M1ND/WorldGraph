"""WorldGraph V1 Impact Model — propagation, business impact, risk boundaries."""

from __future__ import annotations

import pytest

from app.analysis.blast_radius import MAX_CRITICAL_PATHS, calculate_blast_radius
from app.analysis.business_impact import business_impact, regional_capacity, snapshot_metrics
from app.analysis.propagation import (
    IMPACT_THRESHOLD,
    edge_transfer,
    propagate,
)
from app.analysis.risk import WEIGHTS, assess_confidence, score_impact
from app.graph.world_graph import WorldGraph
from app.models.core import (
    HEALTH_VALUES,
    Criticality,
    DependencyType,
    HealthState,
    Severity,
    health_from_value,
    severity_from_score,
)

from .test_graph import edge, entity


class TestEdgeTransfer:
    def test_healthy_dependency_transfers_nothing(self):
        assert edge_transfer(1.0, coupling=1.0, redundancy=0.0) == 1.0

    def test_hard_unredundant_dependency_takes_dependent_down(self):
        assert edge_transfer(0.0, coupling=1.0, redundancy=0.0) == 0.0

    def test_redundancy_absorbs_proportionally(self):
        # 100% loss × full criticality, 55% absorbed by failover → 45% lost, 55% remains.
        assert edge_transfer(0.0, coupling=1.0, redundancy=0.55) == pytest.approx(0.55)

    def test_full_redundancy_makes_a_dependency_harmless(self):
        assert edge_transfer(0.0, coupling=1.0, redundancy=1.0) == 1.0

    def test_partial_criticality_scales_the_loss(self):
        assert edge_transfer(0.0, coupling=0.3, redundancy=0.0) == pytest.approx(0.7)

    def test_result_is_always_bounded(self):
        assert edge_transfer(-5.0, coupling=2.0, redundancy=-1.0) == 0.0
        assert edge_transfer(5.0, coupling=1.0, redundancy=0.0) == 1.0


class TestHealthRoundTrip:
    @pytest.mark.parametrize("state", list(HealthState))
    def test_health_value_maps_back_to_itself(self, state: HealthState):
        if state is HealthState.UNKNOWN:
            # UNKNOWN is deliberately optimistic-but-imperfect; it reads back as HEALTHY.
            assert health_from_value(HEALTH_VALUES[state]) is HealthState.HEALTHY
            return
        assert health_from_value(HEALTH_VALUES[state]) is state


class TestPropagation:
    def test_healthy_world_is_fully_available(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph)
        assert all(value >= IMPACT_THRESHOLD for value in state.availability.values())
        assert all(value >= IMPACT_THRESHOLD for value in state.capacity.values())

    def test_pinned_entity_is_not_healed_by_its_dependencies(self):
        """'Singapore is DOWN' must not quietly become 'Singapore is mostly up'."""
        graph = WorldGraph([entity("a"), entity("b")], [edge("a", "b")])
        state = propagate(graph, initial_availability={"a": 0.0})
        assert state.availability["a"] == 0.0

    def test_failure_propagates_through_a_chain(self):
        graph = WorldGraph(
            [entity("a"), entity("b"), entity("c")],
            [edge("b", "a"), edge("c", "b")],
        )
        state = propagate(graph, initial_availability={"a": 0.0})
        assert state.availability["b"] == 0.0
        assert state.availability["c"] == 0.0

    def test_redundancy_dampens_along_the_chain(self):
        graph = WorldGraph(
            [entity("a"), entity("b")], [edge("b", "a", redundancy=0.5)]
        )
        state = propagate(graph, initial_availability={"a": 0.0})
        assert state.availability["b"] == pytest.approx(0.5)

    def test_converges_on_a_cycle(self):
        graph = WorldGraph(
            [entity("a"), entity("b"), entity("c")],
            [edge("b", "a"), edge("c", "b"), edge("a", "c")],
        )
        state = propagate(graph, initial_availability={"a": 0.0})
        assert state.converged is True
        assert all(0.0 <= value <= 1.0 for value in state.availability.values())

    def test_capacity_and_availability_diverge_for_supply_chains(
        self, atlaspay_graph: WorldGraph
    ):
        """The whole point of capacity_impact.

        Losing the hardware supplier must constrain APAC capacity substantially while
        leaving most traffic still flowing. A model that collapsed the two would report a
        fictional outage.
        """
        state = propagate(atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0})
        cluster = "payments-k8s-singapore"
        assert state.availability[cluster] > 0.85, "traffic should still be flowing"
        assert state.capacity[cluster] < 0.5, "replacement capacity should be badly hit"

    def test_capacity_override_caps_availability(self):
        """A cluster that can serve 40% of load cannot answer more than 40% of requests."""
        graph = WorldGraph([entity("a")])
        state = propagate(graph, capacity_overrides={"a": 0.4})
        assert state.availability["a"] == pytest.approx(0.4)

    def test_disabling_an_edge_severs_propagation(self):
        graph = WorldGraph([entity("a"), entity("b")], [edge("b", "a")])
        state = propagate(
            graph,
            initial_availability={"a": 0.0},
            disabled_edge_ids=frozenset({"a--DEPENDS_ON->b", "b--DEPENDS_ON->a"}),
        )
        assert state.availability["b"] == 1.0

    def test_unknown_pin_is_ignored_rather_than_crashing(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"ghost": 0.0})
        assert "ghost" not in state.availability

    def test_replica_failure_does_not_hurt_the_primary(self, atlaspay_graph: WorldGraph):
        state = propagate(
            atlaspay_graph, initial_availability={"postgres-frankfurt-replica": 0.0}
        )
        assert state.availability["postgres-singapore"] >= IMPACT_THRESHOLD

    def test_dominant_cause_names_the_worst_dependency(self):
        graph = WorldGraph(
            [entity("svc"), entity("big"), entity("small")],
            [edge("svc", "big", criticality=1.0), edge("svc", "small", criticality=0.1)],
        )
        state = propagate(graph, initial_availability={"big": 0.0, "small": 0.0})
        assert state.dominant_cause["svc"][0] == "big"


class TestBusinessImpact:
    def test_healthy_world_has_no_impact(self, atlaspay_graph: WorldGraph):
        impact = business_impact(atlaspay_graph, propagate(atlaspay_graph))
        assert impact.availability == 1.0
        assert impact.customers_affected == 0
        assert impact.revenue_at_risk_per_hour == 0.0
        assert impact.critical_services_impacted == 0

    def test_disclaimer_is_always_present(self, atlaspay_graph: WorldGraph):
        """Schema-enforced, so a careless serializer cannot drop it."""
        impact = business_impact(atlaspay_graph, propagate(atlaspay_graph))
        assert impact.disclaimer == "MODELLED ESTIMATE"
        assert impact.model_dump()["disclaimer"] == "MODELLED ESTIMATE"

    def test_region_outage_hits_customers_and_revenue(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        impact = business_impact(atlaspay_graph, state)
        assert impact.availability < 0.5
        assert impact.customers_affected > 0
        assert impact.revenue_at_risk_per_hour > 0
        assert impact.critical_services_impacted >= 3
        assert impact.sla_breaches, "TIER-0 services should breach in a region outage"

    def test_regional_capacity_uses_the_capacity_solve(self, atlaspay_graph: WorldGraph):
        baseline = regional_capacity(atlaspay_graph, propagate(atlaspay_graph))
        assert baseline["APAC"] == pytest.approx(1.0, abs=1e-6)
        constrained = regional_capacity(
            atlaspay_graph,
            propagate(atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0}),
        )
        assert constrained["APAC"] < 0.8
        assert constrained["EMEA"] == pytest.approx(1.0, abs=1e-6), "EMEA is unaffected"

    def test_snapshot_metrics_carry_the_disclaimer(self, atlaspay_graph: WorldGraph):
        metrics = snapshot_metrics(atlaspay_graph, propagate(atlaspay_graph))
        assert metrics.disclaimer == "MODELLED ESTIMATE"
        assert metrics.material_risk is Severity.LOW


class TestRiskBands:
    """Band boundaries are a documented contract; these pin them."""

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.0, Severity.LOW),
            (24.9, Severity.LOW),
            (25.0, Severity.MODERATE),
            (49.9, Severity.MODERATE),
            (50.0, Severity.HIGH),
            (74.9, Severity.HIGH),
            (75.0, Severity.CRITICAL),
            (100.0, Severity.CRITICAL),
        ],
    )
    def test_band_boundaries(self, score: float, expected: Severity):
        assert severity_from_score(score) is expected

    def test_out_of_range_scores_are_clamped(self):
        assert severity_from_score(-10.0) is Severity.LOW
        assert severity_from_score(500.0) is Severity.CRITICAL


class TestRiskScoring:
    def test_healthy_world_scores_zero(self, atlaspay_graph: WorldGraph):
        risk = score_impact(
            atlaspay_graph, propagate(atlaspay_graph), origin_ids=["payments-api"]
        )
        assert risk.score == 0.0
        assert risk.severity is Severity.LOW

    def test_every_contribution_is_named_and_signed(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        risk = score_impact(
            atlaspay_graph, state, origin_ids=["cloud-region-singapore"], max_depth_reached=4
        )
        assert risk.contributions, "a score with no derivation is not shippable"
        for item in risk.contributions:
            assert item.code
            assert item.label
        assert risk.score == pytest.approx(
            max(0.0, min(100.0, sum(c.points for c in risk.contributions))), abs=0.05
        )

    def test_score_scales_with_severity_of_loss(self, atlaspay_graph: WorldGraph):
        """A critical asset nicked must not score like a critical asset destroyed."""
        mild = score_impact(
            atlaspay_graph,
            propagate(atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.9}),
            origin_ids=["supplier-taiwan-hardware"],
        )
        severe = score_impact(
            atlaspay_graph,
            propagate(atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0}),
            origin_ids=["supplier-taiwan-hardware"],
        )
        assert severe.score > mild.score

    def test_redundancy_earns_a_negative_contribution(self, atlaspay_graph: WorldGraph):
        """A resilient estate must be able to score lower for the same event."""
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-mumbai": 0.0})
        risk = score_impact(atlaspay_graph, state, origin_ids=["cloud-region-mumbai"])
        credits = [c for c in risk.contributions if c.points < 0]
        assert credits, "cloud regions have redundancy 3 and should earn a credit"
        assert credits[0].points == WEIGHTS["redundancy_credit"]

    def test_stale_observation_reduces_the_score(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        fresh = score_impact(atlaspay_graph, state, origin_ids=["cloud-region-singapore"])
        stale = score_impact(
            atlaspay_graph,
            state,
            origin_ids=["cloud-region-singapore"],
            stale_seconds=48 * 3600,
        )
        assert stale.score < fresh.score

    def test_score_never_leaves_zero_to_one_hundred(self, atlaspay_graph: WorldGraph):
        state = propagate(
            atlaspay_graph,
            initial_availability={
                "cloud-region-singapore": 0.0,
                "cloud-region-mumbai": 0.0,
                "cloud-region-frankfurt": 0.0,
            },
        )
        risk = score_impact(
            atlaspay_graph,
            state,
            origin_ids=["cloud-region-singapore", "cloud-region-mumbai"],
            proximity=1.0,
            max_depth_reached=8,
        )
        assert 0.0 <= risk.score <= 100.0

    def test_as_text_renders_the_breakdown(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        text = score_impact(
            atlaspay_graph, state, origin_ids=["cloud-region-singapore"]
        ).as_text()
        assert "+" in text or "-" in text


class TestConfidence:
    def test_names_its_uncertainties(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0})
        confidence = assess_confidence(
            atlaspay_graph,
            state,
            event=None,
            origin_ids=["supplier-taiwan-hardware"],
            truncated=False,
        )
        assert 0.0 < confidence.score <= 1.0
        assert any("synthetic" in item for item in confidence.uncertainties)
        assert any("operating status" in item for item in confidence.uncertainties)

    def test_truncation_lowers_confidence(self, atlaspay_graph: WorldGraph):
        state = propagate(atlaspay_graph, initial_availability={"cloud-region-singapore": 0.0})
        whole = assess_confidence(
            atlaspay_graph, state, event=None, origin_ids=["cloud-region-singapore"], truncated=False
        )
        cut = assess_confidence(
            atlaspay_graph, state, event=None, origin_ids=["cloud-region-singapore"], truncated=True
        )
        assert cut.score < whole.score
        assert any("truncated" in item for item in cut.uncertainties)


class TestCriticalityWeights:
    def test_all_criticalities_have_weights(self):
        from app.models.core import CRITICALITY_WEIGHTS

        for level in Criticality:
            assert level in CRITICALITY_WEIGHTS

    def test_failure_propagating_types_exclude_serves_and_replicates(self):
        from app.models.core import FAILURE_PROPAGATING_TYPES

        assert DependencyType.SERVES not in FAILURE_PROPAGATING_TYPES
        assert DependencyType.REPLICATES_TO not in FAILURE_PROPAGATING_TYPES


class TestCriticalPathSelection:
    """Which routes are called critical, how many, and in what order.

    Mutation testing found this whole block unprotected. Every one of these survived:
    raising `MAX_CRITICAL_PATHS`, inverting the `>=` that enforces it, turning the
    `customer_facing or CRITICAL` selection rule into `and`, and flipping either `is` in
    that rule. These paths are what the globe animates and what the explanation quotes, so
    a silent change here changes what an operator is told to look at.
    """

    @staticmethod
    def _singapore(graph: WorldGraph):
        return calculate_blast_radius(
            graph,
            origin_ids=["cloud-region-singapore"],
            origin_kind="ENTITY",
            origin_label="probe",
            proximity=1.0,
        )

    def test_the_cap_holds_at_six(self, atlaspay_graph: WorldGraph):
        """Six, written out, not `== MAX_CRITICAL_PATHS`.

        Asserting against the imported constant is self-referential: raising the constant
        raises the expectation with it, and the test passes at any value. It survived
        mutation for exactly that reason. The literal is what makes a change to the cap
        show up as a failing test, which is where the decision belongs.
        """
        result = self._singapore(atlaspay_graph)
        assert len(result.critical_paths) == 6
        assert MAX_CRITICAL_PATHS == 6, "the cap moved; update the expectation deliberately"

    def test_the_cap_actually_binds_on_this_estate(self, atlaspay_graph: WorldGraph):
        """Otherwise the test above would pass without the cap doing anything."""
        result = self._singapore(atlaspay_graph)
        qualifying = {
            record.path.hops[-1].entity_id
            for record in result.direct_impact + result.indirect_impact
            if (record.customer_facing is True or record.criticality is Criticality.CRITICAL)
            and record.path.depth >= 1
        }
        assert len(qualifying) > 6, "the estate must offer more paths than the cap allows"

    def test_every_critical_path_ends_somewhere_that_qualifies(self, atlaspay_graph: WorldGraph):
        """The selection rule itself: customer-facing OR critical, never both required."""
        result = self._singapore(atlaspay_graph)
        assert result.critical_paths
        records = {r.entity_id: r for r in result.direct_impact + result.indirect_impact}
        qualifying = 0
        for path in result.critical_paths:
            record = records[path.hops[-1].entity_id]
            assert record.customer_facing is True or record.criticality is Criticality.CRITICAL
            if record.customer_facing is True and record.criticality is not Criticality.CRITICAL:
                qualifying += 1
        # At least one terminus qualifies on customer-facing alone. Were the rule `and`,
        # that path would vanish — which is what makes this more than a restatement.
        assert qualifying >= 1, "the rule must be a disjunction, not a conjunction"

    def test_no_two_critical_paths_share_a_terminus(self, atlaspay_graph: WorldGraph):
        """Six routes to the same service is one finding, not six."""
        paths = self._singapore(atlaspay_graph).critical_paths
        termini = [p.hops[-1].entity_id for p in paths]
        assert len(set(termini)) == len(termini)

    def test_paths_are_ordered_worst_terminus_first(self, atlaspay_graph: WorldGraph):
        result = self._singapore(atlaspay_graph)
        availabilities = [p.terminal_availability for p in result.critical_paths]
        assert availabilities == sorted(availabilities)

    def test_an_origin_is_never_reported_as_a_path(self, atlaspay_graph: WorldGraph):
        """`path.depth < 1` is what excludes it; nothing asserted that."""
        result = self._singapore(atlaspay_graph)
        for path in result.critical_paths:
            assert path.depth >= 1
            assert path.hops, "a path with no hops is an origin, not a route"


class TestCustomerExposureReporting:
    """Which region gets named as the worst, and what is said when a count is missing."""

    @staticmethod
    def _singapore(graph: WorldGraph):
        return calculate_blast_radius(
            graph,
            origin_ids=["cloud-region-singapore"],
            origin_kind="ENTITY",
            origin_label="probe",
            proximity=1.0,
        )

    def test_exposure_is_ordered_worst_first(self, atlaspay_graph: WorldGraph):
        """`exposure[0]` is only the worst region if the list is actually sorted."""
        exposure = self._singapore(atlaspay_graph).customer_exposure
        assert len(exposure) >= 2, "needs at least two regions to order"
        impacts = [e.traffic_impact for e in exposure]
        assert impacts == sorted(impacts, reverse=True)

    def test_the_explanation_names_the_worst_region_not_another_one(
        self, atlaspay_graph: WorldGraph
    ):
        result = self._singapore(atlaspay_graph)
        worst = max(result.customer_exposure, key=lambda e: e.traffic_impact)
        others = [e.region for e in result.customer_exposure if e.region != worst.region]
        text = " ".join(result.explanations)
        assert f"{worst.region} sees an estimated" in text
        for region in others:
            assert f"{region} sees an estimated" not in text


class TestRiskWeightsArePinned:
    """The numbers that turn a world state into a score.

    Mutation testing found almost every constant in `risk.py` unprotected: the WEIGHTS
    ceilings, the criticality multipliers, the severity multipliers, the SPOF availability
    threshold, and the dependency-depth curve. The score is displayed as `61/100` with a
    signed derivation beside it, so each of these is a number an operator reads and acts on.

    The tables are pinned as literals rather than by importing and comparing to themselves.
    A weight is a product decision; changing one should require changing a test that says
    so, not slip through because the assertion moved with it.
    """

    def test_every_weight_is_the_documented_value(self):
        assert WEIGHTS == {
            "asset_criticality": 30.0,
            "customer_facing": 25.0,
            "single_point_of_failure": 20.0,
            "event_proximity": 15.0,
            "customer_exposure": 20.0,
            "dependency_depth": 10.0,
            "event_severity": 15.0,
            "redundancy_credit": -12.0,
            "freshness_penalty": -8.0,
        }

    def test_no_single_dimension_can_reach_critical_alone(self):
        """The stated design rule above the table, asserted rather than trusted."""
        positives = [v for v in WEIGHTS.values() if v > 0]
        assert max(positives) < 75.0
        # "reaching 75 needs at least three of them"
        assert sum(sorted(positives, reverse=True)[:2]) < 75.0
        assert sum(sorted(positives, reverse=True)[:3]) >= 75.0

    def test_credits_are_negative_and_penalties_reduce_the_score(self):
        assert WEIGHTS["redundancy_credit"] < 0
        assert WEIGHTS["freshness_penalty"] < 0

    @pytest.mark.parametrize(
        ("criticality", "expected"),
        [
            (Criticality.CRITICAL, 30.0),
            (Criticality.HIGH, 21.0),
            (Criticality.MEDIUM, 12.0),
            (Criticality.LOW, 4.5),
            (Criticality.UNKNOWN, 0.0),
        ],
    )
    def test_criticality_multiplier_scales_the_asset_contribution(
        self, criticality: Criticality, expected: float
    ):
        """A fully-lost asset of each criticality earns weight x its multiplier.

        The expected values are written out — 30 x 0.7 = 21 — so a change to the
        multiplier table fails here instead of being absorbed by an assertion that reads
        the table back.
        """
        graph = WorldGraph([entity("a", criticality=criticality, health=HealthState.DOWN)], [])
        state = propagate(graph)
        score = score_impact(graph, state, origin_ids=["a"])
        found = [c for c in score.contributions if c.code == "asset_criticality"]
        if criticality is Criticality.UNKNOWN:
            # Not "scores zero" — scored at all. An undeclared criticality is excluded
            # before the multiplier table is reached (REALITY_PASS_AUDIT B4: an undeclared
            # criticality is not a low one). The `UNKNOWN: 0.0` entry in that table is
            # therefore unreachable, which is why mutating it survives; the exclusion is
            # the real behaviour and this is what pins it.
            assert not found, "an undeclared criticality must not be scored at all"
        else:
            assert found, f"{criticality.value} should contribute to the score"
            assert found[0].points == pytest.approx(expected, abs=0.05)
