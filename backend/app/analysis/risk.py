"""Deterministic risk scoring.

The contract this module exists to honour:

    Bad:  "Risk is HIGH because the AI thinks so."
    Good: "Risk HIGH — +30 critical asset, +25 customer-facing service,
           +20 single-region dependency, +15 event proximity, −10 failover capacity."

Every score is the sum of named, signed contributions. The formula is documented in
``docs/IMPACT_MODEL.md`` and its band boundaries are pinned by tests.
"""

from __future__ import annotations

from datetime import datetime

from ..graph.world_graph import WorldGraph
from ..models.analysis import Confidence, RiskScore, ScoreContribution
from ..models.core import (
    CRITICALITY_ORDER,
    Criticality,
    DataMode,
    Severity,
    WorldEvent,
    severity_from_score,
)
from .business_impact import is_customer_facing
from .propagation import PropagationState

#: Maximum points each component can contribute. Chosen so that no single dimension can
#: push a score into CRITICAL alone: reaching 75 needs at least three of them.
WEIGHTS = {
    "asset_criticality": 30.0,
    "customer_facing": 25.0,
    "single_point_of_failure": 20.0,
    "event_proximity": 15.0,
    "customer_exposure": 20.0,
    "dependency_depth": 10.0,
    "event_severity": 15.0,
    "redundancy_credit": -12.0,
    "freshness_penalty": -8.0,
}


