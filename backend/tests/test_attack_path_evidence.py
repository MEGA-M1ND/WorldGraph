"""An attack path must carry the evidence for each of its hops.

Two defects, opposite in direction.

**False positives.** Any ``DEPENDS_ON`` edge was followed backwards as a trust
relationship. That is real for an authentication service — owning it does get you into
what authenticates against it — and false for a database. An operational dependency edge
cannot tell the two apart, so two applications that merely share a Postgres produced a
reported attack path between them.

**A false negative, which is worse.** ``attack_paths`` required entry points to be
internet-facing regardless of the origin asked about, so *any* non-internet foothold
returned an empty list. "This host is owned, where can they go" — the question a responder
actually asks — was answered with silent reassurance.

The fix keeps the traversal and labels it. Dropping the trust rule would hide genuine
identity pivots; presenting it as fact manufactures them. Establishing it properly needs
edges inventory cannot supply — who may assume which role, who accepts whose tokens, what
network policy permits — and until a source for those exists, an inferred hop is labelled
rather than believed.
"""

from __future__ import annotations

from app.analysis.correlation import (
    MOVEMENT_EGRESS,
    MOVEMENT_INFERRED_TRUST,
    attack_paths,
)
from app.graph.world_graph import WorldGraph
from app.models.core import (
    BusinessProfile,
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    ExposureProfile,
    WorldEntity,
)

SRC = DataSourceInfo(source_id="s", source_name="s", mode=DataMode.LIVE)


def ent(entity_id: str, *, kind=EntityType.APPLICATION, internet=False) -> WorldEntity:
    return WorldEntity(
        id=entity_id,
        type=kind,
        name=entity_id,
        source=SRC,
        business=BusinessProfile(region="r"),
        exposure=ExposureProfile(internet_facing=internet, network_zone="z"),
    )


def dep(source: str, target: str, kind=DependencyType.DEPENDS_ON) -> DependencyEdge:
    return DependencyEdge(
        id=f"{source}-{kind.value}-{target}",
        source_entity_id=source,
        target_entity_id=target,
        type=kind,
    )


def shared_database_graph() -> WorldGraph:
    """Two unrelated apps whose only commonality is a database."""
    return WorldGraph(
        [
            ent("internet", kind=EntityType.NETWORK_NODE),
            ent("app-a", internet=True),
            ent("app-b"),
            ent("shared-db", kind=EntityType.DATABASE),
        ],
        [
            dep("app-a", "internet", DependencyType.CONNECTS_TO),
            dep("app-a", "shared-db"),
            dep("app-b", "shared-db"),
        ],
    )


# ======================================================================================
# The false negative
# ======================================================================================


class TestNonInternetFoothold:
    def test_a_foothold_reaches_what_it_connects_to(self):
        """The reproduction: this returned an empty list."""
        graph = WorldGraph(
            [ent("foothold"), ent("target")],
            [dep("foothold", "target", DependencyType.CONNECTS_TO)],
        )
        paths = attack_paths(graph, from_entity_id="foothold", to_entity_ids=["target"])

        assert paths, "a compromised host must be able to answer 'where can they go'"
        assert paths[0].nodes == ["foothold", "target"]

    def test_a_foothold_need_not_be_internet_facing(self):
        """The entry filter belongs to the internet origin, not to every origin."""
        graph = WorldGraph(
            [ent("internal-box", internet=False), ent("crown-jewels")],
            [dep("internal-box", "crown-jewels", DependencyType.CONNECTS_TO)],
        )
        assert attack_paths(graph, from_entity_id="internal-box")

    def test_a_foothold_path_starts_at_the_foothold(self):
        graph = WorldGraph(
            [ent("a"), ent("b"), ent("c")],
            [
                dep("a", "b", DependencyType.CONNECTS_TO),
                dep("b", "c", DependencyType.CONNECTS_TO),
            ],
        )
        paths = attack_paths(graph, from_entity_id="a", to_entity_ids=["c"])
        assert paths[0].nodes[0] == "a"
        assert "internet" not in paths[0].nodes

    def test_an_unknown_origin_still_yields_nothing(self):
        graph = WorldGraph([ent("a")], [])
        assert attack_paths(graph, from_entity_id="nowhere") == []


# ======================================================================================
# The false positive, labelled rather than hidden
# ======================================================================================


