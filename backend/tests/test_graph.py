"""Graph traversal, cycles, bounds and structural queries."""

from __future__ import annotations

import pytest

from app.graph.world_graph import WorldGraph
from app.models.core import (
    Criticality,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    HealthState,
    WorldEntity,
)

SOURCE = DataSourceInfo(source_id="test", source_name="test", mode=DataMode.SYNTHETIC)


def entity(entity_id: str, **kwargs) -> WorldEntity:
    # HEALTHY by default. The model's own default is UNKNOWN (0.9 availability, an
    # honest "we have no telemetry"), which is right for real data but makes every
    # propagation assertion carry an unrelated 0.9 factor.
    return WorldEntity(
        id=entity_id,
        type=kwargs.pop("type", EntityType.MICROSERVICE),
        name=kwargs.pop("name", entity_id),
        health=kwargs.pop("health", HealthState.HEALTHY),
        source=SOURCE,
        **kwargs,
    )


def edge(source: str, target: str, edge_type=DependencyType.DEPENDS_ON, **kwargs) -> DependencyEdge:
    # The id includes the type: the graph keys edges by id, so two relationships between
    # the same pair need two ids or the second silently replaces the first.
    return DependencyEdge(
        id=f"{source}--{edge_type.value}->{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=edge_type,
        **kwargs,
    )


class TestConstruction:
    def test_rejects_edge_to_unknown_entity(self):
        """A dangling edge would make the graph claim reachability into nothing."""
        graph = WorldGraph([entity("a")])
        with pytest.raises(KeyError, match="unknown target entity"):
            graph.add_edge(edge("a", "ghost"))

    def test_rejects_edge_from_unknown_entity(self):
        graph = WorldGraph([entity("a")])
        with pytest.raises(KeyError, match="unknown source entity"):
            graph.add_edge(edge("ghost", "a"))

    def test_rejects_self_edge(self):
        with pytest.raises(ValueError, match="cannot depend on itself"):
            edge("a", "a")

    def test_removing_entity_removes_its_edges(self):
        graph = WorldGraph([entity("a"), entity("b")], [edge("a", "b")])
        graph.remove_entity("b")
        assert graph.edges == []
        assert graph.dependencies_of("a") == []

    def test_supports_multiple_edges_between_same_pair(self):
        """A cluster can be HOSTED_IN a region *and* CONNECTS_TO it."""
        graph = WorldGraph(
            [entity("a"), entity("b")],
            [
                edge("a", "b", DependencyType.HOSTED_IN),
                edge("a", "b", DependencyType.CONNECTS_TO),
            ],
        )
        assert len(graph.dependencies_of("a")) == 2


class TestTraversal:
    def test_dependents_walk_goes_against_the_arrows(self):
        """c DEPENDS_ON b DEPENDS_ON a → losing a reaches b then c."""
        graph = WorldGraph(
            [entity("a"), entity("b"), entity("c")],
            [edge("b", "a"), edge("c", "b")],
        )
        result = graph.traverse_dependents(["a"])
        by_id = result.by_id()
        assert by_id["b"].depth == 1
        assert by_id["c"].depth == 2
        assert by_id["c"].path == ["a", "b", "c"]

    def test_dependencies_walk_follows_the_arrows(self):
        graph = WorldGraph(
            [entity("a"), entity("b"), entity("c")],
            [edge("c", "b"), edge("b", "a")],
        )
        assert set(graph.traverse_dependencies(["c"]).entity_ids()) == {"a", "b", "c"}

    def test_serves_edges_reach_customers_forwards(self):
        """service SERVES customers — the customer suffers when the service does."""
        graph = WorldGraph(
            [entity("svc"), entity("cust", type=EntityType.CUSTOMER_REGION)],
            [edge("svc", "cust", DependencyType.SERVES)],
        )
        assert "cust" in graph.traverse_dependents(["svc"]).entity_ids()

    def test_replicates_to_does_not_propagate_backwards(self):
        """A replica failing must not take its primary down."""
        graph = WorldGraph(
            [entity("primary"), entity("replica")],
            [edge("primary", "replica", DependencyType.REPLICATES_TO)],
        )
        assert graph.traverse_dependents(["replica"]).entity_ids() == ["replica"]

    def test_cycle_is_broken_and_reported(self):
        graph = WorldGraph(
            [entity("a"), entity("b"), entity("c")],
            [edge("b", "a"), edge("c", "b"), edge("a", "c")],
        )
        result = graph.traverse_dependents(["a"])
        assert len(result.entity_ids()) == 3  # each node visited exactly once
        assert result.cycles_broken, "the cycle should be recorded, not silently skipped"

    def test_depth_limit_truncates_and_says_so(self):
        entities = [entity(f"n{i}") for i in range(6)]
        edges = [edge(f"n{i + 1}", f"n{i}") for i in range(5)]
        result = WorldGraph(entities, edges).traverse_dependents(["n0"], max_depth=2)
        assert result.truncated is True
        assert "max depth 2" in (result.truncation_reason or "")
        assert max(step.depth for step in result.steps) == 2

    def test_complete_traversal_is_not_marked_truncated(self):
        """Hitting max_depth with nothing left to visit is a complete answer."""
        graph = WorldGraph([entity("a"), entity("b")], [edge("b", "a")])
        result = graph.traverse_dependents(["a"], max_depth=1)
        assert result.truncated is False

    def test_node_budget_truncates_and_says_so(self):
        entities = [entity("hub")] + [entity(f"leaf{i}") for i in range(20)]
        edges = [edge(f"leaf{i}", "hub") for i in range(20)]
        result = WorldGraph(entities, edges).traverse_dependents(["hub"], node_budget=5)
        assert result.truncated is True
        assert "node budget" in (result.truncation_reason or "")

    def test_unknown_origin_yields_empty_traversal(self):
        assert WorldGraph([entity("a")]).traverse_dependents(["ghost"]).steps == []


