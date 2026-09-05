"""HTTP API.

Shape follows the product, not the storage: ``/world`` for the estate, ``/events`` for
world signals, ``/analysis`` for blast radius and plans, ``/simulation`` for what-ifs,
``/ai`` for the analyst.

Every response is a Pydantic model or a dict of them, so the frontend's types are generated
from something real rather than hand-written. Errors carry a specific, actionable message
— "USGS feed unavailable, showing cached data from 18 minutes ago" rather than "something
went wrong".
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ..ai.analyst import analyst_status
from ..analysis.blast_radius import calculate_blast_radius
from ..analysis.correlation import (
    attack_paths,
    find_assets_near_event,
    match_vulnerable_assets,
    proximity_summary,
)
from ..config import Settings, get_settings
from ..models.analysis import (
    BlastRadiusResult,
    MaterialRisk,
    OverrideKind,
    ResponsePlan,
    SimulationComparison,
    SimulationOverride,
    SimulationScenario,
    TimelineEntry,
    WorldSnapshotMetrics,
)
from ..models.core import (
    DependencyEdge,
    FeedStatus,
    HealthState,
    WorldEntity,
    WorldEvent,
    utcnow,
)
from ..services.world_state import WorldState
from ..simulation.engine import (
    SimulationError,
    new_scenario,
    override_for_capacity,
    override_for_health,
    touch,
)
from .deps import get_analyst, get_state, rate_limit

logger = logging.getLogger("worldgraph.api")

router = APIRouter()

analysis_limit = Depends(rate_limit("analysis"))
ai_limit = Depends(rate_limit("ai"))


# --------------------------------------------------------------------------------------
# Request / response bodies
# --------------------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlastRadiusRequest(_Body):
    entity_ids: list[str] = Field(default_factory=list, max_length=20)
    event_id: str | None = Field(default=None, max_length=192)
    max_depth: int = Field(default=12, ge=1, le=12)


class CreateScenarioRequest(_Body):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1024)
    origin_event_id: str | None = Field(default=None, max_length=192)


class OverrideRequest(_Body):
    target_id: str = Field(min_length=1, max_length=256)
    kind: OverrideKind = OverrideKind.ENTITY_HEALTH
    health: HealthState | None = None
    capacity: float | None = Field(default=None, ge=0.0, le=1.0)
    note: str = Field(default="", max_length=256)


class PlanRequest(_Body):
    analysis_id: str | None = Field(default=None, max_length=128)
    scenario_id: str | None = Field(default=None, max_length=128)
    event_id: str | None = Field(default=None, max_length=192)


class AskRequest(_Body):
    message: str = Field(min_length=1, max_length=2000)
    selected_entity_id: str | None = Field(default=None, max_length=128)
    selected_event_id: str | None = Field(default=None, max_length=192)
    active_scenario_id: str | None = Field(default=None, max_length=128)


class AskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    engine: Literal["deterministic", "model"]
    tool_calls: list[dict[str, Any]]
    directives: list[dict[str, Any]]
    degraded_reason: str | None = None
    active_scenario_id: str | None = None
    duration_ms: float


class WorldGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[WorldEntity]
    edges: list[DependencyEdge]
    metrics: WorldSnapshotMetrics


class EntityDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: WorldEntity
    depends_on: list[DependencyEdge]
    dependents: list[DependencyEdge]
    hosts: list[str]
    availability: float
    capacity: float
    recent_event_ids: list[str]
    risk_count: int


# --------------------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------------------


@router.get("/health", tags=["meta"])
def health(state: WorldState = Depends(get_state)) -> dict[str, Any]:
    """Liveness plus a one-glance view of feed health."""
    feeds = state.feed_statuses()
    return {
        "status": "ok",
        "mode": state.settings.run_mode.value,
        "entities": len(state.graph),
        "feeds": [
            {"id": f.adapter_id, "state": f.state.value, "records": f.record_count}
            for f in feeds
        ],
        "at": utcnow().isoformat(),
    }


@router.get("/config", tags=["meta"])
def config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Client-visible configuration. Contains no secrets except opt-in tile tokens."""
    return {**settings.public_config(), "analyst": analyst_status(settings)}


@router.get("/dashboard", tags=["meta"])
def dashboard(state: WorldState = Depends(get_state)) -> dict[str, Any]:
    """The headline counters."""
    return state.dashboard()


@router.get("/feeds", response_model=list[FeedStatus], tags=["meta"])
def feeds(state: WorldState = Depends(get_state)) -> list[FeedStatus]:
    """Every adapter's honest state and freshness."""
    return state.feed_statuses()


@router.post("/feeds/refresh", response_model=list[FeedStatus], tags=["meta"])
async def refresh_feeds(
    state: WorldState = Depends(get_state), _: None = analysis_limit
) -> list[FeedStatus]:
    """Force a refresh of every adapter."""
    return await state.refresh_feeds()


