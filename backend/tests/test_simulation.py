"""What-if simulation: overlays, non-mutation, comparison and response plans."""

from __future__ import annotations

import pytest

from app.analysis.blast_radius import calculate_blast_radius
from app.analysis.propagation import propagate
from app.analysis.response_plan import generate_response_plan
from app.graph.world_graph import WorldGraph
from app.models.analysis import OverrideKind, SimulationOverride, Urgency
from app.models.core import DataMode, HealthState, Severity
from app.simulation.engine import (
    SimulationError,
    _count,
    _direction,
    _direction_optional,
    _money,
    _percent,
    _risk_rank,
    compare,
    compile_overrides,
    new_scenario,
    override_for_capacity,
    override_for_health,
    touch,
    validate_override,
)


class TestOverrideValidation:
    def test_unknown_entity_is_rejected(self, atlaspay_graph: WorldGraph):
        """A silently ignored override produces a simulation that looks like it ran."""
        with pytest.raises(SimulationError, match="unknown entity"):
            validate_override(atlaspay_graph, override_for_health("ghost", HealthState.DOWN))

    def test_unknown_edge_is_rejected(self, atlaspay_graph: WorldGraph):
        with pytest.raises(SimulationError, match="unknown dependency edge"):
            validate_override(
                atlaspay_graph,
                SimulationOverride(
                    id="o1", kind=OverrideKind.EDGE_DISABLED, target_id="not-an-edge"
                ),
            )

    def test_health_override_needs_a_health_value(self, atlaspay_graph: WorldGraph):
        with pytest.raises(SimulationError, match="requires a health value"):
            validate_override(
                atlaspay_graph,
                SimulationOverride(
                    id="o1", kind=OverrideKind.ENTITY_HEALTH, target_id="payments-api"
                ),
            )

    def test_capacity_is_bounded_by_the_schema(self):
        with pytest.raises(ValueError):
            override_for_capacity("payments-api", 1.7)

    def test_describe_is_human_readable(self):
        assert override_for_health("x", HealthState.DOWN).describe() == "x = DOWN"
        assert "40%" in override_for_capacity("x", 0.4).describe()


