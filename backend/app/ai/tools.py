"""The AI tool layer.

Architectural rule, stated once and enforced by this file: **the model never computes.**
It interprets intent, chooses a tool, and explains the result. Graph traversal, geographic
matching, impact arithmetic and simulation all happen in deterministic Python that a
developer can unit-test and step through.

Consequences that fall out of the rule:

* Every tool has a Pydantic argument schema. An unparseable call is refused with a message,
  not coerced into something plausible.
* The registry is an allowlist. There is no ``execute``, no ``http_get``, no ``eval``. A
  model that asks for a tool that does not exist gets an error naming the ones that do.
* Tools that read never mutate. Tools that mutate (scenario editing) touch only scenarios,
  never the world state, and never anything outside WorldGraph.
* No tool executes an operational change. V1 recommends; it does not act.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..analysis.business_impact import is_customer_facing
from ..analysis.correlation import (
    attack_paths,
    find_assets_near_event,
    match_vulnerable_assets,
    proximity_summary,
)
from ..models.core import HealthState
from ..services.world_state import WorldState
from ..simulation.engine import (
    SimulationError,
    new_scenario,
    override_for_capacity,
    override_for_health,
    touch,
)

logger = logging.getLogger("worldgraph.ai.tools")

#: Cap on any list a tool returns. Keeps a tool result inside a sane prompt budget and
#: stops one query from dumping the whole estate into a context window.
MAX_ROWS = 40


class ToolError(RuntimeError):
    """A tool could not run. The message is safe to show a user and a model."""


# --------------------------------------------------------------------------------------
# Argument schemas
# --------------------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityIdArgs(_Args):
    entity_id: str = Field(min_length=1, max_length=128)


class SearchArgs(_Args):
    query: str = Field(default="", max_length=128)
    entity_type: str | None = Field(default=None, max_length=64)
    criticality: str | None = Field(default=None, max_length=32)
    limit: int = Field(default=20, ge=1, le=MAX_ROWS)


class TraceArgs(_Args):
    entity_id: str = Field(min_length=1, max_length=128)
    max_depth: int = Field(default=4, ge=1, le=8)


class EventIdArgs(_Args):
    event_id: str = Field(min_length=1, max_length=192)


class RecentEventsArgs(_Args):
    limit: int = Field(default=10, ge=1, le=MAX_ROWS)
    minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    category: str | None = Field(default=None, max_length=48)


class BlastRadiusArgs(_Args):
    entity_ids: list[str] = Field(default_factory=list, max_length=10)
    event_id: str | None = Field(default=None, max_length=192)
    scenario_id: str | None = Field(default=None, max_length=128)


class NearEventArgs(_Args):
    event_id: str = Field(min_length=1, max_length=192)
    radius_km: float | None = Field(default=None, ge=0.0, le=5000.0)


class CreateSimulationArgs(_Args):
    name: str = Field(min_length=1, max_length=160)
    origin_event_id: str | None = Field(default=None, max_length=192)


class AddOverrideArgs(_Args):
    scenario_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=256)
    health: str | None = Field(default=None, max_length=32)
    capacity: float | None = Field(default=None, ge=0.0, le=1.0)


class RemoveOverrideArgs(_Args):
    scenario_id: str = Field(min_length=1, max_length=128)
    override_id: str = Field(min_length=1, max_length=128)


class ScenarioArgs(_Args):
    scenario_id: str = Field(min_length=1, max_length=128)


class PlanArgs(_Args):
    analysis_id: str | None = Field(default=None, max_length=128)
    scenario_id: str | None = Field(default=None, max_length=128)
    event_id: str | None = Field(default=None, max_length=192)


class FocusEntityArgs(_Args):
    entity_id: str = Field(min_length=1, max_length=128)


class FocusEventArgs(_Args):
    event_id: str = Field(min_length=1, max_length=192)


class PathArgs(_Args):
    from_entity_id: str = Field(min_length=1, max_length=128)
    to_entity_id: str = Field(min_length=1, max_length=128)


class AnnotateArgs(_Args):
    entity_ids: list[str] = Field(min_length=1, max_length=MAX_ROWS)
    label: str = Field(default="", max_length=120)


class VulnerabilityArgs(_Args):
    cve_id: str = Field(default="", max_length=64)
    product: str = Field(default="", max_length=128)


class AttackPathArgs(_Args):
    to_entity_id: str = Field(default="", max_length=128)
    from_entity_id: str = Field(default="internet", max_length=128)


class NoArgs(_Args):
    pass


# --------------------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tool:
    """One allowlisted capability."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict[str, Any]]
    #: ``read`` tools never change anything. ``write`` tools may edit a *scenario*, and
    #: nothing else. There is no ``execute`` class, by design.
    kind: str = "read"

    def json_schema(self) -> dict[str, Any]:
        """Anthropic-style tool definition for the model."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


@dataclass(slots=True)
class ToolContext:
    """What a tool is allowed to touch.

    Selected context (the entity/event the operator has open, the active scenario) is
    passed in rather than the model being handed the whole world — the "do not dump the
    application state into the prompt" rule made structural.
    """

    state: WorldState
    selected_entity_id: str | None = None
    selected_event_id: str | None = None
    active_scenario_id: str | None = None
    #: Camera-directive side effects a tool asked for; the frontend applies them.
    directives: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.directives is None:
            self.directives = []

    def emit(self, kind: str, **payload: Any) -> None:
        """Record a UI directive (fly camera, highlight path, annotate)."""
        self.directives.append({"kind": kind, **payload})


# -- handlers ---------------------------------------------------------------------------


def _tri_state(value: bool | None) -> str:
    """Render a tri-state for a model. ``None`` becomes an explicit UNKNOWN.

    A bare ``null`` in a tool result is routinely read as "no" or as zero. The word cannot
    be, which is the whole point of the Reality Pass changes.
    """
    if value is None:
        return "UNKNOWN"
    return "YES" if value else "NO"


def _entity_row(state: WorldState, entity_id: str) -> dict[str, Any]:
    entity = state.entity(entity_id)
    if entity is None:
        raise ToolError(f"No entity '{entity_id}' exists in the AtlasPay world model.")
    return {
        "id": entity.id,
        "name": entity.name,
        "type": entity.type.value,
        "criticality": entity.criticality.value,
        "health": entity.health.value,
        "region": entity.business.region,
        "location": entity.location.model_dump() if entity.location else None,
        # Explicit "UNKNOWN" rather than a bare null: a model reading `null` may render it
        # as 0, and 0 is a measurement. The string cannot be mistaken for one.
        "traffic_share": entity.business.traffic_share
        if entity.business.has_traffic
        else "UNKNOWN",
        "redundancy": entity.business.redundancy
        if entity.business.redundancy is not None
        else "UNKNOWN",
        "customer_facing": _tri_state(is_customer_facing(entity)),
        "internet_facing": entity.exposure.internet_facing,
        "software": [c.name for c in entity.software],
        "mode": entity.source.mode.value,
        "description": entity.description,
    }


def _get_selected_entity(ctx: ToolContext, _: BaseModel) -> dict[str, Any]:
    if not ctx.selected_entity_id:
        return {"selected": None, "note": "No entity is currently selected in the UI."}
    return {"selected": _entity_row(ctx.state, ctx.selected_entity_id)}


def _get_entity(ctx: ToolContext, args: EntityIdArgs) -> dict[str, Any]:
    row = _entity_row(ctx.state, args.entity_id)
    graph = ctx.state.graph
    row["depends_on"] = [
        {"id": e.target_entity_id, "type": e.type.value, "criticality": e.criticality}
        for e in graph.dependencies_of(args.entity_id)
    ][:MAX_ROWS]
    row["dependents"] = [
        {"id": e.source_entity_id, "type": e.type.value, "criticality": e.criticality}
        for e in graph.dependents_of(args.entity_id)
    ][:MAX_ROWS]
    return row


def _search_entities(ctx: ToolContext, args: SearchArgs) -> dict[str, Any]:
    query = args.query.strip().lower()
    rows = []
    for entity in ctx.state.entities():
        if query and query not in entity.name.lower() and query not in entity.id.lower():
            continue
        if args.entity_type and entity.type.value != args.entity_type.upper():
            continue
        if args.criticality and entity.criticality.value != args.criticality.upper():
            continue
        rows.append(_entity_row(ctx.state, entity.id))
    rows.sort(key=lambda r: (r["criticality"], r["name"]))
    return {"count": len(rows), "entities": rows[: args.limit]}


def _list_recent_events(ctx: ToolContext, args: RecentEventsArgs) -> dict[str, Any]:
    since = None
    if args.minutes is not None:
        from ..models.core import utcnow

        since = utcnow() - timedelta(minutes=args.minutes)
    events = ctx.state.events(limit=args.limit, since=since, category=args.category)
    return {"count": len(events), "events": [_event_row(ctx.state, e) for e in events]}


def _event_row(state: WorldState, event) -> dict[str, Any]:
    summary = proximity_summary(state.graph, event) if event.location else {}
    return {
        "id": event.id,
        "title": event.title,
        "category": event.category.value,
        "severity": event.severity.value,
        "occurred_at": event.occurred_at.isoformat(),
        "location": event.location.model_dump() if event.location else None,
        "exposure_radius_km": event.exposure_radius_km,
        "source": event.source.source_name,
        # The mode is repeated on every event row so a model summarizing a list cannot
        # lose track of which items are live and which are replayed fixtures.
        "mode": event.source.mode.value,
        "enterprise_proximity": summary,
        # External prose. Named as untrusted in the payload so the system prompt's rule
        # about it has something concrete to point at.
        "untrusted_description": event.description,
    }


def _get_event(ctx: ToolContext, args: EventIdArgs) -> dict[str, Any]:
    event = ctx.state.event(args.event_id)
    if event is None:
        raise ToolError(f"No event '{args.event_id}' is known to WorldGraph.")
    return _event_row(ctx.state, event)


def _trace_dependencies(ctx: ToolContext, args: TraceArgs) -> dict[str, Any]:
    if args.entity_id not in ctx.state.graph:
        raise ToolError(f"No entity '{args.entity_id}' exists in the AtlasPay world model.")
    result = ctx.state.graph.traverse_dependencies([args.entity_id], max_depth=args.max_depth)
    return {
        "origin": args.entity_id,
        "direction": "what this entity depends on",
        "truncated": result.truncated,
        "nodes": [
            {
                "id": step.entity_id,
                "name": ctx.state.graph.require_entity(step.entity_id).name,
                "depth": step.depth,
                "path": step.path,
            }
            for step in result.steps[:MAX_ROWS]
        ],
    }


def _trace_dependents(ctx: ToolContext, args: TraceArgs) -> dict[str, Any]:
    if args.entity_id not in ctx.state.graph:
        raise ToolError(f"No entity '{args.entity_id}' exists in the AtlasPay world model.")
    result = ctx.state.graph.traverse_dependents([args.entity_id], max_depth=args.max_depth)
    return {
        "origin": args.entity_id,
        "direction": "what depends on this entity",
        "truncated": result.truncated,
        "cycles": result.cycles_broken,
        "nodes": [
            {
                "id": step.entity_id,
                "name": ctx.state.graph.require_entity(step.entity_id).name,
                "depth": step.depth,
                "path": step.path,
            }
            for step in result.steps[:MAX_ROWS]
        ],
    }


def _blast_result_row(result) -> dict[str, Any]:
    return {
        "analysis_id": result.id,
        "origin": result.origin_label,
        "origin_ids": result.origin_ids,
        "severity": result.severity.value,
        "risk_score": result.risk.score,
        "risk_breakdown": [
            {"label": c.label, "points": c.points, "detail": c.detail}
            for c in result.risk.contributions
        ],
        "direct_impact": [
            {"id": r.entity_id, "name": r.entity_name, "availability": r.availability}
            for r in result.direct_impact[:MAX_ROWS]
        ],
        "indirect_impact": [
            {
                "id": r.entity_id,
                "name": r.entity_name,
                "availability": r.availability,
                "depth": r.depth,
            }
            for r in result.indirect_impact[:MAX_ROWS]
        ],
        "critical_paths": [p.as_text() for p in result.critical_paths],
        "customer_exposure": [
            {
                "region": c.region,
                "traffic_impact": c.traffic_impact,
                "customers": c.customer_count,
            }
            for c in result.customer_exposure
        ],
        "business_impact": result.business_impact.model_dump(mode="json"),
        "explanations": result.explanations,
        "confidence": result.confidence.model_dump(mode="json"),
        "truncated": result.truncated,
        "mode": result.mode.value,
    }


def _calculate_blast_radius(ctx: ToolContext, args: BlastRadiusArgs) -> dict[str, Any]:
    from ..analysis.blast_radius import calculate_blast_radius as compute

    if args.event_id:
        return _blast_result_row(ctx.state.analyze_event(args.event_id))
    if args.scenario_id:
        scenario = ctx.state.scenario(args.scenario_id)
        if scenario is None:
            raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
        comparison = ctx.state.compare_scenario(scenario)
        if comparison.blast_radius is None:
            raise ToolError("That scenario has no failures in it yet, so it has no blast radius.")
        return _blast_result_row(comparison.blast_radius)
    ids = args.entity_ids or ([ctx.selected_entity_id] if ctx.selected_entity_id else [])
    ids = [eid for eid in ids if eid]
    if not ids:
        raise ToolError(
            "calculate_blast_radius needs entity_ids, an event_id or a scenario_id, and "
            "nothing is currently selected."
        )
    unknown = [eid for eid in ids if eid not in ctx.state.graph]
    if unknown:
        raise ToolError(f"Unknown entities: {', '.join(unknown)}.")
    result = compute(ctx.state.graph, origin_ids=ids, origin_kind="ENTITY")
    ctx.state._analyses[result.id] = result
    return _blast_result_row(result)


def _find_assets_near_event(ctx: ToolContext, args: NearEventArgs) -> dict[str, Any]:
    event = ctx.state.event(args.event_id)
    if event is None:
        raise ToolError(f"No event '{args.event_id}' is known to WorldGraph.")
    matches = find_assets_near_event(ctx.state.graph, event, radius_km=args.radius_km)
    return {
        "event_id": event.id,
        "radius_km": args.radius_km or event.exposure_radius_km,
        "count": len(matches),
        "assets": [
            {
                "id": m.entity.id,
                "name": m.entity.name,
                "type": m.entity.type.value,
                "distance_km": round(m.distance_km, 1),
                "direction": m.direction,
                "proximity": round(m.proximity, 3),
            }
            for m in matches[:MAX_ROWS]
        ],
    }


def _create_simulation(ctx: ToolContext, args: CreateSimulationArgs) -> dict[str, Any]:
    scenario = new_scenario(args.name, origin_event_id=args.origin_event_id)
    ctx.state.put_scenario(scenario)
    ctx.active_scenario_id = scenario.id
    ctx.emit("simulation_mode", scenario_id=scenario.id, active=True)
    return {
        "scenario_id": scenario.id,
        "name": scenario.name,
        "overrides": [],
        "note": "Simulation created. Nothing in the real world state has changed.",
    }


def _add_simulation_override(ctx: ToolContext, args: AddOverrideArgs) -> dict[str, Any]:
    scenario = ctx.state.scenario(args.scenario_id)
    if scenario is None:
        raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
    if args.health is not None:
        try:
            health = HealthState(args.health.upper())
        except ValueError as error:
            raise ToolError(
                f"'{args.health}' is not a health state. Valid: "
                + ", ".join(h.value for h in HealthState)
            ) from error
        override = override_for_health(args.target_id, health)
    elif args.capacity is not None:
        override = override_for_capacity(args.target_id, args.capacity)
    else:
        raise ToolError("An override needs either a health state or a capacity value.")

    try:
        from ..simulation.engine import validate_override

        validate_override(ctx.state.graph, override)
    except SimulationError as error:
        raise ToolError(str(error)) from error

    scenario.overrides.append(override)
    ctx.state.put_scenario(touch(scenario))
    return {
        "scenario_id": scenario.id,
        "added": override.describe(),
        "override_id": override.id,
        "overrides": [o.describe() for o in scenario.overrides],
        "note": "This is a hypothetical. The real world state is unchanged.",
    }


def _remove_simulation_override(ctx: ToolContext, args: RemoveOverrideArgs) -> dict[str, Any]:
    scenario = ctx.state.scenario(args.scenario_id)
    if scenario is None:
        raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
    before = len(scenario.overrides)
    scenario.overrides = [o for o in scenario.overrides if o.id != args.override_id]
    if len(scenario.overrides) == before:
        raise ToolError(f"Scenario '{scenario.id}' has no override '{args.override_id}'.")
    ctx.state.put_scenario(touch(scenario))
    return {"scenario_id": scenario.id, "overrides": [o.describe() for o in scenario.overrides]}


def _reset_simulation(ctx: ToolContext, args: ScenarioArgs) -> dict[str, Any]:
    scenario = ctx.state.scenario(args.scenario_id)
    if scenario is None:
        raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
    scenario.overrides = []
    ctx.state.put_scenario(touch(scenario))
    ctx.emit("simulation_mode", scenario_id=scenario.id, active=False)
    return {"scenario_id": scenario.id, "overrides": [], "note": "Scenario reset to baseline."}


def _compare_simulation(ctx: ToolContext, args: ScenarioArgs) -> dict[str, Any]:
    scenario = ctx.state.scenario(args.scenario_id)
    if scenario is None:
        raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
    comparison = ctx.state.compare_scenario(scenario)
    return {
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "overrides": [o.describe() for o in scenario.overrides],
        "baseline": comparison.baseline.model_dump(mode="json"),
        "simulated": comparison.simulated.model_dump(mode="json"),
        "deltas": [d.model_dump(mode="json") for d in comparison.deltas],
        "newly_impacted": [
            {"id": r.entity_id, "name": r.entity_name, "availability": r.availability}
            for r in comparison.newly_impacted[:MAX_ROWS]
        ],
        "cascade_paths": [p.as_text() for p in comparison.cascade_paths],
        "analysis_id": comparison.blast_radius.id if comparison.blast_radius else None,
        "note": "SIMULATED — modelled estimate over a hypothetical world state.",
    }


def _generate_response_plan(ctx: ToolContext, args: PlanArgs) -> dict[str, Any]:
    result = None
    scenario_name = None
    if args.analysis_id:
        result = ctx.state.analysis(args.analysis_id)
        if result is None:
            raise ToolError(f"No analysis '{args.analysis_id}' exists.")
    elif args.scenario_id:
        scenario = ctx.state.scenario(args.scenario_id)
        if scenario is None:
            raise ToolError(f"No simulation scenario '{args.scenario_id}' exists.")
        comparison = ctx.state.compare_scenario(scenario)
        result = comparison.blast_radius
        scenario_name = scenario.name
        if result is None:
            raise ToolError("That scenario has no failures in it, so there is nothing to plan for.")
    elif args.event_id:
        result = ctx.state.analyze_event(args.event_id)
    else:
        recent = ctx.state.recent_analyses(limit=1)
        if not recent:
            raise ToolError(
                "No analysis has been run yet. Analyse an event or a scenario first."
            )
        result = recent[0]

    plan = ctx.state.build_plan(result, scenario_name=scenario_name)
    return {
        "plan_id": plan.id,
        "summary": plan.summary,
        "objectives": plan.objectives,
        "actions": [a.model_dump(mode="json") for a in plan.actions],
        "assumptions": plan.assumptions,
        "unresolved_questions": plan.unresolved_questions,
        "note": (
            "Every action is a RECOMMENDATION. WorldGraph has not executed and will not "
            "execute any of them."
        ),
    }


def _focus_entity(ctx: ToolContext, args: FocusEntityArgs) -> dict[str, Any]:
    entity = ctx.state.entity(args.entity_id)
    if entity is None:
        raise ToolError(f"No entity '{args.entity_id}' exists in the AtlasPay world model.")
    if entity.location is None:
        raise ToolError(f"{entity.name} has no location, so the camera cannot fly to it.")
    ctx.emit("focus_entity", entity_id=entity.id)
    return {"focused": entity.id, "name": entity.name}


def _focus_event(ctx: ToolContext, args: FocusEventArgs) -> dict[str, Any]:
    event = ctx.state.event(args.event_id)
    if event is None:
        raise ToolError(f"No event '{args.event_id}' is known to WorldGraph.")
    if event.location is None:
        raise ToolError(f"{event.title} has no location, so the camera cannot fly to it.")
    ctx.emit("focus_event", event_id=event.id)
    return {"focused": event.id, "title": event.title}


def _show_dependency_path(ctx: ToolContext, args: PathArgs) -> dict[str, Any]:
    graph = ctx.state.graph
    path = graph.shortest_dependency_path(args.from_entity_id, args.to_entity_id)
    direction = "depends-on"
    if path is None:
        path = graph.impact_path(args.from_entity_id, args.to_entity_id)
        direction = "impact"
    if path is None:
        raise ToolError(
            f"No dependency path connects {args.from_entity_id} and {args.to_entity_id}."
        )
    ctx.emit("show_path", path=path)
    return {
        "path": path,
        "direction": direction,
        "names": [graph.require_entity(node).name for node in path],
    }


def _annotate_entities(ctx: ToolContext, args: AnnotateArgs) -> dict[str, Any]:
    unknown = [eid for eid in args.entity_ids if eid not in ctx.state.graph]
    if unknown:
        raise ToolError(f"Unknown entities: {', '.join(unknown)}.")
    ctx.emit("annotate", entity_ids=args.entity_ids, label=args.label)
    return {"annotated": args.entity_ids, "label": args.label}


def _get_vulnerability_exposure(ctx: ToolContext, args: VulnerabilityArgs) -> dict[str, Any]:
    products = [args.product] if args.product else []
    matches = match_vulnerable_assets(ctx.state.graph, cve_id=args.cve_id, product_names=products)
    if not matches:
        return {
            "cve_id": args.cve_id,
            "count": 0,
            "assets": [],
            "note": "No AtlasPay asset runs software matching that vulnerability.",
        }
    # Counted apart, because they are different facts. `count` stays the confirmed total:
    # it is what the model and the router quote, and a product-name collision quoted as a
    # finding is exactly the noise this distinction exists to prevent.
    confirmed = [m for m in matches if m.is_confirmed]
    potential = [m for m in matches if not m.is_confirmed]
    return {
        "cve_id": args.cve_id,
        "count": len(confirmed),
        "unverified_count": len(potential),
        "internet_facing_count": sum(1 for m in confirmed if m.internet_facing),
        "assets": [
            {
                "id": m.entity.id,
                "name": m.entity.name,
                "component": f"{m.component_name} {m.component_version}".strip(),
                "assessment": m.assessment.value,
                "internet_facing": m.internet_facing,
                "network_zone": m.entity.exposure.network_zone,
            }
            for m in (confirmed + potential)[:MAX_ROWS]
        ],
    }


def _get_attack_paths(ctx: ToolContext, args: AttackPathArgs) -> dict[str, Any]:
    targets = [args.to_entity_id] if args.to_entity_id else None
    paths = attack_paths(
        ctx.state.graph, from_entity_id=args.from_entity_id, to_entity_ids=targets
    )
    graph = ctx.state.graph
    return {
        "from": args.from_entity_id,
        "to": args.to_entity_id or "any reachable asset",
        "count": len(paths),
        "established_count": sum(1 for path in paths if not path.relies_on_inference),
        "inferred_count": sum(1 for path in paths if path.relies_on_inference),
        "paths": [
            {
                "ids": path.nodes,
                "names": [
                    graph.entity(node).name if graph.entity(node) else node
                    for node in path.nodes
                ],
                # The model must not be able to present an inferred path as an established
                # one, so the basis travels with the path rather than in a preamble it can
                # drop.
                "basis": path.basis,
                "confidence": round(path.confidence, 2),
                "inferred_hops": [
                    hop.evidence for hop in path.hops if hop.is_inferred
                ],
            }
            for path in paths[:10]
        ],
        "note": (
            "Reachability, not proof of exploitability. WorldGraph models declared network "
            "adjacency; it does not test controls."
        ),
    }


def _get_world_status(ctx: ToolContext, _: BaseModel) -> dict[str, Any]:
    """Dashboard counters, feed health and standing risks — the "what can hurt us" seed."""
    return {
        "dashboard": ctx.state.dashboard(),
        "baseline": ctx.state.baseline_metrics().model_dump(mode="json"),
        "material_risks": [
            {
                "id": r.id,
                "title": r.title,
                "severity": r.severity.value,
                "summary": r.summary,
                "score": r.score,
                "focus_entity_ids": r.focus_entity_ids,
            }
            for r in ctx.state.material_risks()
        ],
        "feeds": [f.model_dump(mode="json") for f in ctx.state.feed_statuses()],
    }


def _get_recent_changes(ctx: ToolContext, args: RecentEventsArgs) -> dict[str, Any]:
    window = timedelta(minutes=args.minutes or 60)
    changes = ctx.state.changes_since(window)
    return {
        "window_minutes": changes["window_minutes"],
        "new_events": [_event_row(ctx.state, e) for e in changes["new_events"]],  # type: ignore[union-attr]
        "timeline": [
            {"at": entry.at.isoformat(), "stage": entry.stage, "message": entry.message}
            for entry in changes["timeline"]  # type: ignore[union-attr]
        ],
        "feeds": [f.model_dump(mode="json") for f in changes["feed_statuses"]],  # type: ignore[union-attr]
    }


# --------------------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------------------

TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> None:
    if tool.name in TOOLS:
        raise RuntimeError(f"duplicate tool '{tool.name}'")
    TOOLS[tool.name] = tool


for _tool in (
    Tool(
        "get_selected_entity",
        "Return the entity the operator currently has selected in the UI, if any.",
        NoArgs,
        _get_selected_entity,
    ),
    Tool(
        "get_entity",
        "Return one entity by id, including what it depends on and what depends on it.",
        EntityIdArgs,
        _get_entity,
    ),
    Tool(
        "search_entities",
        "Search AtlasPay entities by name fragment, type and criticality.",
        SearchArgs,
        _search_entities,
    ),
    Tool(
        "list_recent_events",
        "List recent world events (earthquakes, outages, vulnerabilities), newest first.",
        RecentEventsArgs,
        _list_recent_events,
    ),
    Tool("get_event", "Return one world event by id with its enterprise proximity.", EventIdArgs, _get_event),
    Tool(
        "trace_dependencies",
        "Walk what an entity depends on, transitively. Returns paths, not just ids.",
        TraceArgs,
        _trace_dependencies,
    ),
    Tool(
        "trace_dependents",
        "Walk what depends on an entity, transitively — the failure-propagation direction.",
        TraceArgs,
        _trace_dependents,
    ),
    Tool(
        "calculate_blast_radius",
        "Run the deterministic blast-radius engine for entities, an event, or a scenario. "
        "Returns direct and indirect impact, critical paths, customer exposure, a scored "
        "risk with its full derivation, and confidence.",
        BlastRadiusArgs,
        _calculate_blast_radius,
    ),
    Tool(
        "find_assets_near_event",
        "Find AtlasPay assets inside an event's exposure radius, with distance and bearing.",
        NearEventArgs,
        _find_assets_near_event,
    ),
    Tool(
        "create_simulation",
        "Create an empty what-if scenario. Does not change the real world state.",
        CreateSimulationArgs,
        _create_simulation,
        kind="write",
    ),
    Tool(
        "add_simulation_override",
        "Add a hypothetical failure to a scenario: set an entity's health (HEALTHY, "
        "DEGRADED, SEVERELY_DEGRADED, DOWN) or its capacity (0-1).",
        AddOverrideArgs,
        _add_simulation_override,
        kind="write",
    ),
    Tool(
        "remove_simulation_override",
        "Remove one override from a scenario.",
        RemoveOverrideArgs,
        _remove_simulation_override,
        kind="write",
    ),
    Tool("reset_simulation", "Clear every override from a scenario.", ScenarioArgs, _reset_simulation, kind="write"),
    Tool(
        "compare_simulation",
        "Compare a scenario against the real baseline: availability, regional capacity, "
        "critical services, customer regions, revenue at risk, and cascading paths.",
        ScenarioArgs,
        _compare_simulation,
    ),
    Tool(
        "generate_response_plan",
        "Generate a structured response plan from an analysis, scenario or event. Every "
        "action carries a rationale. Nothing is executed.",
        PlanArgs,
        _generate_response_plan,
    ),
    Tool("focus_entity", "Fly the operator's camera to an entity.", FocusEntityArgs, _focus_entity),
    Tool("focus_event", "Fly the operator's camera to a world event.", FocusEventArgs, _focus_event),
    Tool(
        "show_dependency_path",
        "Highlight the dependency path between two entities on the globe.",
        PathArgs,
        _show_dependency_path,
    ),
    Tool("annotate_entities", "Highlight a set of entities on the globe.", AnnotateArgs, _annotate_entities),
    Tool(
        "get_vulnerability_exposure",
        "Find AtlasPay assets running software affected by a CVE, and which are internet-facing.",
        VulnerabilityArgs,
        _get_vulnerability_exposure,
    ),
    Tool(
        "get_attack_paths",
        "Trace network reachability from the public internet to an asset.",
        AttackPathArgs,
        _get_attack_paths,
    ),
    Tool(
        "get_world_status",
        "Dashboard counters, baseline metrics, standing material risks and feed health.",
        NoArgs,
        _get_world_status,
    ),
    Tool(
        "get_recent_changes",
        "What changed in a recent time window: new events, timeline entries, feed states.",
        RecentEventsArgs,
        _get_recent_changes,
    ),
):
    _register(_tool)


def tool_definitions() -> list[dict[str, Any]]:
    """Every tool as a model-facing definition."""
    return [tool.json_schema() for tool in TOOLS.values()]


def run_tool(ctx: ToolContext, name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
    """Execute one allowlisted tool with validated arguments.

    Raises :class:`ToolError` for an unknown tool, invalid arguments, or a handler failure.
    Every failure message is safe to return to a model and to a user — no stack traces, no
    internal paths, no secrets.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(
            f"'{name}' is not an available tool. Available: {', '.join(sorted(TOOLS))}."
        )
    try:
        args = tool.args_model.model_validate(raw_args or {})
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or 'input'}: {item['msg']}"
            for item in error.errors()[:4]
        )
        raise ToolError(f"Invalid arguments for {name} — {details}") from error

    logger.info("ai_tool_call tool=%s kind=%s", name, tool.kind)
    try:
        return tool.handler(ctx, args)
    except ToolError:
        raise
    except KeyError as error:
        raise ToolError(f"{name} could not find {error}.") from error
    except SimulationError as error:
        raise ToolError(str(error)) from error
    except Exception as error:
        logger.exception("ai_tool_failed tool=%s", name)
        raise ToolError(f"{name} failed: {type(error).__name__}.") from error