@router.get("/timeline", response_model=list[TimelineEntry], tags=["meta"])
def timeline(
    limit: int = Query(default=60, ge=1, le=300),
    event_id: str | None = Query(default=None, max_length=192),
    state: WorldState = Depends(get_state),
) -> list[TimelineEntry]:
    """The incident timeline, newest first."""
    return state.timeline(limit=limit, event_id=event_id)


# --------------------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------------------


@router.get("/world", response_model=WorldGraphResponse, tags=["world"])
def world(state: WorldState = Depends(get_state)) -> WorldGraphResponse:
    """The whole estate. Small enough to send at once at V1 scale (~40 entities)."""
    return WorldGraphResponse(
        entities=state.entities(),
        edges=state.graph.edges,
        metrics=state.baseline_metrics(),
    )


@router.get("/world/entities/{entity_id}", response_model=EntityDetail, tags=["world"])
def entity_detail(
    entity_id: str, state: WorldState = Depends(get_state)
) -> EntityDetail:
    """Everything the entity panel needs, in one round trip."""
    entity = state.entity(entity_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No entity '{entity_id}' exists in the AtlasPay world model.",
        )
    from ..analysis.propagation import propagate

    settled = propagate(state.graph)
    recent = [
        event.id
        for event in state.events(limit=50)
        if entity_id in event.directly_named_entity_ids
        or (
            event.location is not None
            and entity.location is not None
            and any(m.entity.id == entity_id for m in find_assets_near_event(state.graph, event))
        )
    ]
    risks = [
        risk for risk in state.material_risks() if entity_id in risk.focus_entity_ids
    ]
    return EntityDetail(
        entity=entity,
        depends_on=state.graph.dependencies_of(entity_id),
        dependents=state.graph.dependents_of(entity_id),
        hosts=[e.id for e in state.graph.hosted_entities(entity_id)],
        availability=round(settled.availability.get(entity_id, 1.0), 4),
        capacity=round(settled.capacity.get(entity_id, 1.0), 4),
        recent_event_ids=recent[:10],
        risk_count=len(risks),
    )


@router.get("/world/risks", response_model=list[MaterialRisk], tags=["world"])
def risks(state: WorldState = Depends(get_state)) -> list[MaterialRisk]:
    """Standing material risks for executive mode."""
    return state.material_risks()


@router.get("/world/trace/{entity_id}", tags=["world"])
def trace(
    entity_id: str,
    direction: Literal["dependencies", "dependents"] = Query(default="dependents"),
    max_depth: int = Query(default=4, ge=1, le=8),
    state: WorldState = Depends(get_state),
) -> dict[str, Any]:
    """Traverse the graph in either direction, with explanation paths."""
    if entity_id not in state.graph:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No entity '{entity_id}' exists in the AtlasPay world model.",
        )
    walk = (
        state.graph.traverse_dependents
        if direction == "dependents"
        else state.graph.traverse_dependencies
    )
    result = walk([entity_id], max_depth=max_depth)
    return {
        "origin": entity_id,
        "direction": direction,
        "truncated": result.truncated,
        "truncation_reason": result.truncation_reason,
        "cycles": result.cycles_broken,
        "steps": [
            {
                "entity_id": step.entity_id,
                "depth": step.depth,
                "path": step.path,
                "edge_types": [t.value for t in step.edge_types],
            }
            for step in result.steps
        ],
    }


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


@router.get("/events", response_model=list[WorldEvent], tags=["events"])
def events(
    limit: int = Query(default=50, ge=1, le=200),
    minutes: int | None = Query(default=None, ge=1, le=60 * 24 * 90),
    category: str | None = Query(default=None, max_length=48),
    state: WorldState = Depends(get_state),
) -> list[WorldEvent]:
    """Recent world events, newest first."""
    since = utcnow() - timedelta(minutes=minutes) if minutes else None
    return state.events(limit=limit, since=since, category=category)


@router.get("/events/scenarios", tags=["events"])
def scenarios_catalog(state: WorldState = Depends(get_state)) -> list[dict[str, Any]]:
    """The replay scenarios offered in first-run and the scenario tray."""
    return state.replay_scenarios()


