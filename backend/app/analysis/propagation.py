"""WorldGraph V1 Impact Model — failure propagation over the dependency graph.

This module is deliberately small and completely deterministic. Given a world state and
a set of initial impacts, it answers one question: *what is every entity's availability
and serviceable capacity once the consequences settle?*

The model
---------
Each entity has a base availability from its :class:`HealthState` (1.0 healthy … 0.0 down).
For an entity ``X`` with dependencies ``T_i``::

    dependencyAvailability(X) = Π_i [ 1 − criticality_i · (1 − availability(T_i)) · (1 − redundancy_i) ]

    effectiveAvailability(X)  = baseAvailability(X) · dependencyAvailability(X)

Reading the dependency term: a target that is fully down (``availability = 0``) removes
``criticality_i`` of ``X``'s function, and ``redundancy_i`` of *that* loss is absorbed by
failover. A hard, unredundant dependency (``c = 1, r = 0``) therefore takes ``X`` down with
its target; a half-important dependency with a warm standby barely moves it.

**Capacity is solved separately**, with the same shape but the edge's
``capacity_impact``::

    capacity(X) = baseCapacity(X) · Π_i [ 1 − capacityImpact_i · (1 − capacity(T_i)) · (1 − redundancy_i) ]

Availability answers "are requests succeeding right now"; capacity answers "how much load
could we take". They coincide for most edges (``capacity_impact`` defaults to
``criticality``) and deliberately diverge for supply chains: losing a hardware supplier
leaves today's traffic flowing while removing the ability to replace or scale. Reporting
one number for both is how a model invents outages that are not happening.

Because dependencies can be cyclic, the solution is found by **Jacobi iteration**: every
round recomputes each entity from the previous round's values. Both quantities are
monotone non-increasing and bounded below by 0, so the iteration always converges; it is
stopped early once nothing moves by more than :data:`CONVERGENCE_EPSILON`.

This is **not** a general reliability model. It has no notion of queueing, retry storms,
saturation dynamics or correlated failure. It is a transparent first-order model whose
assumptions are written down in ``docs/IMPACT_MODEL.md``, and it is called the
*WorldGraph V1 Impact Model* everywhere it surfaces so nobody mistakes it for one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..graph.world_graph import WorldGraph
from ..models.core import (
    FAILURE_PROPAGATING_TYPES,
    DependencyEdge,
    DependencyType,
    HealthState,
    health_from_value,
)

#: Iteration stops when no value moves more than this in a round.
CONVERGENCE_EPSILON = 1e-4

#: Hard iteration cap. Convergence is normally reached in ≤ graph-depth rounds; the cap
#: only fires on pathological graphs and is reported rather than hidden.
MAX_ITERATIONS = 64

#: Availability below which an entity counts as "impacted". Above it, the drop is noise
#: from a partially-redundant dependency and would clutter every result.
IMPACT_THRESHOLD = 0.999

#: How much availability an entity must lose before a failure is credited with causing it.
#:
#: Small enough to catch a real cascade, large enough that solver noise and rounding do not
#: attribute entities nobody touched. Shared by the blast-radius and simulation engines so
#: the two cannot drift apart — they did, and the blast radius was the one that was wrong.
MATERIAL_DEGRADATION = 0.005


@dataclass(slots=True)
class ResolvedInput:
    """One pre-resolved dependency of an entity, ready for the solver's inner loop."""

    dependency_id: str
    criticality: float
    capacity_impact: float
    redundancy: float
    edge_type: DependencyType
    edge_id: str


