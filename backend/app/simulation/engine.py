"""What-if simulation.

The contract: **the real world state is never mutated.** A scenario is a list of
overrides; applying it clones the graph, layers the overrides onto the clone, solves it,
and compares against a baseline solve of the untouched world. That is what makes the
"CURRENT vs SIMULATION" table trustworthy — both columns come from the same engine run
against two worlds that differ only by the overrides the operator added.

Everything a simulation produces is stamped ``SIMULATED`` so it can never be mistaken for
an observation.
"""

from __future__ import annotations

import time
import uuid

from ..analysis.blast_radius import calculate_blast_radius
from ..analysis.business_impact import snapshot_metrics
from ..analysis.propagation import IMPACT_THRESHOLD, propagate
from ..graph.world_graph import WorldGraph
from ..models.analysis import (
    ImpactedEntity,
    ImpactPath,
    MetricDelta,
    OverrideKind,
    PathHop,
    SimulationComparison,
    SimulationOverride,
    SimulationScenario,
    WorldSnapshotMetrics,
)
from ..models.core import DataMode, HealthState, utcnow


class SimulationError(ValueError):
    """An override that cannot be applied — an unknown entity or edge."""


def new_scenario(
    name: str, *, description: str = "", origin_event_id: str | None = None
) -> SimulationScenario:
    """Create an empty scenario."""
    return SimulationScenario(
        id=f"sim-{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        origin_event_id=origin_event_id,
    )


def validate_override(graph: WorldGraph, override: SimulationOverride) -> None:
    """Reject an override that names something the world does not contain.

    Silently ignoring an unknown target would produce a simulation that looks like it ran
    and quietly did nothing — the most dangerous possible outcome for a decision tool.
    """
    if override.kind is OverrideKind.EDGE_DISABLED:
        if graph.edge(override.target_id) is None:
            raise SimulationError(f"unknown dependency edge '{override.target_id}'")
        return
    if override.target_id not in graph:
        raise SimulationError(f"unknown entity '{override.target_id}'")
    if override.kind is OverrideKind.ENTITY_HEALTH and override.health is None:
        raise SimulationError("ENTITY_HEALTH override requires a health value")
    if override.kind is OverrideKind.ENTITY_CAPACITY and override.capacity is None:
        raise SimulationError("ENTITY_CAPACITY override requires a capacity value")


def compile_overrides(
    graph: WorldGraph, scenario: SimulationScenario
) -> tuple[dict[str, float], dict[str, float], frozenset[str]]:
    """Turn a scenario into solver inputs.

    Returns ``(pinned_availability, capacity_overrides, disabled_edge_ids)``.
    Later overrides on the same target win, so an operator can revise a failure without
    removing and re-adding it.
    """
    from ..models.core import HEALTH_VALUES

    pinned: dict[str, float] = {}
    capacities: dict[str, float] = {}
    disabled: set[str] = set()

    for override in scenario.overrides:
        validate_override(graph, override)
        if override.kind is OverrideKind.ENTITY_HEALTH and override.health is not None:
            pinned[override.target_id] = HEALTH_VALUES[override.health]
        elif override.kind is OverrideKind.ENTITY_CAPACITY and override.capacity is not None:
            capacities[override.target_id] = override.capacity
        elif override.kind is OverrideKind.EDGE_DISABLED:
            disabled.add(override.target_id)

    return pinned, capacities, frozenset(disabled)