@router.get("/events/{event_id}", tags=["events"])
def event_detail(event_id: str, state: WorldState = Depends(get_state)) -> dict[str, Any]:
    """One event plus its enterprise proximity and the assets inside its radius."""
    event = state.event(event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No event '{event_id}' is known to WorldGraph.",
        )
    matches = find_assets_near_event(state.graph, event)
    vulnerable = []
    if event.category.value == "SECURITY_VULNERABILITY":
        raw = event.metadata.get("product_names")
        vulnerable = [
            {
                "entity_id": m.entity.id,
                "name": m.entity.name,
                "component": m.component_name,
                "internet_facing": m.internet_facing,
            }
            for m in match_vulnerable_assets(
                state.graph,
                cve_id=str(event.metadata.get("cve_id") or ""),
                product_names=[str(p) for p in raw] if isinstance(raw, list) else [],
            )
        ]
    return {
        "event": event.model_dump(mode="json"),
        "proximity": proximity_summary(state.graph, event) if event.location else {},
        "assets_in_radius": [
            {
                "entity_id": m.entity.id,
                "name": m.entity.name,
                "type": m.entity.type.value,
                "distance_km": round(m.distance_km, 1),
                "direction": m.direction,
                "proximity": round(m.proximity, 3),
            }
            for m in matches
        ],
        "vulnerable_assets": vulnerable,
        "freshness_seconds": event.source.freshness_seconds(),
    }


# --------------------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------------------


@router.post("/analysis/blast-radius", response_model=BlastRadiusResult, tags=["analysis"])
def blast_radius(
    body: BlastRadiusRequest,
    state: WorldState = Depends(get_state),
    _: None = analysis_limit,
) -> BlastRadiusResult:
    """Run the deterministic blast-radius engine."""
    if body.event_id:
        try:
            return state.analyze_event(body.event_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No event '{body.event_id}' is known to WorldGraph.",
            ) from error
    if not body.entity_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide either entity_ids or an event_id.",
        )
    unknown = [eid for eid in body.entity_ids if eid not in state.graph]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown entities: {', '.join(unknown)}.",
        )
    result = calculate_blast_radius(
        state.graph,
        origin_ids=body.entity_ids,
        origin_kind="ENTITY",
        max_depth=body.max_depth,
    )
    state.repository.save_analysis(result)
    state._analyses[result.id] = result
    return result


@router.get("/analysis/{analysis_id}", response_model=BlastRadiusResult, tags=["analysis"])
def analysis(analysis_id: str, state: WorldState = Depends(get_state)) -> BlastRadiusResult:
    result = state.analysis(analysis_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No analysis '{analysis_id}' exists."
        )
    return result


@router.post("/analysis/response-plan", response_model=ResponsePlan, tags=["analysis"])
def response_plan(
    body: PlanRequest,
    state: WorldState = Depends(get_state),
    _: None = analysis_limit,
) -> ResponsePlan:
    """Generate a structured response plan. Recommendations only — nothing is executed."""
    result = None
    scenario_name = None
    if body.analysis_id:
        result = state.analysis(body.analysis_id)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No analysis '{body.analysis_id}' exists.",
            )
    elif body.scenario_id:
        scenario = state.scenario(body.scenario_id)
        if scenario is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No simulation scenario '{body.scenario_id}' exists.",
            )
        comparison = state.compare_scenario(scenario)
        result = comparison.blast_radius
        scenario_name = scenario.name
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That scenario contains no failures, so there is nothing to plan for.",
            )
    elif body.event_id:
        try:
            result = state.analyze_event(body.event_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No event '{body.event_id}' is known to WorldGraph.",
            ) from error
    else:
        recent = state.recent_analyses(limit=1)
        if not recent:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Nothing has been analysed yet. Analyse an event or a scenario first.",
            )
        result = recent[0]
    return state.build_plan(result, scenario_name=scenario_name)


@router.get("/analysis/security/attack-paths", tags=["analysis"])
def security_attack_paths(
    to_entity_id: str = Query(default="", max_length=128),
    from_entity_id: str = Query(default="internet", max_length=128),
    state: WorldState = Depends(get_state),
) -> dict[str, Any]:
    """Network reachability from an origin (default: the public internet)."""
    paths = attack_paths(
        state.graph,
        from_entity_id=from_entity_id,
        to_entity_ids=[to_entity_id] if to_entity_id else None,
    )
    return {
        "from": from_entity_id,
        "to": to_entity_id or None,
        "count": len(paths),
        "paths": [
            {
                "ids": path,
                "names": [
                    (entity.name if (entity := state.entity(node)) else node) for node in path
                ],
            }
            for path in paths[:20]
        ],
        "disclaimer": (
            "Reachability, not exploitability. WorldGraph models declared network adjacency; "
            "it does not test authentication, network policy or whether an exploit works."
        ),
    }


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------


@router.get("/simulation", response_model=list[SimulationScenario], tags=["simulation"])
def list_scenarios(state: WorldState = Depends(get_state)) -> list[SimulationScenario]:
    return state.scenarios()


@router.post("/simulation", response_model=SimulationScenario, tags=["simulation"])
def create_scenario(
    body: CreateScenarioRequest, state: WorldState = Depends(get_state)
) -> SimulationScenario:
    """Create an empty what-if scenario. The real world state is untouched."""
    scenario = new_scenario(
        body.name, description=body.description, origin_event_id=body.origin_event_id
    )
    return state.put_scenario(scenario)