@dataclass(slots=True)
class PropagationState:
    """The settled availability and capacity of every entity, plus how it got there."""

    availability: dict[str, float]
    capacity: dict[str, float]
    #: Availability each entity would have with no propagation — its own health only.
    baseline_availability: dict[str, float]
    #: For each entity, the dependency that hurt it most: ``(dependency_id, loss)``.
    dominant_cause: dict[str, tuple[str, float]] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = True

    def health(self, entity_id: str) -> HealthState:
        """Projected discrete health for an entity."""
        return health_from_value(self.availability.get(entity_id, 1.0))

    def delta(self, entity_id: str) -> float:
        """How much availability this entity lost relative to its own baseline."""
        return self.baseline_availability.get(entity_id, 1.0) - self.availability.get(
            entity_id, 1.0
        )

    def caused_ids(
        self,
        *,
        threshold: float = IMPACT_THRESHOLD,
        minimum_loss: float = MATERIAL_DEGRADATION,
    ) -> list[str]:
        """Entities this propagation actually *made worse*, worst first.

        The difference from :meth:`impacted_ids` is attribution, and it is the whole
        point. ``impacted_ids`` answers "what is degraded", which is the right question
        for a dashboard. This answers "what did the thing I just failed degrade", which is
        the only honest basis for a blast radius.

        On an estate that declares its health the two nearly agree, because everything
        starts at 1.0. On an imported estate they diverge completely: every entity starts
        at UNKNOWN (0.9) and inherits less through its edges, so *everything* is already
        below the threshold and ``impacted_ids`` returns the entire graph no matter what
        failed — including entities with no edge to the origin at all.
        """
        hits = [
            entity_id
            for entity_id, value in self.availability.items()
            if value < threshold and self.delta(entity_id) >= minimum_loss
        ]
        hits.sort(key=lambda entity_id: (self.availability.get(entity_id, 1.0), entity_id))
        return hits

    def impacted_ids(self, *, threshold: float = IMPACT_THRESHOLD) -> list[str]:
        """Entities whose availability sits below ``threshold``, worst first.

        Absolute, not causal: this is "what is degraded right now", which is what a
        dashboard wants. For "what did this failure cause", use :meth:`caused_ids`.
        """
        hits = [
            entity_id for entity_id, value in self.availability.items() if value < threshold
        ]
        hits.sort(key=lambda eid: (self.availability[eid], eid))
        return hits

    def capacity_constrained_ids(self, *, threshold: float = IMPACT_THRESHOLD) -> list[str]:
        """Entities whose *capacity* is constrained, even if they are still serving."""
        hits = [
            entity_id for entity_id, value in self.capacity.items() if value < threshold
        ]
        hits.sort(key=lambda eid: (self.capacity[eid], eid))
        return hits


def resolve_inputs(
    graph: WorldGraph,
    entity_id: str,
    *,
    disabled_edge_ids: frozenset[str] = frozenset(),
) -> list[ResolvedInput]:
    """Everything whose failure degrades ``entity_id``.

    Two families, unified here so the solver has one uniform notion of "input":

    * **outgoing** failure-propagating edges — what this entity needs;
    * **incoming ``SERVES``** edges — the provider of a service to this entity (used for
      customer regions, whose "dependency" is the service pointed at them).

    ``REPLICATES_TO`` is excluded in both directions: a replica going down does not take
    the primary with it, which is the entire reason replicas exist.
    """
    inputs: list[ResolvedInput] = []
    for edge in graph.dependencies_of(entity_id):
        if edge.type in FAILURE_PROPAGATING_TYPES and edge.id not in disabled_edge_ids:
            inputs.append(_resolved(edge, edge.target_entity_id))
    for edge in graph.dependents_of(entity_id):
        if edge.type is DependencyType.SERVES and edge.id not in disabled_edge_ids:
            inputs.append(_resolved(edge, edge.source_entity_id))
    return inputs


def _resolved(edge: DependencyEdge, dependency_id: str) -> ResolvedInput:
    return ResolvedInput(
        dependency_id=dependency_id,
        criticality=edge.criticality,
        capacity_impact=edge.effective_capacity_impact,
        redundancy=edge.redundancy,
        edge_type=edge.type,
        edge_id=edge.id,
    )


def edge_transfer(availability: float, coupling: float, redundancy: float) -> float:
    """The multiplier one dependency applies to its dependent.

    ``1.0`` means "this dependency is fine, or fully covered by failover"; ``0.0`` means
    "this dependency alone takes the dependent down". ``coupling`` is ``criticality`` when
    solving availability and ``capacity_impact`` when solving capacity.
    """
    lost = (1.0 - availability) * coupling * (1.0 - redundancy)
    return max(0.0, min(1.0, 1.0 - lost))