class TestQueries:
    def test_shortest_dependency_path(self, atlaspay_graph: WorldGraph):
        path = atlaspay_graph.shortest_dependency_path("checkout-platform", "postgres-singapore")
        assert path is not None
        assert path[0] == "checkout-platform"
        assert path[-1] == "postgres-singapore"

    def test_no_path_returns_none(self, atlaspay_graph: WorldGraph):
        assert atlaspay_graph.shortest_dependency_path("customers-apac", "warehouse-virginia") is None

    def test_impact_path_from_supplier_reaches_customers(self, atlaspay_graph: WorldGraph):
        path = atlaspay_graph.impact_path("supplier-taiwan-hardware", "customers-apac")
        assert path is not None
        assert "payments-api" in path

    def test_hosted_entities(self, atlaspay_graph: WorldGraph):
        hosted = {e.id for e in atlaspay_graph.hosted_entities("payments-k8s-singapore")}
        assert {"payments-api", "admin-api", "internal-auth"} <= hosted

    def test_single_points_of_failure_finds_the_sole_source_supplier(
        self, atlaspay_graph: WorldGraph
    ):
        spofs = dict(atlaspay_graph.single_points_of_failure(min_dependents=3))
        assert "supplier-taiwan-hardware" in spofs

    def test_clone_is_independent(self, atlaspay_graph: WorldGraph):
        clone = atlaspay_graph.clone()
        clone.require_entity("payments-api").name = "mutated"
        assert atlaspay_graph.require_entity("payments-api").name == "payments-api"


class TestAtlasPayFixture:
    def test_every_edge_resolves(self, atlaspay_graph: WorldGraph):
        """Construction would have raised, but assert it so a regression is named."""
        for e in atlaspay_graph.edges:
            assert e.source_entity_id in atlaspay_graph
            assert e.target_entity_id in atlaspay_graph

    def test_fixture_is_deterministic(self):
        from app.fixtures.atlaspay import build_atlaspay

        first_entities, first_edges = build_atlaspay()
        second_entities, second_edges = build_atlaspay()
        assert [e.id for e in first_entities] == [e.id for e in second_entities]
        assert [e.model_dump_json() for e in first_edges] == [
            e.model_dump_json() for e in second_edges
        ]

    def test_all_entities_are_labelled_synthetic(self, atlaspay_graph: WorldGraph):
        """Nothing in the demo estate may present as observed data."""
        for e in atlaspay_graph.entities:
            assert e.source.mode is DataMode.SYNTHETIC

    def test_customer_traffic_shares_sum_to_one(self, atlaspay_graph: WorldGraph):
        total = sum(
            e.business.traffic_share
            for e in atlaspay_graph.entities_of_type(EntityType.CUSTOMER_REGION)
        )
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_estate_has_no_unintended_cycles(self, atlaspay_graph: WorldGraph):
        assert atlaspay_graph.cycles() == []

    def test_estate_is_one_connected_component(self, atlaspay_graph: WorldGraph):
        """An island of infrastructure is almost always a missing edge."""
        assert len(atlaspay_graph.connected_components()) == 1

    def test_critical_entities_exist(self, atlaspay_graph: WorldGraph):
        critical = [
            e for e in atlaspay_graph.entities if e.criticality is Criticality.CRITICAL
        ]
        assert len(critical) >= 5
