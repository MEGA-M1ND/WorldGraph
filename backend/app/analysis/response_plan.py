"""Deterministic response-plan generation.

Recommendations are derived from the graph and the analysis, not invented by a model.
Each rule looks for a specific structural condition (an unredundant dependency below
threshold, a promotable replica, a pausable workload, a sole-source supplier) and, when it
fires, emits an action whose ``rationale`` names the evidence that triggered it.

An LLM may later rewrite the *prose* around these actions — see ``ai/analyst.py`` — but
never the action list, the affected entities, or the urgency. V1 executes nothing.
"""

from __future__ import annotations

import uuid

from ..graph.world_graph import WorldGraph
from ..models.analysis import (
    BlastRadiusResult,
    ResponseAction,
    ResponsePlan,
    Urgency,
)
from ..models.core import Criticality, EntityType, Severity

#: Availability below which an entity is considered "in trouble" for planning purposes.
_TROUBLE = 0.9

#: Customer traffic impact below which notifying customers is noise, not a response.
_NOTIFY_THRESHOLD = 0.02

#: Availability floor for a peer to be worth failing over to at all.
_HEALTHY_PEER = 0.80

#: A peer must also be this much healthier than the entity it is replacing. Shifting
#: traffic onto something barely better moves the outage instead of resolving it, and a
#: fixed "peer must be ≥95%" bar would silently withhold the recommendation whenever the
#: same shared dependency has nicked both sides — which is exactly when it is needed.
_PEER_MARGIN = 0.20


