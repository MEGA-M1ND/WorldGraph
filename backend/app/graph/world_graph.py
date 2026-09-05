"""The WorldGraph dependency graph.

A thin, explicit wrapper over NetworkX. It exists so that:

  * direction is stated once, in one place, instead of being re-derived at every call
    site (edges point *dependent → dependency*, so failure flows against the arrows);
  * traversal always carries an explanation path, never just a set of ids;
  * every traversal is bounded — max depth and a node budget — and says so when it
    truncates, because a silently truncated blast radius is worse than none.

NetworkX is used for storage and the classic algorithms (cycles, components, shortest
path). The failure-propagation walk is hand-written because it carries availability
arithmetic that no generic algorithm knows about.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import networkx as nx

from ..models.core import (
    DependencyEdge,
    DependencyType,
    EntityType,
    WorldEntity,
)

#: Hard ceiling on traversal depth. The AtlasPay estate is 6 layers deep; 12 leaves
#: headroom for a real estate while still bounding a pathological cycle-free chain.
DEFAULT_MAX_DEPTH = 12

#: Hard ceiling on nodes visited in one traversal. Protects the API from a graph that
#: grows past the V1 in-memory design without anyone noticing.
DEFAULT_NODE_BUDGET = 5000


@dataclass(slots=True)
class TraversalStep:
    """One node reached by a traversal, with the route that reached it."""

    entity_id: str
    depth: int
    #: Ids from the origin to this node, inclusive of both ends.
    path: list[str]
    #: Edge types used along ``path``; ``len(edge_types) == len(path) - 1``.
    edge_types: list[DependencyType] = field(default_factory=list)


@dataclass(slots=True)
class TraversalResult:
    """Result of a bounded traversal."""

    steps: list[TraversalStep]
    truncated: bool = False
    truncation_reason: str | None = None
    cycles_broken: list[list[str]] = field(default_factory=list)

    def entity_ids(self) -> list[str]:
        """Ids reached, in traversal (breadth-first) order."""
        return [step.entity_id for step in self.steps]

    def by_id(self) -> dict[str, TraversalStep]:
        """Index of the shallowest step per entity."""
        return {step.entity_id: step for step in self.steps}


class WorldGraph:
    """An in-memory dependency graph over a set of entities.

    V1 keeps the whole graph resident: the AtlasPay estate is ~60 nodes and a real mid-
    size estate is thousands, which is trivially in-memory. Persistence is relational
    (see ``storage/``); this class is rebuilt from the repository on load and on write.
    """

    def __init__(
        self,
        entities: Iterable[WorldEntity] = (),
        edges: Iterable[DependencyEdge] = (),
    ) -> None:
        self._entities: dict[str, WorldEntity] = {}
        self._edges: dict[str, DependencyEdge] = {}
        # MultiDiGraph: two entities can be related in more than one way (a cluster is
        # HOSTED_IN a region *and* CONNECTS_TO it for egress).
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        for entity in entities:
            self.add_entity(entity)
        for edge in edges:
            self.add_edge(edge)

    # -- construction ------------------------------------------------------------------

    def add_entity(self, entity: WorldEntity) -> None:
        """Insert or replace an entity."""
        self._entities[entity.id] = entity
        self._g.add_node(entity.id)

    def add_edge(self, edge: DependencyEdge) -> None:
        """Insert or replace an edge.

        Edges referencing unknown entities are rejected rather than silently creating
        phantom nodes — a dangling edge would make the graph claim reachability into
        infrastructure that does not exist, which is precisely the failure mode the AI
        tests assert against.
        """
        if edge.source_entity_id not in self._entities:
            raise KeyError(f"unknown source entity '{edge.source_entity_id}' for edge '{edge.id}'")
        if edge.target_entity_id not in self._entities:
            raise KeyError(f"unknown target entity '{edge.target_entity_id}' for edge '{edge.id}'")
        self._edges[edge.id] = edge
        self._g.add_edge(edge.source_entity_id, edge.target_entity_id, key=edge.id)

    def remove_entity(self, entity_id: str) -> None:
        """Remove an entity and every edge touching it."""
        self._entities.pop(entity_id, None)
        if self._g.has_node(entity_id):
            self._g.remove_node(entity_id)
        self._edges = {
            eid: e
            for eid, e in self._edges.items()
            if entity_id not in (e.source_entity_id, e.target_entity_id)
        }

    # -- access ------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entities)

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._entities

    @property
    def entities(self) -> list[WorldEntity]:
        return list(self._entities.values())

    @property
    def edges(self) -> list[DependencyEdge]:
        return list(self._edges.values())

    def entity(self, entity_id: str) -> WorldEntity | None:
        return self._entities.get(entity_id)

    def require_entity(self, entity_id: str) -> WorldEntity:
        entity = self._entities.get(entity_id)
        if entity is None:
            raise KeyError(f"unknown entity '{entity_id}'")
        return entity

    def edge(self, edge_id: str) -> DependencyEdge | None:
        return self._edges.get(edge_id)

    def entities_of_type(self, *types: EntityType) -> list[WorldEntity]:
        wanted = set(types)
        return [e for e in self._entities.values() if e.type in wanted]

    def located_entities(self) -> list[WorldEntity]:
        """Entities that have coordinates — the candidate set for spatial correlation."""
        return [e for e in self._entities.values() if e.location is not None]

    # -- neighbourhood -----------------------------------------------------------------

    def dependencies_of(self, entity_id: str) -> list[DependencyEdge]:
        """Edges where ``entity_id`` is the dependent (what it needs)."""
        if not self._g.has_node(entity_id):
            return []
        return [self._edges[key] for _, _, key in self._g.out_edges(entity_id, keys=True)]

    def dependents_of(self, entity_id: str) -> list[DependencyEdge]:
        """Edges where ``entity_id`` is the dependency (what needs it)."""
        if not self._g.has_node(entity_id):
            return []
        return [self._edges[key] for _, _, key in self._g.in_edges(entity_id, keys=True)]

    def hosted_entities(self, site_id: str) -> list[WorldEntity]:
        """Logical entities hosted at a physical site (``X HOSTED_IN site``)."""
        return [
            self._entities[edge.source_entity_id]
            for edge in self.dependents_of(site_id)
            if edge.type is DependencyType.HOSTED_IN
        ]

    # -- traversal ---------------------------------------------------------------------

    def traverse_dependents(
        self,
        origin_ids: Iterable[str],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        node_budget: int = DEFAULT_NODE_BUDGET,
        edge_types: frozenset[DependencyType] | None = None,
    ) -> TraversalResult:
        """Walk *upstream against the arrows*: who is hurt when the origins fail.

        This is the direction blast radius cares about. ``SERVES`` edges are followed in
        their natural direction as well, because ``service SERVES customers`` already
        points at the party who suffers.
        """
        return self._bounded_walk(
            origin_ids,
            successors=lambda node: self._failure_successors(node, edge_types),
            max_depth=max_depth,
            node_budget=node_budget,
        )

    def traverse_dependencies(
        self,
        origin_ids: Iterable[str],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        node_budget: int = DEFAULT_NODE_BUDGET,
    ) -> TraversalResult:
        """Walk *with the arrows*: what the origins rely on, transitively."""
        return self._bounded_walk(
            origin_ids,
            successors=lambda node: [
                (edge.target_entity_id, edge.type) for edge in self.dependencies_of(node)
            ],
            max_depth=max_depth,
            node_budget=node_budget,
        )

    def _failure_successors(
        self,
        node: str,
        edge_types: frozenset[DependencyType] | None,
    ) -> list[tuple[str, DependencyType]]:
        """Nodes that degrade when ``node`` degrades."""
        out: list[tuple[str, DependencyType]] = []
        for edge in self.dependents_of(node):
            # Incoming edge: someone declares a need for `node`. Failure flows to them,
            # except REPLICATES_TO, where the *replica* failing does not take down the
            # primary (that asymmetry is the whole point of a replica).
            if edge.type is DependencyType.REPLICATES_TO:
                continue
            if edge_types is not None and edge.type not in edge_types:
                continue
            out.append((edge.source_entity_id, edge.type))
        for edge in self.dependencies_of(node):
            # Outgoing SERVES: node provides something to the target, so the target
            # (a customer region, typically) suffers when node does.
            if edge.type is DependencyType.SERVES:
                if edge_types is not None and edge.type not in edge_types:
                    continue
                out.append((edge.target_entity_id, edge.type))
        return out

    def _bounded_walk(
        self,
        origin_ids: Iterable[str],
        *,
        successors,
        max_depth: int,
        node_budget: int,
    ) -> TraversalResult:
        """Breadth-first walk with cycle breaking, depth cap and node budget."""
        origins = [oid for oid in dict.fromkeys(origin_ids) if oid in self._entities]
        steps: list[TraversalStep] = []
        seen: set[str] = set()
        cycles: list[list[str]] = []
        truncated = False
        reason: str | None = None

        queue: deque[TraversalStep] = deque()
        for oid in origins:
            step = TraversalStep(entity_id=oid, depth=0, path=[oid])
            seen.add(oid)
            steps.append(step)
            queue.append(step)

        while queue:
            current = queue.popleft()
            if current.depth >= max_depth:
                # Only a real, unexplored successor counts as truncation. A node whose
                # successors were all visited already is a complete answer, not a cut one.
                if any(nid not in seen for nid, _ in successors(current.entity_id)):
                    truncated = True
                    reason = f"traversal stopped at max depth {max_depth}"
                continue
            for next_id, edge_type in successors(current.entity_id):
                if next_id in current.path:
                    # A cycle. Record it once and do not follow it — otherwise
                    # availability would be multiplied down forever.
                    cycle = [*current.path[current.path.index(next_id):], next_id]
                    if cycle not in cycles:
                        cycles.append(cycle)
                    continue
                if next_id in seen:
                    continue
                if len(seen) >= node_budget:
                    truncated = True
                    reason = f"traversal stopped at node budget {node_budget}"
                    queue.clear()
                    break
                seen.add(next_id)
                step = TraversalStep(
                    entity_id=next_id,
                    depth=current.depth + 1,
                    path=[*current.path, next_id],
                    edge_types=[*current.edge_types, edge_type],
                )
                steps.append(step)
                queue.append(step)

        return TraversalResult(
            steps=steps, truncated=truncated, truncation_reason=reason, cycles_broken=cycles
        )

    # -- classic graph queries ---------------------------------------------------------

    def shortest_dependency_path(self, source_id: str, target_id: str) -> list[str] | None:
        """Shortest chain of dependencies from ``source_id`` down to ``target_id``.

        Follows the arrows: "how does checkout-platform end up relying on
        postgres-singapore?" Returns ``None`` when no such chain exists.
        """
        if source_id not in self._entities or target_id not in self._entities:
            return None
        try:
            return nx.shortest_path(self._g, source_id, target_id)  # type: ignore[no-any-return]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def impact_path(self, origin_id: str, impacted_id: str) -> list[str] | None:
        """Shortest failure-propagation chain from ``origin_id`` to ``impacted_id``."""
        result = self.traverse_dependents([origin_id])
        step = result.by_id().get(impacted_id)
        return step.path if step else None

    def cycles(self, *, limit: int = 25) -> list[list[str]]:
        """Dependency cycles present in the graph.

        Bounded: ``simple_cycles`` is exponential in the worst case, and a UI that lists
        cycles only ever needs the first handful.
        """
        found: list[list[str]] = []
        for cycle in nx.simple_cycles(self._g):
            found.append(list(cycle))
            if len(found) >= limit:
                break
        return found

    def connected_components(self) -> list[list[str]]:
        """Weakly connected components — islands of infrastructure."""
        return [sorted(c) for c in nx.weakly_connected_components(self._g)]

    def single_points_of_failure(
        self, *, min_dependents: int = 2
    ) -> list[tuple[str, int]]:
        """Entities with no redundancy whose loss reaches ``min_dependents`` others.

        Structural risk, computed from the graph rather than asserted by a human. Used by
        executive mode's material-risk list.
        """
        results: list[tuple[str, int]] = []
        for entity in self._entities.values():
            if entity.business.redundancy > 1:
                continue
            reach = self.traverse_dependents([entity.id])
            downstream = len(reach.steps) - 1
            if downstream >= min_dependents:
                results.append((entity.id, downstream))
        results.sort(key=lambda pair: (-pair[1], pair[0]))
        return results

    def iter_entities(self) -> Iterator[WorldEntity]:
        return iter(self._entities.values())

    def clone(self) -> WorldGraph:
        """A deep-enough copy for simulation.

        Entity models are copied (the simulation mutates health and capacity); edges are
        copied too so an ``EDGE_DISABLED`` override can drop one without touching the
        baseline graph. Pydantic's ``model_copy(deep=True)`` gives value semantics.
        """
        return WorldGraph(
            entities=[e.model_copy(deep=True) for e in self._entities.values()],
            edges=[e.model_copy(deep=True) for e in self._edges.values()],
        )