class TestNonMutation:
    def test_baseline_world_is_untouched_by_a_simulation(self, atlaspay_graph: WorldGraph):
        """The single most important guarantee in this subsystem."""
        before = {e.id: e.health for e in atlaspay_graph.entities}
        edges_before = len(atlaspay_graph.edges)

        scenario = new_scenario("destructive")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        scenario.overrides.append(override_for_capacity("payments-k8s-mumbai", 0.1))
        compare(atlaspay_graph, scenario)

        assert {e.id: e.health for e in atlaspay_graph.entities} == before
        assert len(atlaspay_graph.edges) == edges_before

    def test_baseline_column_matches_an_untouched_solve(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("x")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        from app.analysis.business_impact import snapshot_metrics

        assert comparison.baseline.availability == snapshot_metrics(
            atlaspay_graph, propagate(atlaspay_graph)
        ).availability

    def test_scenario_is_always_labelled_simulated(self):
        assert new_scenario("x").mode is DataMode.SIMULATED


class TestCompileOverrides:
    def test_later_override_on_the_same_target_wins(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("revised")
        scenario.overrides.append(override_for_health("payments-api", HealthState.DOWN))
        scenario.overrides.append(override_for_health("payments-api", HealthState.DEGRADED))
        pinned, _, _ = compile_overrides(atlaspay_graph, scenario)
        assert pinned["payments-api"] == pytest.approx(0.7)

    def test_all_three_override_kinds_compile(self, atlaspay_graph: WorldGraph):
        edge_id = atlaspay_graph.edges[0].id
        scenario = new_scenario("mixed")
        scenario.overrides.extend(
            [
                override_for_health("payments-api", HealthState.DOWN),
                override_for_capacity("payments-k8s-mumbai", 0.5),
                SimulationOverride(
                    id="o3", kind=OverrideKind.EDGE_DISABLED, target_id=edge_id
                ),
            ]
        )
        pinned, capacities, disabled = compile_overrides(atlaspay_graph, scenario)
        assert pinned == {"payments-api": 0.0}
        assert capacities == {"payments-k8s-mumbai": 0.5}
        assert disabled == frozenset({edge_id})


class TestComparison:
    def test_empty_scenario_shows_no_change(self, atlaspay_graph: WorldGraph):
        comparison = compare(atlaspay_graph, new_scenario("empty"))
        assert comparison.blast_radius is None
        assert comparison.newly_impacted == []
        assert all(row.direction == "same" for row in comparison.deltas)

    def test_supplier_loss_constrains_capacity_more_than_availability(
        self, atlaspay_graph: WorldGraph
    ):
        """The hero scenario's headline: traffic keeps flowing, capacity does not."""
        scenario = new_scenario("Taiwan supplier unavailable")
        scenario.overrides.append(override_for_health("supplier-taiwan-hardware", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)

        assert comparison.simulated.availability > 0.85
        assert comparison.simulated.regional_capacity["APAC"] < 0.8
        assert comparison.simulated.material_risk is Severity.HIGH
        assert comparison.baseline.material_risk is Severity.LOW

    def test_adding_a_second_failure_escalates(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("escalating")
        scenario.overrides.append(override_for_health("supplier-taiwan-hardware", HealthState.DOWN))
        first = compare(atlaspay_graph, scenario)
        scenario.overrides.append(override_for_health("payments-k8s-singapore", HealthState.DOWN))
        second = compare(atlaspay_graph, scenario)

        assert second.simulated.availability < first.simulated.availability
        assert len(second.newly_impacted) >= len(first.newly_impacted)
        assert second.simulated.material_risk is Severity.CRITICAL

    def test_deltas_are_directional_not_assumed(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("outage")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        by_key = {row.key: row for row in comparison.deltas}
        assert by_key["availability"].direction == "worse"
        assert by_key["customers_affected"].direction == "worse"
        assert by_key["material_risk"].direction == "worse"

    def test_removing_an_override_restores_the_baseline(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("reset")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        compare(atlaspay_graph, scenario)
        scenario.overrides = []
        restored = compare(atlaspay_graph, scenario)
        assert restored.simulated.availability == restored.baseline.availability

    def test_cascade_paths_are_multi_hop(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("cascade")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        assert comparison.cascade_paths
        assert all(path.depth >= 2 for path in comparison.cascade_paths)

    def test_blast_radius_is_stamped_simulated(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("labelled")
        scenario.overrides.append(override_for_health("payments-k8s-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        assert comparison.blast_radius is not None
        assert comparison.blast_radius.mode is DataMode.SIMULATED

    def test_comparison_meets_the_performance_budget(self, atlaspay_graph: WorldGraph):
        """README budget: simulation recalculation < 1 s for the demo graph."""
        scenario = new_scenario("perf")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        scenario.overrides.append(override_for_health("supplier-taiwan-hardware", HealthState.DOWN))
        assert compare(atlaspay_graph, scenario).duration_ms < 1000


class TestBlastRadius:
    def test_requires_a_known_origin(self, atlaspay_graph: WorldGraph):
        with pytest.raises(KeyError, match="no known origin"):
            calculate_blast_radius(atlaspay_graph, origin_ids=["ghost"])

    def test_direct_and_indirect_are_split_by_depth(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(
            atlaspay_graph, origin_ids=["cloud-region-singapore"]
        )
        assert all(record.depth <= 1 for record in result.direct_impact)
        assert all(record.depth >= 2 for record in result.indirect_impact)

    def test_every_impacted_entity_carries_a_path(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        for record in [*result.direct_impact, *result.indirect_impact]:
            assert record.path.hops, "an impact with no explanation path is not shippable"
            assert record.path.hops[-1].entity_id == record.entity_id

    def test_critical_paths_end_at_things_that_matter(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        assert result.critical_paths
        for path in result.critical_paths:
            assert path.depth >= 1

    def test_explanations_are_always_present(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["payments-api"])
        assert result.explanations
        assert any("MODELLED ESTIMATE" in line for line in result.explanations)

    def test_meets_the_performance_budget(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        assert result.duration_ms < 1000


class TestResponsePlan:
    def test_every_action_has_a_rationale(self, atlaspay_graph: WorldGraph):
        """Schema-enforced, and asserted so the intent is visible in the suite."""
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        plan = generate_response_plan(atlaspay_graph, result)
        assert plan.actions
        for action in plan.actions:
            assert action.rationale.strip()

    def test_nothing_is_ever_executed(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        plan = generate_response_plan(atlaspay_graph, result)
        for action in plan.actions:
            assert action.executed is False
        assert "executes nothing" in plan.summary

    def test_region_outage_recommends_failover_and_replica_promotion(
        self, atlaspay_graph: WorldGraph
    ):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        plan = generate_response_plan(atlaspay_graph, result)
        text = " ".join(action.action for action in plan.actions)
        assert "Shift traffic" in text
        assert "Promote" in text
        assert "Notify" in text

    def test_supplier_loss_recommends_a_continuity_review(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(
            atlaspay_graph, origin_ids=["supplier-taiwan-hardware"]
        )
        plan = generate_response_plan(atlaspay_graph, result)
        escalations = [a for a in plan.actions if "continuity review" in a.action]
        assert escalations
        assert escalations[0].urgency is Urgency.MONITOR
        assert "second source" in escalations[0].rationale

    def test_no_impact_yields_a_monitor_action_rather_than_nothing(
        self, atlaspay_graph: WorldGraph
    ):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-tokyo"])
        plan = generate_response_plan(atlaspay_graph, result)
        assert plan.actions
        assert plan.actions[0].urgency is Urgency.MONITOR

    def test_assumptions_name_the_synthetic_data(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        plan = generate_response_plan(atlaspay_graph, result)
        assert any("synthetic" in item.lower() for item in plan.assumptions)

    def test_plan_is_deterministic_for_the_same_analysis(self, atlaspay_graph: WorldGraph):
        result = calculate_blast_radius(atlaspay_graph, origin_ids=["cloud-region-singapore"])
        first = generate_response_plan(atlaspay_graph, result)
        second = generate_response_plan(atlaspay_graph, result)
        assert [a.action for a in first.actions] == [a.action for a in second.actions]


class TestComparisonRowSemantics:
    """The compare table's keys, labels, direction arrows and number formatting.

    Mutation testing found these unprotected. `higher_is_better=False` could be flipped on
    the customers and revenue rows and nothing failed — which would render a simulation
    that puts 41,500 more customers at risk as an improvement. The row keys and labels are
    the UI's contract, and the money formatter turns a modelled figure into the string an
    executive reads.
    """

    @staticmethod
    def _cascade(graph: WorldGraph):
        scenario = new_scenario("probe")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        return compare(graph, scenario)

    def test_the_rows_are_these_keys_in_this_order(self, atlaspay_graph: WorldGraph):
        """A renamed or reordered key silently breaks the panel that reads them."""
        assert [d.key for d in self._cascade(atlaspay_graph).deltas] == [
            "availability",
            "infrastructure_availability",
            "capacity.APAC",
            "critical_services",
            "customer_regions",
            "customers_affected",
            "revenue_at_risk",
            "material_risk",
        ]

    def test_every_row_carries_the_label_the_operator_sees(self, atlaspay_graph: WorldGraph):
        labels = {d.key: d.label for d in self._cascade(atlaspay_graph).deltas}
        assert labels["customers_affected"] == "Customers affected"
        assert labels["revenue_at_risk"] == "Revenue at risk / hour"
        assert labels["customer_regions"] == "Customer regions impacted"

    def test_more_customers_and_more_revenue_at_risk_read_as_worse(self, atlaspay_graph: WorldGraph):
        """`higher_is_better=False` on these rows. Flipped, a cascade would look like a win."""
        by_key = {d.key: d for d in self._cascade(atlaspay_graph).deltas}
        for key in ("customers_affected", "revenue_at_risk", "critical_services", "customer_regions"):
            assert by_key[key].direction == "worse", f"{key} moved the wrong way"
        # And availability falling is worse too, on the opposite polarity.
        assert by_key["availability"].direction == "worse"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "UNKNOWN"),
            (0.0, "$0"),
            (999.0, "$999"),
            (1_000.0, "$1K"),
            (2_260_000.0, "$2.26M"),
            (999_999.0, "$1000K"),
        ],
    )
    def test_money_formatting_including_its_boundaries(self, value, expected: str):
        """UNKNOWN is not $0: an undeclared revenue must never render as a number."""
        assert _money(value) == expected

    def test_risk_rank_orders_the_bands(self):
        ranks = [_risk_rank(Severity(s)) for s in ("INFO", "LOW", "MODERATE", "HIGH", "CRITICAL")]
        assert ranks == sorted(ranks), "a reordered band would invert the risk arrow"
        assert len(set(ranks)) == 5


class TestAnUnknownIsNeverRenderedAsAWin:
    """Reality Pass §13 and §24, in the two functions that decide a table cell.

    `_direction_optional` returning "better" for a missing measurement would paint an
    undeclared figure green, and `_percent`/`_count` substituting a number for `None`
    would put a fabricated one next to it. Mutation testing found every branch here
    unprotected.
    """

    def test_a_missing_side_reads_as_same_not_better(self):
        assert _direction_optional(None, 5, higher_is_better=False) == "same"
        assert _direction_optional(5, None, higher_is_better=False) == "same"
        assert _direction_optional(None, None, higher_is_better=False) == "same"
        # Both polarities: an unknown is uncomparable regardless of which way is good.
        assert _direction_optional(None, 5, higher_is_better=True) == "same"

    def test_two_known_sides_are_compared_normally(self):
        assert _direction_optional(10, 4, higher_is_better=False) == "better"
        assert _direction_optional(4, 10, higher_is_better=False) == "worse"
        assert _direction_optional(4, 10, higher_is_better=True) == "better"

    def test_zero_is_a_measurement_and_none_is_not(self):
        """The distinction the whole tri-state design exists for."""
        assert _direction_optional(0, 5, higher_is_better=False) == "worse"
        assert _percent(0.0) == "0.00%"
        assert _percent(None) == "UNKNOWN"
        assert _count(0) == "0"
        assert _count(None) == "UNKNOWN"

    def test_percent_keeps_two_decimals_because_availability_lives_there(self):
        """99.9% and 99.99% are 8 hours of downtime a year apart."""
        assert _percent(0.9999) == "99.99%"
        assert _percent(0.999) == "99.90%"
        assert _percent(1.0) == "100.00%"

    def test_counts_are_thousands_separated(self):
        assert _count(41_500) == "41,500"


class TestDirectionIsNotFooledByFloatNoise:
    def test_an_identical_pair_reads_as_same(self):
        assert _direction(0.9999, 0.9999, higher_is_better=True) == "same"

    def test_a_difference_below_the_epsilon_reads_as_same(self):
        """Solver noise must not render as a change the operator can act on."""
        assert _direction(0.9999, 0.9999 + 1e-12, higher_is_better=True) == "same"

    def test_a_real_difference_still_moves(self):
        assert _direction(0.99, 0.98, higher_is_better=True) == "worse"
        assert _direction(0.98, 0.99, higher_is_better=True) == "better"
        assert _direction(0.98, 0.99, higher_is_better=False) == "worse"


class TestOverrideBuildersAndTouch:
    def test_a_built_override_is_identifiable_and_unique(self, atlaspay_graph: WorldGraph):
        first = override_for_health("payments-api", HealthState.DOWN)
        second = override_for_health("payments-api", HealthState.DOWN)
        assert first.id.startswith("ovr-") and second.id.startswith("ovr-")
        assert first.id != second.id, "two overrides sharing an id would overwrite each other"
        assert first.kind is OverrideKind.ENTITY_HEALTH
        assert first.health is HealthState.DOWN
        assert first.capacity is None

    def test_a_capacity_override_carries_capacity_and_no_health(self):
        override = override_for_capacity("payments-api", 0.4, note="half a region")
        assert override.id.startswith("ovr-")
        assert override.kind is OverrideKind.ENTITY_CAPACITY
        assert override.capacity == 0.4
        assert override.health is None
        assert override.note == "half a region"

    def test_touch_bumps_updated_at_and_changes_nothing_else(self):
        scenario = new_scenario("probe")
        bumped = touch(scenario)
        # Strictly later, not merely "not earlier": `>=` would also hold if the field were
        # never written, which is the failure this test exists to catch.
        assert bumped.updated_at > scenario.updated_at
        assert bumped.created_at == scenario.created_at
        assert bumped.id == scenario.id
        assert bumped.overrides == scenario.overrides
        # A copy, not a mutation: the caller may still be holding the original.
        assert bumped is not scenario
        assert scenario.updated_at < bumped.updated_at


class TestCascadePathsCarryTheirEdgeTypes:
    """A path is evidence. Hop availabilities and edge types are what make it readable."""

    def test_the_first_hop_has_no_incoming_edge_and_the_rest_do(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("probe")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        multi_hop = [p for p in comparison.cascade_paths if len(p.hops) > 1]
        assert multi_hop, "a region outage must cascade through at least one edge"
        for path in multi_hop:
            assert path.hops[0].edge_type is None
            assert all(hop.edge_type is not None for hop in path.hops[1:])

    def test_hop_availabilities_are_rounded_but_not_flattened(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("probe")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        values = [hop.availability for path in comparison.cascade_paths for hop in path.hops]
        assert values
        assert all(0.0 <= v <= 1.0 for v in values)
        assert all(round(v, 4) == v for v in values), "4dp, so the UI never prints 0.9999999"
        # The cascade must actually depress something — all-1.0 would mean the override
        # never propagated and the path is decoration.
        assert any(v < 1.0 for v in values)

    def test_a_paths_terminal_availability_is_its_last_hop(self, atlaspay_graph: WorldGraph):
        scenario = new_scenario("probe")
        scenario.overrides.append(override_for_health("cloud-region-singapore", HealthState.DOWN))
        comparison = compare(atlaspay_graph, scenario)
        for path in comparison.cascade_paths:
            assert path.terminal_availability == path.hops[-1].availability