class TestInferredTrustIsLabelled:
    def test_a_shared_database_still_produces_a_path(self):
        """Kept, because for an auth service the same shape is a real pivot."""
        paths = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["app-b"]
        )
        assert [p.nodes for p in paths] == [["internet", "app-a", "shared-db", "app-b"]]

    def test_but_it_is_marked_inferred(self):
        path = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["app-b"]
        )[0]
        assert path.relies_on_inference is True
        assert path.basis == "INFERRED"

    def test_the_inferred_hop_names_what_is_missing(self):
        path = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["app-b"]
        )[0]
        inferred = [hop for hop in path.hops if hop.is_inferred]
        assert len(inferred) == 1
        assert inferred[0].from_entity_id == "shared-db"
        assert inferred[0].to_entity_id == "app-b"
        assert "trust relationship" in inferred[0].evidence

    def test_confidence_is_the_weakest_hop_not_the_average(self):
        """Averaging lets four solid hops disguise one invented one."""
        path = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["app-b"]
        )[0]
        assert path.confidence == min(hop.confidence for hop in path.hops)
        assert path.confidence < 0.5

    def test_an_egress_only_path_is_established(self):
        path = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["shared-db"]
        )[0]
        assert path.relies_on_inference is False
        assert path.basis == "ESTABLISHED"
        assert all(hop.movement == MOVEMENT_EGRESS for hop in path.hops)

    def test_inference_can_be_excluded_entirely(self):
        """A caller who wants only proved routes can have them."""
        graph = shared_database_graph()
        assert attack_paths(graph, to_entity_ids=["app-b"], include_inferred=True)
        assert attack_paths(graph, to_entity_ids=["app-b"], include_inferred=False) == []

    def test_established_paths_are_listed_before_inferred_ones(self):
        graph = shared_database_graph()
        paths = attack_paths(graph, from_entity_id="internet")
        bases = [p.relies_on_inference for p in paths]
        assert bases == sorted(bases), "established routes must come first"

    def test_movement_kinds_are_named(self):
        path = attack_paths(
            shared_database_graph(), from_entity_id="internet", to_entity_ids=["app-b"]
        )[0]
        movements = {hop.movement for hop in path.hops}
        assert movements <= {MOVEMENT_EGRESS, MOVEMENT_INFERRED_TRUST}


# ======================================================================================
# Hops that are not attacker movement
# ======================================================================================


class TestNonAdjacency:
    def test_being_in_a_region_is_not_a_route_into_it(self):
        """WorldGraph models no hypervisor or control plane for that hop to mean anything."""
        graph = WorldGraph(
            [
                ent("internet", kind=EntityType.NETWORK_NODE),
                ent("web", internet=True),
                ent("region", kind=EntityType.CLOUD_REGION),
            ],
            [
                dep("web", "internet", DependencyType.CONNECTS_TO),
                dep("web", "region", DependencyType.HOSTED_IN),
            ],
        )
        reached = {node for path in attack_paths(graph) for node in path.nodes}
        assert "region" not in reached

    def test_co_hosting_does_not_connect_two_workloads(self):
        """Otherwise every workload in a region is reachable from every other."""
        graph = WorldGraph(
            [
                ent("internet", kind=EntityType.NETWORK_NODE),
                ent("web", internet=True),
                ent("unrelated"),
                ent("region", kind=EntityType.CLOUD_REGION),
            ],
            [
                dep("web", "internet", DependencyType.CONNECTS_TO),
                dep("web", "region", DependencyType.HOSTED_IN),
                dep("unrelated", "region", DependencyType.HOSTED_IN),
            ],
        )
        reached = {node for path in attack_paths(graph) for node in path.nodes}
        assert "unrelated" not in reached

    def test_a_supply_contract_is_not_network_adjacency(self):
        graph = WorldGraph(
            [
                ent("internet", kind=EntityType.NETWORK_NODE),
                ent("web", internet=True),
                ent("supplier", kind=EntityType.SUPPLIER),
            ],
            [
                dep("web", "internet", DependencyType.CONNECTS_TO),
                dep("web", "supplier", DependencyType.SUPPLIED_BY),
            ],
        )
        reached = {node for path in attack_paths(graph) for node in path.nodes}
        assert "supplier" not in reached


class TestDeterminism:
    def test_the_same_graph_yields_the_same_answer(self):
        graph = shared_database_graph()
        first = attack_paths(graph, from_entity_id="internet")
        second = attack_paths(graph, from_entity_id="internet")
        assert [p.nodes for p in first] == [p.nodes for p in second]

    def test_a_cycle_does_not_loop(self):
        graph = WorldGraph(
            [ent("a"), ent("b")],
            [
                dep("a", "b", DependencyType.CONNECTS_TO),
                dep("b", "a", DependencyType.CONNECTS_TO),
            ],
        )
        paths = attack_paths(graph, from_entity_id="a")
        assert paths
        for path in paths:
            assert len(path.nodes) == len(set(path.nodes))