def score_impact(
    graph: WorldGraph,
    state: PropagationState,
    *,
    origin_ids: list[str],
    event: WorldEvent | None = None,
    proximity: float = 0.0,
    max_depth_reached: int = 0,
    stale_seconds: float | None = None,
) -> RiskScore:
    """Score a settled impact 0-100 with a full derivation.

    Args:
        graph: the world the impact was computed against.
        state: the settled propagation state.
        origin_ids: what failed / was struck.
        event: the originating world event, when there is one.
        proximity: 0-1 geographic proximity factor from the correlation step.
        max_depth_reached: how deep the propagation actually travelled.
        stale_seconds: age of the underlying observation, when known.
    """
    contributions: list[ScoreContribution] = []
    impacted_ids = state.impacted_ids()
    impacted = [graph.entity(eid) for eid in impacted_ids]
    impacted = [entity for entity in impacted if entity is not None]

    # 1. Asset criticality — the most critical thing actually degraded.
    #    Entities whose criticality was never declared are excluded outright: an
    #    undeclared criticality is not a low one, and scoring it would be WorldGraph
    #    grading infrastructure on a judgement nobody made
    #    (docs/REALITY_PASS_AUDIT.md, B4).
    declared = [e for e in impacted if e.criticality is not Criticality.UNKNOWN]
    if declared:
        worst = min(declared, key=lambda e: CRITICALITY_ORDER.index(e.criticality))
        weight = {
            Criticality.CRITICAL: 1.0,
            Criticality.HIGH: 0.7,
            Criticality.MEDIUM: 0.4,
            Criticality.LOW: 0.15,
            Criticality.UNKNOWN: 0.0,
        }[worst.criticality]
        # Scale by how badly the asset was actually hurt. A CRITICAL asset that lost 5% of
        # its availability is not the same event as one that is gone, and a flat "a
        # critical thing was touched" term makes every incident look like a catastrophe —
        # which is how a risk score stops carrying information.
        severity_of_loss = 1.0 - state.availability.get(worst.id, 1.0)
        contributions.append(
            ScoreContribution(
                code="asset_criticality",
                label=f"{worst.criticality.value.lower()} asset impacted",
                points=round(WEIGHTS["asset_criticality"] * weight * severity_of_loss, 1),
                detail=(
                    f"{worst.name} is rated {worst.criticality.value} and is modelled at "
                    f"{state.availability.get(worst.id, 1.0) * 100:.0f}% availability"
                ),
            )
        )

    # 2. Customer-facing service degraded — the difference between an internal wobble and
    #    an outage a merchant sees.
    # Only an explicit declaration counts. `None` (nobody said) must not score.
    customer_facing = [e for e in impacted if is_customer_facing(e) is True]
    if customer_facing:
        worst_availability = min(
            state.availability.get(e.id, 1.0) for e in customer_facing
        )
        factor = min(1.0, (1.0 - worst_availability) * 1.6)
        contributions.append(
            ScoreContribution(
                code="customer_facing",
                label="customer-facing service degraded",
                points=round(WEIGHTS["customer_facing"] * factor, 1),
                detail=f"{len(customer_facing)} customer-facing entities below nominal",
            )
        )

    # 3. Single point of failure on the impact path.
    # A declared redundancy of 1 is a single point of failure. An *undeclared* one is a
    # coverage gap, not a finding, so it does not score.
    spof = [
        e
        for e in impacted
        if e.business.is_single_point_of_failure is True
        and state.availability.get(e.id, 1.0) < 0.5
    ]
    if spof:
        worst_spof_loss = 1.0 - min(state.availability.get(e.id, 1.0) for e in spof)
        contributions.append(
            ScoreContribution(
                code="single_point_of_failure",
                label="single-region dependency with no failover",
                points=round(WEIGHTS["single_point_of_failure"] * worst_spof_loss, 1),
                detail=", ".join(sorted(e.name for e in spof)[:3]),
            )
        )

    # 4. Geographic proximity of the originating event.
    if event is not None and proximity > 0:
        contributions.append(
            ScoreContribution(
                code="event_proximity",
                label="event proximity to enterprise assets",
                points=round(WEIGHTS["event_proximity"] * proximity, 1),
                detail=f"closest asset at {proximity * 100:.0f}% of the exposure radius",
            )
        )

    # 5. Raw event severity, independent of what it happened to hit.
    if event is not None:
        severity_weight = {
            Severity.CRITICAL: 1.0,
            Severity.HIGH: 0.75,
            Severity.MODERATE: 0.45,
            Severity.LOW: 0.2,
            Severity.INFO: 0.05,
        }[event.severity]
        contributions.append(
            ScoreContribution(
                code="event_severity",
                label=f"{event.severity.value.lower()} severity event",
                points=round(WEIGHTS["event_severity"] * severity_weight, 1),
                detail=event.title[:160],
            )
        )

    # 6. Customer traffic exposure.
    from .business_impact import business_impact  # local import avoids a cycle

    impact = business_impact(graph, state)
    if impact.traffic_impact is not None and impact.traffic_impact > 0:
        contributions.append(
            ScoreContribution(
                code="customer_exposure",
                label="customer traffic exposure",
                points=round(WEIGHTS["customer_exposure"] * min(1.0, impact.traffic_impact * 2.2), 1),
                detail=f"{impact.traffic_impact * 100:.0f}% of modelled customer traffic degraded",
            )
        )

    # 7. Dependency depth — a failure that reaches five layers in is structurally worse
    #    than one contained at its own site.
    if max_depth_reached >= 2:
        depth_factor = min(1.0, (max_depth_reached - 1) / 4.0)
        contributions.append(
            ScoreContribution(
                code="dependency_depth",
                label="deep dependency propagation",
                points=round(WEIGHTS["dependency_depth"] * depth_factor, 1),
                detail=f"impact travelled {max_depth_reached} dependency hops",
            )
        )

    # 8. Redundancy credit — a negative term, because a resilient estate should score
    #    lower for the same event and the model must be able to say so.
    origins = [graph.entity(oid) for oid in origin_ids]
    redundant_origins = [
        e
        for e in origins
        if e is not None and e.business.redundancy is not None and e.business.redundancy >= 2
    ]
    if redundant_origins:
        contributions.append(
            ScoreContribution(
                code="redundancy_credit",
                label="failover capacity available",
                points=WEIGHTS["redundancy_credit"],
                detail=", ".join(sorted(e.name for e in redundant_origins)[:3]),
            )
        )

    # 9. Freshness penalty — an old observation should not carry a full-confidence score.
    if stale_seconds is not None and stale_seconds > 3600:
        hours = stale_seconds / 3600.0
        factor = min(1.0, (hours - 1.0) / 11.0)  # full penalty at 12 h
        contributions.append(
            ScoreContribution(
                code="freshness_penalty",
                label="observation is stale",
                points=round(WEIGHTS["freshness_penalty"] * factor, 1),
                detail=f"underlying observation is {hours:.1f} h old",
            )
        )

    total = sum(item.points for item in contributions)
    clamped = max(0.0, min(100.0, total))
    return RiskScore(
        score=round(clamped, 1),
        severity=severity_from_score(clamped),
        contributions=contributions,
    )


