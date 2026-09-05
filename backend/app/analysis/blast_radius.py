"""The blast-radius engine.

Deterministic, explainable, and bounded. Given an origin — a world event, a failed asset,
a security finding, or a simulation scenario — it produces the full
:class:`BlastRadiusResult`: who is directly hit, who is transitively hit, the critical
paths, customer exposure, business impact, a scored risk with its derivation, and honest
confidence.

No language model participates in any step of this file.
"""

from __future__ import annotations

import time
import uuid
from typing import Iterable, Literal

from ..graph.world_graph import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_NODE_BUDGET,
    WorldGraph,
)
from ..models.analysis import (
    BlastRadiusResult,
    ImpactedEntity,
    ImpactPath,
    PathHop,
)
from ..models.core import DataMode, WorldEvent
from .business_impact import business_impact, customer_exposure, is_customer_facing
from .propagation import IMPACT_THRESHOLD, PropagationState, propagate
from .risk import assess_confidence, score_impact

#: How many critical paths to surface. More than this is unreadable in a side panel and
#: the UI already lets an operator trace any entity individually.
MAX_CRITICAL_PATHS = 6


def calculate_blast_radius(
    graph: WorldGraph,
    *,
    origin_ids: Iterable[str],
    origin_kind: Literal["EVENT", "ENTITY", "SCENARIO", "SECURITY_FINDING"] = "ENTITY",
    origin_label: str | None = None,
    initial_availability: dict[str, float] | None = None,
    capacity_overrides: dict[str, float] | None = None,
    disabled_edge_ids: frozenset[str] | None = None,
    event: WorldEvent | None = None,
    proximity: float = 0.0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    node_budget: int = DEFAULT_NODE_BUDGET,
    mode: DataMode = DataMode.SYNTHETIC,
    impact_threshold: float = IMPACT_THRESHOLD,
) -> BlastRadiusResult:
    """Compute the blast radius of one or more failing origins.

    Args:
        graph: the world to compute against (baseline or a simulation clone).
        origin_ids: entities that are failing or have been struck.
        origin_kind: what kind of thing started this, for the UI and stored analyses.
        origin_label: human label; defaults to the origin entity names.
        initial_availability: pinned availabilities per origin. Defaults to 0.0 (down)
            for every origin that has no explicit value.
        capacity_overrides / disabled_edge_ids: simulation modifiers.
        event: originating world event, when there is one.
        proximity: 0-1 geographic proximity from the correlation step.
        max_depth / node_budget: traversal bounds. Truncation is reported, never silent.
        mode: provenance mode stamped on the result.
        impact_threshold: availability below which an entity counts as impacted.
    """
    started = time.perf_counter()
    origins = [oid for oid in dict.fromkeys(origin_ids) if oid in graph]
    if not origins:
        raise KeyError("no known origin entities supplied to the blast-radius engine")

    pinned = {oid: 0.0 for oid in origins}
    pinned.update(initial_availability or {})
    # Only keep pins that name a real entity — an unknown pin would silently do nothing.
    pinned = {eid: value for eid, value in pinned.items() if eid in graph}

    state = propagate(
        graph,
        initial_availability=pinned,
        capacity_overrides=capacity_overrides,
        disabled_edge_ids=disabled_edge_ids,
    )

    traversal = graph.traverse_dependents(
        origins, max_depth=max_depth, node_budget=node_budget
    )
    depth_by_id = {step.entity_id: step.depth for step in traversal.steps}
    path_by_id = {step.entity_id: step for step in traversal.steps}

    direct: list[ImpactedEntity] = []
    indirect: list[ImpactedEntity] = []
    max_depth_reached = 0

    for entity_id in state.impacted_ids(threshold=impact_threshold):
        entity = graph.entity(entity_id)
        if entity is None:
            continue
        depth = depth_by_id.get(entity_id, 0 if entity_id in origins else 1)
        max_depth_reached = max(max_depth_reached, depth)
        record = ImpactedEntity(
            entity_id=entity.id,
            entity_name=entity.name,
            entity_type=entity.type.value,
            criticality=entity.criticality,
            depth=depth,
            availability=round(state.availability.get(entity_id, 1.0), 4),
            availability_delta=round(state.delta(entity_id), 4),
            projected_health=state.health(entity_id),
            path=_build_path(graph, state, path_by_id.get(entity_id), entity_id),
            customer_facing=is_customer_facing(entity),
        )
        # Depth 0 (an origin) and depth 1 (a direct dependent) are "directly affected";
        # anything the failure had to travel further to reach is indirect.
        if depth <= 1:
            direct.append(record)
        else:
            indirect.append(record)

    direct.sort(key=lambda r: (r.availability, r.entity_id))
    indirect.sort(key=lambda r: (r.availability, r.entity_id))

    exposure = customer_exposure(graph, state, threshold=impact_threshold)
    impact = business_impact(graph, state, threshold=impact_threshold)

    stale_seconds = event.source.freshness_seconds() if event is not None else None
    risk = score_impact(
        graph,
        state,
        origin_ids=origins,
        event=event,
        proximity=proximity,
        max_depth_reached=max_depth_reached,
        stale_seconds=stale_seconds,
    )
    confidence = assess_confidence(
        graph,
        state,
        event=event,
        origin_ids=origins,
        truncated=traversal.truncated,
        proximity=proximity,
    )

    critical_paths = _critical_paths(graph, state, direct + indirect)
    label = origin_label or ", ".join(
        graph.require_entity(oid).name for oid in origins[:3]
    )

    explanations = _explain(
        graph,
        state,
        origins=origins,
        direct=direct,
        indirect=indirect,
        exposure=exposure,
        impact=impact,
        event=event,
        critical_paths=critical_paths,
    )

    return BlastRadiusResult(
        id=f"blast-{uuid.uuid4().hex[:12]}",
        origin_kind=origin_kind,
        origin_ids=origins,
        origin_label=label,
        severity=risk.severity,
        risk=risk,
        direct_impact=direct,
        indirect_impact=indirect,
        critical_paths=critical_paths,
        customer_exposure=exposure,
        business_impact=impact,
        explanations=explanations,
        confidence=confidence,
        truncated=traversal.truncated,
        truncation_reason=traversal.truncation_reason,
        cycles_detected=traversal.cycles_broken,
        mode=mode,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def _build_path(
    graph: WorldGraph,
    state: PropagationState,
    step,
    entity_id: str,
) -> ImpactPath:
    """Turn a traversal step into a hop-by-hop explanation with availabilities."""
    if step is None:
        entity = graph.require_entity(entity_id)
        return ImpactPath(
            hops=[
                PathHop(
                    entity_id=entity.id,
                    entity_name=entity.name,
                    availability=round(state.availability.get(entity.id, 1.0), 4),
                )
            ],
            terminal_availability=round(state.availability.get(entity.id, 1.0), 4),
        )

    hops: list[PathHop] = []
    for index, node_id in enumerate(step.path):
        entity = graph.entity(node_id)
        hops.append(
            PathHop(
                entity_id=node_id,
                entity_name=entity.name if entity else node_id,
                edge_type=step.edge_types[index - 1] if index > 0 else None,
                availability=round(state.availability.get(node_id, 1.0), 4),
            )
        )
    return ImpactPath(
        hops=hops,
        terminal_availability=round(state.availability.get(step.path[-1], 1.0), 4),
    )


def _critical_paths(
    graph: WorldGraph, state: PropagationState, impacted: list[ImpactedEntity]
) -> list[ImpactPath]:
    """The most consequential routes through the blast radius.

    "Critical" means: the path terminates at a customer-facing or CRITICAL entity, and it
    is ranked by how badly that terminus was hurt. These are the paths the globe animates.
    """
    candidates = [
        record
        for record in impacted
        if record.customer_facing or record.criticality.value == "CRITICAL"
    ]
    candidates.sort(key=lambda r: (r.availability, -r.depth, r.entity_id))
    paths: list[ImpactPath] = []
    seen_termini: set[str] = set()
    for record in candidates:
        if record.path.depth < 1:
            continue  # an origin is not a path
        terminus = record.path.hops[-1].entity_id
        if terminus in seen_termini:
            continue
        seen_termini.add(terminus)
        paths.append(record.path)
        if len(paths) >= MAX_CRITICAL_PATHS:
            break
    return paths


def _explain(
    graph: WorldGraph,
    state: PropagationState,
    *,
    origins: list[str],
    direct: list[ImpactedEntity],
    indirect: list[ImpactedEntity],
    exposure,
    impact,
    event: WorldEvent | None,
    critical_paths: list[ImpactPath],
) -> list[str]:
    """Plain-language, fully derived explanation lines.

    Written here rather than in a prompt so they exist with or without a model, and so
    they can never disagree with the numbers they describe.
    """
    lines: list[str] = []
    origin_names = [graph.require_entity(oid).name for oid in origins]

    if event is not None:
        lines.append(
            f"Origin: {event.title} "
            f"({event.category.value.replace('_', ' ').lower()}, {event.severity.value})."
        )
    lines.append(
        "Directly affected: "
        + (", ".join(sorted(r.entity_name for r in direct)) if direct else "none")
        + "."
    )
    if indirect:
        lines.append(
            f"Indirectly affected through dependencies: {len(indirect)} entities, "
            f"deepest at {max(r.depth for r in indirect)} hops from origin."
        )
    else:
        lines.append("No transitive dependents were degraded beyond the direct impact.")

    for path in critical_paths[:3]:
        lines.append(
            f"Critical path: {path.as_text()} "
            f"(terminal availability {path.terminal_availability * 100:.1f}%)."
        )

    if exposure:
        worst = exposure[0]
        lines.append(
            f"Customer exposure: {worst.region} sees an estimated "
            f"{worst.traffic_impact * 100:.0f}% traffic impact "
            f"({worst.customer_count:,} modelled customers). MODELLED ESTIMATE."
        )
    else:
        lines.append("No customer region falls below nominal availability in this model.")

    lines.append(
        f"Modelled organisation availability {impact.availability * 100:.2f}%, "
        f"{impact.critical_services_impacted} critical services impacted, "
        f"{impact.customer_regions_impacted} customer regions impacted. MODELLED ESTIMATE."
    )

    for origin_id in origins:
        cause = state.dominant_cause.get(origin_id)
        if cause is None:
            continue
        dominant = graph.entity(cause[0])
        if dominant is not None:
            lines.append(
                f"{graph.require_entity(origin_id).name}'s largest single dependency loss "
                f"comes from {dominant.name}."
            )
    if not origin_names:
        lines.append("No origin entity resolved.")
    return lines
