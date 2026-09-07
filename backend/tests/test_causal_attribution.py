"""A blast radius must report what a failure *caused*, not what is merely degraded.

The defect: two entities with no edge between them. Failing one put the other in its
blast radius at depth 1, with `availability_delta` of exactly 0.0 — the evidence that it
was untouched, computed and then ignored one line from where it would have excluded it.

The cause is the same absolute-threshold rule the Reality Pass already replaced in the
simulation engine and left in place here, so the two engines disagreed about what a
failure had done. On an estate that declares its health they nearly agree, because
everything starts at 1.0. On an imported estate every entity starts at UNKNOWN (0.9) and
inherits less through its edges, so everything is already below the threshold and the
blast radius returns the whole graph regardless of what failed.
"""

from __future__ import annotations

import pytest

from app.analysis.blast_radius import calculate_blast_radius
from app.analysis.propagation import MATERIAL_DEGRADATION, propagate
from app.graph.world_graph import WorldGraph
from app.models.core import (
    BusinessProfile,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    HealthState,
    WorldEntity,
)

SRC = DataSourceInfo(source_id="import", source_name="Imported", mode=DataMode.LIVE)


def node(entity_id: str, *, health: HealthState = HealthState.UNKNOWN) -> WorldEntity:
    """An entity as a cloud import produces one: no declared health."""
    return WorldEntity(
        id=entity_id,
        type=EntityType.APPLICATION,
        name=entity_id,
        source=SRC,
        health=health,
        business=BusinessProfile(region="westeurope"),
    )


def edge(source: str, target: str) -> DependencyEdge:
    return DependencyEdge(
        id=f"{source}--DEPENDS_ON--{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=DependencyType.DEPENDS_ON,
        criticality=1.0,
    )


def reached(result) -> set[str]:
    return {row.entity_id for row in result.direct_impact + result.indirect_impact}


class TestAttribution:
    def test_an_unconnected_entity_is_never_in_the_blast_radius(self):
        """The headline defect, reproduced exactly."""
        graph = WorldGraph([node("alpha"), node("beta")], [])
        assert len(graph.edges) == 0

        result = calculate_blast_radius(graph, origin_ids=["alpha"])

        assert "beta" not in reached(result)
        assert reached(result) == {"alpha"}

    def test_everything_is_already_below_the_threshold(self):
        """The precondition that made the absolute rule wrong."""
        from app.analysis.propagation import IMPACT_THRESHOLD

        state = propagate(WorldGraph([node("alpha"), node("beta")], []))
        assert all(v < IMPACT_THRESHOLD for v in state.availability.values())

    def test_a_connected_entity_is_still_reported(self):
        """The filter must not achieve correctness by reporting nothing."""
        graph = WorldGraph(
            [node("web"), node("db"), node("unrelated")], [edge("web", "db")]
        )
        result = calculate_blast_radius(graph, origin_ids=["db"])

        assert "web" in reached(result), "a real dependent must survive the filter"
        assert "unrelated" not in reached(result)

    def test_a_transitive_dependent_is_still_reported(self):
        graph = WorldGraph(
            [node("a"), node("b"), node("c")], [edge("a", "b"), edge("b", "c")]
        )
        result = calculate_blast_radius(graph, origin_ids=["c"])
        assert {"a", "b"} <= reached(result)

    def test_every_reported_entity_actually_lost_availability(self):
        graph = WorldGraph(
            [node("a"), node("b"), node("c"), node("spectator")],
            [edge("a", "b"), edge("b", "c")],
        )
        result = calculate_blast_radius(graph, origin_ids=["c"])
        for row in result.direct_impact + result.indirect_impact:
            if row.depth == 0:
                continue  # the origin, which the operator named
            assert row.availability_delta >= MATERIAL_DEGRADATION, row.entity_id

    def test_a_named_origin_survives_even_if_already_down(self):
        """Naming an origin is the operator asserting it failed."""
        graph = WorldGraph([node("dead", health=HealthState.DOWN), node("other")], [])
        result = calculate_blast_radius(
            graph, origin_ids=["dead"], initial_availability={"dead": 0.0}
        )
        assert "dead" in reached(result)
        assert "other" not in reached(result)

    def test_the_headline_count_matches_the_list_beneath_it(self):
        """A count of 10 above a list of 1 is the same lie in a smaller font."""
        graph = WorldGraph(
            [node("a"), node("b"), node("c"), node("spectator")],
            [edge("a", "b"), edge("b", "c")],
        )
        result = calculate_blast_radius(graph, origin_ids=["c"])
        assert result.business_impact.impacted_entity_count == len(reached(result))


class TestEnginesAgree:
    """Blast radius and simulation must not disagree about what a failure did."""

    @staticmethod
    def _graph() -> WorldGraph:
        return WorldGraph(
            [node("a"), node("b"), node("c"), node("spectator")],
            [edge("a", "b"), edge("b", "c")],
        )

    def test_the_same_failure_attributes_the_same_entities(self):
        from app.models.analysis import OverrideKind, SimulationOverride
        from app.simulation.engine import compare, new_scenario

        graph = self._graph()
        blast = calculate_blast_radius(
            graph, origin_ids=["c"], initial_availability={"c": 0.0}
        )

        scenario = new_scenario("same failure")
        scenario.overrides.append(
            SimulationOverride(
                id="o1",
                kind=OverrideKind.ENTITY_HEALTH,
                target_id="c",
                health=HealthState.DOWN,
            )
        )
        simulated = {row.entity_id for row in compare(graph, scenario).newly_impacted}

        assert reached(blast) == simulated

    def test_both_engines_share_one_threshold(self):
        """They drifted apart once; a single constant is what stops it recurring."""
        from app.simulation import engine

        assert engine.MATERIAL_DEGRADATION is MATERIAL_DEGRADATION


class TestAtlasPayUnchanged:
    """The estate that declares everything must be unaffected by all of this."""

    def test_the_hero_blast_radius_is_identical(self, atlaspay_graph: WorldGraph):
        from app.adapters.fixtures import taiwan_earthquake
        from app.analysis.correlation import correlate_event
        from app.fixtures.atlaspay import REPLAY_EVALUATED_AT

        event = taiwan_earthquake()
        pinned, _matches, proximity = correlate_event(atlaspay_graph, event)
        result = calculate_blast_radius(
            atlaspay_graph,
            origin_ids=sorted(pinned),
            origin_kind="EVENT",
            initial_availability=pinned,
            event=event,
            proximity=proximity,
            now=REPLAY_EVALUATED_AT,
        )
        assert result.risk.score == pytest.approx(68.7, abs=0.05)
        assert len(result.direct_impact) == 5
        assert len(result.indirect_impact) == 13

    def test_a_declared_estate_attributes_the_same_set_either_way(
        self, atlaspay_graph: WorldGraph
    ):
        """Where health is declared, causal and absolute agree — which is why the bug hid."""
        state = propagate(
            atlaspay_graph, initial_availability={"supplier-taiwan-hardware": 0.0}
        )
        assert set(state.caused_ids()) == set(state.impacted_ids())