def assess_confidence(
    graph: WorldGraph,
    state: PropagationState,
    *,
    event: WorldEvent | None,
    origin_ids: list[str],
    truncated: bool,
    proximity: float = 0.0,
    now: datetime | None = None,
) -> Confidence:
    """Honest confidence, with the evidence that supports it and what is missing.

    Starts from the data source's own confidence and adjusts for things WorldGraph can
    actually check: whether the geometry is unambiguous, whether the dependency chain is
    explicit in the graph, whether anything was truncated, and whether the estate we are
    reasoning over is synthetic.
    """
    strong: list[str] = []
    uncertain: list[str] = []
    score = 0.5

    if event is not None:
        score = event.source.confidence * 0.9
        if event.source.mode is DataMode.LIVE:
            strong.append(f"event reported live by {event.source.source_name}")
        elif event.source.mode is DataMode.REPLAY:
            strong.append(f"event replayed from a recorded {event.source.source_name} fixture")
        else:
            uncertain.append("event is synthetic, not an observation")
            score = min(score, 0.75)
        freshness = event.source.freshness_seconds(now=now)
        if freshness is not None and freshness > 6 * 3600:
            uncertain.append(f"observation is {freshness / 3600:.1f} h old")
            score -= 0.1

    if proximity > 0:
        strong.append("affected facility lies inside the modelled event radius")
        score += 0.1
    elif event is not None and event.location is not None:
        uncertain.append("no facility lies inside the modelled event radius")

    explicit_edges = sum(len(graph.dependencies_of(oid)) for oid in origin_ids)
    if explicit_edges:
        strong.append("direct dependency edges exist in the graph")
        score += 0.08

    impacted = state.impacted_ids()
    if impacted:
        strong.append(f"{len(impacted)} entities degraded by deterministic propagation")

    if truncated:
        uncertain.append("dependency traversal was truncated; impact may be wider")
        score -= 0.15

    if not state.converged:
        uncertain.append("propagation did not fully converge within the iteration budget")
        score -= 0.1

    # Structural unknowns WorldGraph genuinely does not have. Naming them is the point.
    for origin_id in origin_ids:
        entity = graph.entity(origin_id)
        if entity is None:
            continue
        if entity.type.value == "SUPPLIER":
            uncertain.append(f"current operating status of {entity.name} is unavailable")
            score -= 0.05
        if entity.health.value == "UNKNOWN":
            uncertain.append(f"{entity.name} has no health telemetry")
            score -= 0.05

    # Derived from the data, never asserted. Stating "this estate is synthetic demo data"
    # about a live cloud import was a falsehood in the one field whose entire job is
    # honesty (docs/REALITY_PASS_AUDIT.md, B7).
    uncertain.extend(_estate_provenance_notes(graph))

    # Undeclared business metadata is a real limit on any conclusion drawn here, and the
    # operator can act on knowing which dimension is missing.
    undeclared_criticality = sum(
        1 for entity in graph.entities if entity.criticality is Criticality.UNKNOWN
    )
    if undeclared_criticality:
        uncertain.append(
            f"{undeclared_criticality} of {len(graph.entities)} entities have no declared "
            "criticality, so business severity is not fully modelled"
        )
        score -= 0.05

    return Confidence(
        score=round(max(0.05, min(0.99, score)), 2),
        strong_evidence=_dedupe(strong),
        uncertainties=_dedupe(uncertain),
    )


def _estate_provenance_notes(graph: WorldGraph) -> list[str]:
    """Describe what kind of data this estate actually is, from the records themselves."""
    modes = {entity.source.mode for entity in graph.entities}
    notes: list[str] = []
    if DataMode.SYNTHETIC in modes:
        notes.append("part of this estate is synthetic demonstration data")
    if DataMode.SIMULATED in modes:
        notes.append("part of this estate is a simulated world state")
    if DataMode.LIVE in modes and len(modes) > 1:
        notes.append("this estate mixes live inventory with non-live records")
    return notes


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(values))