def generate_response_plan(
    graph: WorldGraph,
    result: BlastRadiusResult,
    *,
    scenario_name: str | None = None,
) -> ResponsePlan:
    """Build a response plan from a blast-radius result."""
    impacted = {r.entity_id: r for r in [*result.direct_impact, *result.indirect_impact]}
    actions: list[ResponseAction] = []
    assumptions: list[str] = []
    questions: list[str] = []

    # --- 1. Shift traffic away from degraded regions to healthy peers -----------------
    for record in sorted(impacted.values(), key=lambda r: r.availability):
        entity = graph.entity(record.entity_id)
        if entity is None or entity.type is not EntityType.KUBERNETES_CLUSTER:
            continue
        if record.availability >= _TROUBLE:
            continue
        peers = _peer_clusters(graph, entity.id)
        healthy = [
            p
            for p in peers
            if _availability(impacted, p.id) >= _HEALTHY_PEER
            and _availability(impacted, p.id) >= record.availability + _PEER_MARGIN
        ]
        if not healthy:
            continue
        target = max(healthy, key=lambda p: _availability(impacted, p.id))
        actions.append(
            ResponseAction(
                action=f"Shift traffic from {entity.name} to {target.name}.",
                rationale=(
                    f"{entity.name} is modelled at {record.availability * 100:.0f}% availability "
                    f"while {target.name} serves the same workload and remains above "
                    f"{_HEALTHY_PEER * 100:.0f}%. Both host "
                    f"{', '.join(sorted(w.name for w in _workloads(graph, entity.id))[:3]) or 'shared workloads'}."
                ),
                affected_entities=[entity.id, target.id],
                urgency=Urgency.NOW if record.availability < 0.6 else Urgency.SOON,
                confidence=round(min(0.95, result.confidence.score + 0.1), 2),
            )
        )
        # One traffic-shift recommendation is a decision; five is a to-do list nobody reads.
        break

    # --- 2. Add capacity where the shift is going ------------------------------------
    for action in list(actions):
        if not action.action.startswith("Shift traffic"):
            continue
        target_id = action.affected_entities[-1]
        target = graph.entity(target_id)
        if target is None:
            continue
        source_id = action.affected_entities[0]
        lost_share = graph.require_entity(source_id).business.traffic_share
        target_share = target.business.traffic_share or 0.01
        uplift = min(2.0, lost_share / target_share)
        actions.append(
            ResponseAction(
                action=f"Increase {target.name} capacity by {uplift * 100:.0f}%.",
                rationale=(
                    f"{graph.require_entity(source_id).name} carries "
                    f"{lost_share * 100:.0f}% of modelled traffic against {target.name}'s "
                    f"{target_share * 100:.0f}%. Absorbing the shift without adding capacity "
                    f"would move the failure rather than resolve it."
                ),
                affected_entities=[target.id],
                urgency=Urgency.NOW,
                confidence=round(max(0.4, result.confidence.score - 0.1), 2),
            )
        )
        assumptions.append(
            "Traffic shift assumes the destination region can be scaled within the incident window."
        )
        break

    # --- 3. Promote a replica when its primary is degraded ---------------------------
    for record in sorted(impacted.values(), key=lambda r: r.availability):
        entity = graph.entity(record.entity_id)
        if entity is None or entity.type is not EntityType.DATABASE:
            continue
        if record.availability >= _TROUBLE:
            continue
        replicas = [
            graph.require_entity(edge.target_entity_id)
            for edge in graph.dependencies_of(entity.id)
            if edge.type.value == "REPLICATES_TO"
        ]
        promotable = [
            r
            for r in replicas
            if r.metadata.get("promotable") is True
            and _availability(impacted, r.id) >= _HEALTHY_PEER
            and _availability(impacted, r.id) >= record.availability + _PEER_MARGIN
        ]
        if not promotable:
            continue
        replica = promotable[0]
        ratio = replica.metadata.get("promoted_throughput_ratio")
        ratio_note = (
            f" It carries an estimated {float(ratio) * 100:.0f}% of primary throughput once promoted."
            if isinstance(ratio, (int, float))
            else ""
        )
        actions.append(
            ResponseAction(
                action=f"Promote {replica.name} to primary.",
                rationale=(
                    f"{entity.name} is modelled at {record.availability * 100:.0f}% availability and "
                    f"{replica.name} is a promotable replica outside the impact radius.{ratio_note}"
                ),
                affected_entities=[entity.id, replica.id],
                urgency=Urgency.NOW,
                confidence=round(min(0.9, result.confidence.score), 2),
                requires_approval=True,
            )
        )
        questions.append(
            f"Has replication lag on {replica.name} been confirmed inside the acceptable RPO?"
        )
        break

    # --- 4. Pause non-critical workloads to free capacity ----------------------------
    pausable = [
        entity
        for entity in graph.entities
        if entity.metadata.get("pausable") is True
        or (entity.criticality is Criticality.LOW and entity.type is EntityType.MICROSERVICE)
    ]
    if pausable and result.severity in {Severity.HIGH, Severity.CRITICAL}:
        names = ", ".join(sorted(e.name for e in pausable)[:3])
        actions.append(
            ResponseAction(
                action=f"Pause non-critical workloads ({names}).",
                rationale=(
                    "These workloads are rated LOW criticality and are not on any path to a "
                    "customer-facing service in this analysis, so pausing them frees shared "
                    "capacity without adding customer impact."
                ),
                affected_entities=sorted(e.id for e in pausable),
                urgency=Urgency.SOON,
                confidence=0.8,
            )
        )

    # --- 5. Notify affected customer regions -----------------------------------------
    # Only above a materiality floor. Propagation surfaces exposure down to fractions of a
    # percent, and recommending that an operator email customers over a 0.2% modelled
    # impact is how a response plan trains people to ignore it.
    material_exposure = [
        row for row in result.customer_exposure if row.traffic_impact >= _NOTIFY_THRESHOLD
    ]
    if material_exposure:
        worst = material_exposure[0]
        regions = ", ".join(row.region for row in material_exposure[:3])
        actions.append(
            ResponseAction(
                action=f"Notify affected enterprise customers ({regions}).",
                rationale=(
                    f"{worst.region} is modelled at {worst.projected_availability * 100:.0f}% "
                    f"availability, affecting {worst.customer_count:,} modelled customers. "
                    "Proactive notice is a contractual expectation at TIER-0 and TIER-1."
                ),
                affected_entities=[row.entity_id for row in material_exposure],
                urgency=Urgency.NOW if worst.traffic_impact > 0.2 else Urgency.SOON,
                confidence=round(result.confidence.score, 2),
            )
        )

    # --- 6. Escalate structural supplier risk ----------------------------------------
    suppliers = [
        graph.require_entity(record.entity_id)
        for record in impacted.values()
        if (entity := graph.entity(record.entity_id)) is not None
        and entity.type is EntityType.SUPPLIER
    ]
    for supplier in suppliers:
        second_source = supplier.metadata.get("second_source_qualified")
        lead_time = supplier.metadata.get("lead_time_weeks")
        detail = []
        if second_source is False:
            detail.append("no second source is qualified")
        if isinstance(lead_time, (int, float)):
            detail.append(f"replacement lead time is {lead_time:.0f} weeks")
        actions.append(
            ResponseAction(
                action=f"Escalate {supplier.name} continuity review.",
                rationale=(
                    f"{supplier.name} is a single-site dependency inside the impact radius"
                    + (f" and {' and '.join(detail)}" if detail else "")
                    + ". Capacity to replace failed hardware — not current traffic — is the "
                    "exposure here."
                ),
                affected_entities=[supplier.id],
                urgency=Urgency.MONITOR,
                confidence=0.7,
            )
        )
        questions.append(f"What is {supplier.name}'s current operating status on the ground?")
        break

    # --- fallback ---------------------------------------------------------------------
    if not actions:
        actions.append(
            ResponseAction(
                action="Monitor. No structural mitigation is indicated by this analysis.",
                rationale=(
                    "No impacted entity fell below the planning threshold, and no promotable "
                    "replica, healthy peer or pausable workload was identified as needed."
                ),
                affected_entities=result.origin_ids,
                urgency=Urgency.MONITOR,
                confidence=round(result.confidence.score, 2),
                requires_approval=False,
            )
        )

    objectives = _objectives(result)
    assumptions.extend(
        [
            "All figures are WorldGraph V1 Impact Model estimates over synthetic AtlasPay data.",
            "Dependency criticality and redundancy values come from the modelled estate, "
            "not from live telemetry.",
        ]
    )
    if result.truncated:
        assumptions.append(
            "Dependency traversal was truncated; entities beyond the traversal bound are not "
            "represented in this plan."
        )
    questions.extend(uncertainty for uncertainty in result.confidence.uncertainties)

    return ResponsePlan(
        id=f"plan-{uuid.uuid4().hex[:12]}",
        summary=_summary(result, scenario_name),
        objectives=objectives,
        actions=actions,
        assumptions=_dedupe(assumptions),
        unresolved_questions=_dedupe(questions),
    )