def propagate(
    graph: WorldGraph,
    *,
    initial_availability: dict[str, float] | None = None,
    capacity_overrides: dict[str, float] | None = None,
    disabled_edge_ids: frozenset[str] | None = None,
) -> PropagationState:
    """Solve the world's availability and capacity.

    Args:
        graph: the world (baseline or a simulation clone).
        initial_availability: hard-pinned availabilities, e.g. ``{"supplier-x": 0.0}``.
            These entities are *sources* of failure: their value is fixed and their own
            dependencies are not allowed to raise it.
        capacity_overrides: per-entity capacity ceilings in ``[0, 1]``. A capacity ceiling
            also caps availability — a cluster that can serve 40 % of its load cannot
            answer more than 40 % of its requests.
        disabled_edge_ids: edges removed for this solve (a severed link).

    Returns:
        A :class:`PropagationState` with settled values and dominant causes.
    """
    pinned = dict(initial_availability or {})
    caps = dict(capacity_overrides or {})
    disabled = disabled_edge_ids or frozenset()

    entities = {entity.id: entity for entity in graph.entities}
    pinned = {eid: value for eid, value in pinned.items() if eid in entities}
    caps = {eid: value for eid, value in caps.items() if eid in entities}

    inputs = {
        entity_id: resolve_inputs(graph, entity_id, disabled_edge_ids=disabled)
        for entity_id in entities
    }

    baseline_availability: dict[str, float] = {}
    availability: dict[str, float] = {}
    capacity: dict[str, float] = {}
    for entity_id, entity in entities.items():
        baseline_availability[entity_id] = entity.health_value
        base_capacity = caps.get(entity_id, entity.business.capacity)
        seed = pinned.get(entity_id, entity.health_value)
        availability[entity_id] = max(0.0, min(1.0, min(seed, base_capacity)))
        # A pinned outage takes the entity's capacity with it: a supplier that is down is
        # not quietly still able to supply.
        capacity[entity_id] = max(
            0.0, min(1.0, min(base_capacity, pinned.get(entity_id, 1.0)))
        )

    dominant: dict[str, tuple[str, float]] = {}
    iterations = 0
    converged = False

    for _ in range(MAX_ITERATIONS):
        iterations += 1
        next_availability: dict[str, float] = {}
        next_capacity: dict[str, float] = {}
        moved = 0.0

        for entity_id, entity in entities.items():
            base_capacity = caps.get(entity_id, entity.business.capacity)

            if entity_id in pinned:
                # A pinned entity is a boundary condition. Its own dependencies cannot
                # heal it, otherwise "Singapore is DOWN" would quietly become "mostly up".
                pinned_value = max(0.0, min(1.0, min(pinned[entity_id], base_capacity)))
                next_availability[entity_id] = pinned_value
                next_capacity[entity_id] = max(0.0, min(base_capacity, pinned[entity_id]))
            else:
                availability_factor = 1.0
                capacity_factor = 1.0
                worst_cause: tuple[str, float] | None = None
                for item in inputs[entity_id]:
                    dep_availability = availability.get(item.dependency_id, 1.0)
                    # Availability propagates from availability; capacity propagates from
                    # *capacity*. Using availability for both would let a cluster that is
                    # still answering requests hide the fact that it can no longer take
                    # the load of the peer it is supposed to fail over from.
                    dep_capacity = capacity.get(item.dependency_id, 1.0)
                    transfer = edge_transfer(dep_availability, item.criticality, item.redundancy)
                    availability_factor *= transfer
                    capacity_factor *= edge_transfer(
                        dep_capacity, item.capacity_impact, item.redundancy
                    )
                    loss = 1.0 - transfer
                    if loss > 0 and (worst_cause is None or loss > worst_cause[1]):
                        worst_cause = (item.dependency_id, loss)

                solved_capacity = max(0.0, min(1.0, base_capacity * capacity_factor))
                solved_availability = entity.health_value * availability_factor
                # Capacity is a ceiling on availability: you cannot serve load you have
                # no capacity for.
                solved_availability = max(0.0, min(solved_availability, base_capacity))
                next_availability[entity_id] = max(0.0, min(1.0, solved_availability))
                next_capacity[entity_id] = solved_capacity
                if worst_cause is not None:
                    dominant[entity_id] = worst_cause

            moved = max(
                moved,
                abs(next_availability[entity_id] - availability[entity_id]),
                abs(next_capacity[entity_id] - capacity[entity_id]),
            )

        availability = next_availability
        capacity = next_capacity
        if moved < CONVERGENCE_EPSILON:
            converged = True
            break

    return PropagationState(
        availability=availability,
        capacity=capacity,
        baseline_availability=baseline_availability,
        dominant_cause=dominant,
        iterations=iterations,
        converged=converged,
    )