def compare(
    graph: WorldGraph,
    scenario: SimulationScenario,
    *,
    include_blast_radius: bool = True,
    impact_threshold: float = IMPACT_THRESHOLD,
) -> SimulationComparison:
    """Solve the baseline and the scenario, and diff them."""
    started = time.perf_counter()
    pinned, capacities, disabled = compile_overrides(graph, scenario)

    baseline_state = propagate(graph)
    baseline_metrics = snapshot_metrics(graph, baseline_state)

    # The clone is belt-and-braces: the solver already treats the graph as read-only, but
    # a future override kind that edits entities must not be able to reach the real world.
    sim_graph = graph.clone()
    sim_state = propagate(
        sim_graph,
        initial_availability=pinned,
        capacity_overrides=capacities,
        disabled_edge_ids=disabled,
    )

    blast = None
    risk_score = None
    if include_blast_radius and (pinned or capacities or disabled):
        origins = sorted(pinned.keys()) or sorted(capacities.keys())
        if origins:
            blast = calculate_blast_radius(
                sim_graph,
                origin_ids=origins,
                origin_kind="SCENARIO",
                origin_label=scenario.name,
                initial_availability=pinned,
                capacity_overrides=capacities,
                disabled_edge_ids=disabled,
                mode=DataMode.SIMULATED,
                impact_threshold=impact_threshold,
            )
            risk_score = blast.risk.score

    sim_metrics = snapshot_metrics(sim_graph, sim_state, risk_score=risk_score)

    newly_impacted: list[ImpactedEntity] = []
    for entity_id in sim_state.impacted_ids(threshold=impact_threshold):
        if baseline_state.availability.get(entity_id, 1.0) < impact_threshold:
            continue  # already degraded before the scenario; not caused by it
        entity = sim_graph.entity(entity_id)
        if entity is None:
            continue
        path = _cascade_path(sim_graph, sim_state, sorted(pinned.keys()), entity_id)
        newly_impacted.append(
            ImpactedEntity(
                entity_id=entity.id,
                entity_name=entity.name,
                entity_type=entity.type.value,
                criticality=entity.criticality,
                depth=path.depth,
                availability=round(sim_state.availability.get(entity_id, 1.0), 4),
                availability_delta=round(
                    baseline_state.availability.get(entity_id, 1.0)
                    - sim_state.availability.get(entity_id, 1.0),
                    4,
                ),
                projected_health=sim_state.health(entity_id),
                path=path,
                customer_facing=_is_customer_facing(entity),
            )
        )
    newly_impacted.sort(key=lambda r: (r.availability, r.entity_id))

    cascade = [record.path for record in newly_impacted if record.path.depth >= 2][:6]

    return SimulationComparison(
        scenario=scenario,
        baseline=baseline_metrics,
        simulated=sim_metrics,
        deltas=build_deltas(baseline_metrics, sim_metrics),
        newly_impacted=newly_impacted,
        cascade_paths=cascade,
        blast_radius=blast,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def build_deltas(
    baseline: WorldSnapshotMetrics, simulated: WorldSnapshotMetrics
) -> list[MetricDelta]:
    """The CURRENT vs SIMULATION table rows.

    ``direction`` is computed from the numbers rather than assumed, so a scenario that
    *improves* something (removing a bad dependency, say) is coloured honestly.
    """
    rows: list[MetricDelta] = [
        MetricDelta(
            key="availability",
            label="Availability",
            baseline=f"{baseline.availability * 100:.2f}%",
            simulated=f"{simulated.availability * 100:.2f}%",
            direction=_direction(baseline.availability, simulated.availability, higher_is_better=True),
        )
    ]

    for region in sorted(set(baseline.regional_capacity) | set(simulated.regional_capacity)):
        base_value = baseline.regional_capacity.get(region, 1.0)
        sim_value = simulated.regional_capacity.get(region, 1.0)
        if abs(base_value - sim_value) < 1e-4 and base_value >= 0.9999:
            continue  # unchanged and healthy — noise in a comparison table
        rows.append(
            MetricDelta(
                key=f"capacity.{region}",
                label=f"{region} capacity",
                baseline=f"{base_value * 100:.0f}%",
                simulated=f"{sim_value * 100:.0f}%",
                direction=_direction(base_value, sim_value, higher_is_better=True),
            )
        )

    rows.extend(
        [
            MetricDelta(
                key="critical_services",
                label="Critical services impacted",
                baseline=str(baseline.critical_services_impacted),
                simulated=str(simulated.critical_services_impacted),
                direction=_direction(
                    baseline.critical_services_impacted,
                    simulated.critical_services_impacted,
                    higher_is_better=False,
                ),
            ),
            MetricDelta(
                key="customer_regions",
                label="Customer regions impacted",
                baseline=str(baseline.customer_regions_impacted),
                simulated=str(simulated.customer_regions_impacted),
                direction=_direction(
                    baseline.customer_regions_impacted,
                    simulated.customer_regions_impacted,
                    higher_is_better=False,
                ),
            ),
            MetricDelta(
                key="customers_affected",
                label="Customers affected",
                baseline=f"{baseline.customers_affected:,}",
                simulated=f"{simulated.customers_affected:,}",
                direction=_direction(
                    baseline.customers_affected,
                    simulated.customers_affected,
                    higher_is_better=False,
                ),
            ),
            MetricDelta(
                key="revenue_at_risk",
                label="Revenue at risk / hour",
                baseline=_money(baseline.revenue_at_risk_per_hour),
                simulated=_money(simulated.revenue_at_risk_per_hour),
                direction=_direction(
                    baseline.revenue_at_risk_per_hour,
                    simulated.revenue_at_risk_per_hour,
                    higher_is_better=False,
                ),
            ),
            MetricDelta(
                key="material_risk",
                label="Material risk",
                baseline=baseline.material_risk.value,
                simulated=simulated.material_risk.value,
                direction=_direction(
                    _risk_rank(baseline.material_risk),
                    _risk_rank(simulated.material_risk),
                    higher_is_better=False,
                ),
            ),
        ]
    )
    return rows


def _direction(
    baseline: float, simulated: float, *, higher_is_better: bool
) -> str:
    if abs(float(baseline) - float(simulated)) < 1e-9:
        return "same"
    improved = simulated > baseline if higher_is_better else simulated < baseline
    return "better" if improved else "worse"


def _risk_rank(severity) -> int:
    order = ["INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"]
    return order.index(severity.value) if severity.value in order else 0


def _money(value: float) -> str:
    """Compact currency for a comparison cell. Always a modelled figure."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _is_customer_facing(entity) -> bool:
    from ..analysis.business_impact import is_customer_facing

    return is_customer_facing(entity)


def _cascade_path(
    graph: WorldGraph, state, origins: list[str], entity_id: str
) -> ImpactPath:
    """Route from a scenario origin to a newly impacted entity."""
    if origins:
        traversal = graph.traverse_dependents(origins)
        step = traversal.by_id().get(entity_id)
        if step is not None:
            hops = []
            for index, node_id in enumerate(step.path):
                node = graph.entity(node_id)
                hops.append(
                    PathHop(
                        entity_id=node_id,
                        entity_name=node.name if node else node_id,
                        edge_type=step.edge_types[index - 1] if index > 0 else None,
                        availability=round(state.availability.get(node_id, 1.0), 4),
                    )
                )
            return ImpactPath(
                hops=hops,
                terminal_availability=round(state.availability.get(entity_id, 1.0), 4),
            )
    entity = graph.require_entity(entity_id)
    return ImpactPath(
        hops=[
            PathHop(
                entity_id=entity.id,
                entity_name=entity.name,
                availability=round(state.availability.get(entity_id, 1.0), 4),
            )
        ],
        terminal_availability=round(state.availability.get(entity_id, 1.0), 4),
    )


def override_for_health(target_id: str, health: HealthState, *, note: str = "") -> SimulationOverride:
    """Convenience builder used by the API and the AI tool layer."""
    return SimulationOverride(
        id=f"ovr-{uuid.uuid4().hex[:10]}",
        kind=OverrideKind.ENTITY_HEALTH,
        target_id=target_id,
        health=health,
        note=note,
    )


def override_for_capacity(target_id: str, capacity: float, *, note: str = "") -> SimulationOverride:
    return SimulationOverride(
        id=f"ovr-{uuid.uuid4().hex[:10]}",
        kind=OverrideKind.ENTITY_CAPACITY,
        target_id=target_id,
        capacity=capacity,
        note=note,
    )


def touch(scenario: SimulationScenario) -> SimulationScenario:
    """Bump ``updated_at`` after a mutation."""
    return scenario.model_copy(update={"updated_at": utcnow()})