def _summary(result: BlastRadiusResult, scenario_name: str | None) -> str:
    prefix = f"{scenario_name}: " if scenario_name else ""
    return (
        f"{prefix}{result.severity.value} material risk from {result.origin_label}. "
        f"{len(result.direct_impact)} directly affected and {len(result.indirect_impact)} "
        f"indirectly affected entities; modelled organisation availability "
        f"{result.business_impact.availability * 100:.2f}%. "
        f"Confidence {result.confidence.score * 100:.0f}%. "
        "Recommendations only — WorldGraph executes nothing."
    )


def _objectives(result: BlastRadiusResult) -> list[str]:
    objectives = []
    if result.business_impact.critical_services_impacted:
        objectives.append("Restore availability of impacted customer-facing payment paths.")
    if result.customer_exposure:
        objectives.append("Contain customer-visible impact in the exposed regions.")
    if any(
        record.criticality is Criticality.CRITICAL
        for record in [*result.direct_impact, *result.indirect_impact]
    ):
        objectives.append("Remove the single points of failure this event exposed.")
    objectives.append("Preserve an audit trail of every decision taken during the incident.")
    return objectives


def _availability(impacted: dict[str, object], entity_id: str) -> float:
    """Modelled availability of an entity, defaulting to healthy when unimpacted."""
    record = impacted.get(entity_id)
    return record.availability if record is not None else 1.0  # type: ignore[union-attr]


def _peer_clusters(graph: WorldGraph, cluster_id: str):
    """Other clusters hosting at least one workload in common with ``cluster_id``."""
    workloads = _workloads(graph, cluster_id)
    peers: dict[str, object] = {}
    for workload in workloads:
        for edge in graph.dependencies_of(workload.id):
            if edge.type.value != "HOSTED_IN" or edge.target_entity_id == cluster_id:
                continue
            peer = graph.entity(edge.target_entity_id)
            if peer is not None and peer.type is EntityType.KUBERNETES_CLUSTER:
                peers[peer.id] = peer
    return list(peers.values())  # type: ignore[return-value]


def _workloads(graph: WorldGraph, cluster_id: str):
    """Entities hosted on a cluster."""
    return graph.hosted_entities(cluster_id)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