@router.get("/simulation/{scenario_id}", response_model=SimulationScenario, tags=["simulation"])
def get_scenario(scenario_id: str, state: WorldState = Depends(get_state)) -> SimulationScenario:
    scenario = state.scenario(scenario_id)
    if scenario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No simulation scenario '{scenario_id}' exists.",
        )
    return scenario


@router.post(
    "/simulation/{scenario_id}/overrides",
    response_model=SimulationScenario,
    tags=["simulation"],
)
def add_override(
    scenario_id: str,
    body: OverrideRequest,
    state: WorldState = Depends(get_state),
) -> SimulationScenario:
    """Add one hypothetical failure to a scenario."""
    scenario = _require_scenario(state, scenario_id)
    if body.kind is OverrideKind.ENTITY_CAPACITY:
        if body.capacity is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A capacity override needs a capacity value between 0 and 1.",
            )
        override = override_for_capacity(body.target_id, body.capacity, note=body.note)
    elif body.kind is OverrideKind.EDGE_DISABLED:
        override = SimulationOverride(
            id=f"ovr-{utcnow().timestamp():.0f}-{body.target_id[:16]}",
            kind=OverrideKind.EDGE_DISABLED,
            target_id=body.target_id,
            note=body.note,
        )
    else:
        if body.health is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A health override needs a health state.",
            )
        override = override_for_health(body.target_id, body.health, note=body.note)

    from ..simulation.engine import validate_override

    try:
        validate_override(state.graph, override)
    except SimulationError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    scenario.overrides.append(override)
    return state.put_scenario(touch(scenario))


@router.delete(
    "/simulation/{scenario_id}/overrides/{override_id}",
    response_model=SimulationScenario,
    tags=["simulation"],
)
def remove_override(
    scenario_id: str, override_id: str, state: WorldState = Depends(get_state)
) -> SimulationScenario:
    scenario = _require_scenario(state, scenario_id)
    before = len(scenario.overrides)
    scenario.overrides = [o for o in scenario.overrides if o.id != override_id]
    if len(scenario.overrides) == before:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scenario '{scenario_id}' has no override '{override_id}'.",
        )
    return state.put_scenario(touch(scenario))


@router.post(
    "/simulation/{scenario_id}/reset", response_model=SimulationScenario, tags=["simulation"]
)
def reset_scenario(scenario_id: str, state: WorldState = Depends(get_state)) -> SimulationScenario:
    scenario = _require_scenario(state, scenario_id)
    scenario.overrides = []
    return state.put_scenario(touch(scenario))


@router.delete("/simulation/{scenario_id}", status_code=204, tags=["simulation"])
def delete_scenario(scenario_id: str, state: WorldState = Depends(get_state)) -> None:
    _require_scenario(state, scenario_id)
    state.drop_scenario(scenario_id)


@router.get(
    "/simulation/{scenario_id}/compare",
    response_model=SimulationComparison,
    tags=["simulation"],
)
def compare(
    scenario_id: str,
    state: WorldState = Depends(get_state),
    _: None = analysis_limit,
) -> SimulationComparison:
    """Baseline vs simulated, with cascading paths and a blast radius."""
    scenario = _require_scenario(state, scenario_id)
    try:
        return state.compare_scenario(scenario)
    except SimulationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


def _require_scenario(state: WorldState, scenario_id: str) -> SimulationScenario:
    scenario = state.scenario(scenario_id)
    if scenario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No simulation scenario '{scenario_id}' exists.",
        )
    return scenario


# --------------------------------------------------------------------------------------
# AI
# --------------------------------------------------------------------------------------


@router.get("/ai/status", tags=["ai"])
def ai_status(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Which analyst backend is active, so the UI can label itself honestly."""
    return analyst_status(settings)


@router.get("/ai/tools", tags=["ai"])
def ai_tools() -> list[dict[str, Any]]:
    """The allowlisted tool surface. Public because it is a transparency feature."""
    from ..ai.tools import tool_definitions

    return tool_definitions()


@router.post("/ai/ask", response_model=AskResponse, tags=["ai"])
async def ask(
    body: AskRequest = Body(...),
    analyst=Depends(get_analyst),
    _: None = ai_limit,
) -> AskResponse:
    """Ask the analyst a question."""
    answer = await analyst.ask(
        body.message,
        selected_entity_id=body.selected_entity_id,
        selected_event_id=body.selected_event_id,
        active_scenario_id=body.active_scenario_id,
    )
    return AskResponse(
        answer=answer.answer,
        engine=answer.engine,  # type: ignore[arg-type]
        tool_calls=answer.tool_calls,
        directives=answer.directives,
        degraded_reason=answer.degraded_reason,
        active_scenario_id=answer.active_scenario_id,
        duration_ms=answer.duration_ms,
    )
