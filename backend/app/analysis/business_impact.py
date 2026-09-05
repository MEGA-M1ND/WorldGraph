"""Business impact from a settled propagation state.

Every figure produced here is a **MODELLED ESTIMATE**. The schema carries that literal in
:class:`BusinessImpact.disclaimer` so it cannot be dropped by a careless serializer, and
the UI renders it beside the numbers. WorldGraph does not have access to AtlasPay's ledger
— it has a synthetic estate and an arithmetic model, and it says so.
"""

from __future__ import annotations

from ..graph.world_graph import WorldGraph
from ..models.analysis import BusinessImpact, CustomerExposure, WorldSnapshotMetrics
from ..models.core import (
    Criticality,
    DependencyType,
    EntityType,
    Severity,
    WorldEntity,
    severity_from_score,
)
from .propagation import IMPACT_THRESHOLD, PropagationState

#: Entity types that count as "a service" when tallying critical services impacted.
SERVICE_TYPES = frozenset(
    {
        EntityType.BUSINESS_SERVICE,
        EntityType.APPLICATION,
        EntityType.MICROSERVICE,
        EntityType.DATABASE,
    }
)

#: Availability floor each SLA tier promises. A projected availability below the floor is
#: reported as a modelled breach — not a contractual determination.
SLA_FLOORS: dict[str, float] = {
    "TIER-0": 0.9995,
    "TIER-1": 0.999,
    "TIER-2": 0.99,
}


def is_customer_facing(entity: WorldEntity) -> bool:
    """Whether users outside AtlasPay notice this entity failing."""
    if entity.type is EntityType.CUSTOMER_REGION:
        return True
    if entity.metadata.get("customer_facing") is True:
        return True
    if entity.metadata.get("customer_facing") is False:
        return False
    return entity.type in {EntityType.BUSINESS_SERVICE, EntityType.APPLICATION}


def customer_exposure(
    graph: WorldGraph, state: PropagationState, *, threshold: float = IMPACT_THRESHOLD
) -> list[CustomerExposure]:
    """Per-customer-region exposure, worst first."""
    rows: list[CustomerExposure] = []
    for entity in graph.entities_of_type(EntityType.CUSTOMER_REGION):
        availability = state.availability.get(entity.id, 1.0)
        if availability >= threshold:
            continue
        # Which services actually reach this region — named so the UI can show the route.
        via = [
            edge.source_entity_id
            for edge in graph.dependents_of(entity.id)
            if edge.type is DependencyType.SERVES
            and state.availability.get(edge.source_entity_id, 1.0) < threshold
        ]
        rows.append(
            CustomerExposure(
                entity_id=entity.id,
                region=entity.business.region or entity.name,
                customer_count=entity.business.customer_count,
                traffic_impact=round(min(1.0, max(0.0, 1.0 - availability)), 4),
                projected_availability=round(availability, 4),
                via_service_ids=sorted(set(via)),
            )
        )
    rows.sort(key=lambda row: (-row.traffic_impact, row.entity_id))
    return rows


def business_impact(
    graph: WorldGraph, state: PropagationState, *, threshold: float = IMPACT_THRESHOLD
) -> BusinessImpact:
    """Aggregate the settled state into headline business figures."""
    regions = graph.entities_of_type(EntityType.CUSTOMER_REGION)

    # Organisation availability is the traffic-weighted availability seen by customers.
    # Weighting by customer regions (not by every node) is what makes the number mean
    # "what our users experience" rather than "how many boxes are green".
    total_weight = sum(region.business.traffic_share for region in regions)
    if total_weight > 0:
        availability = sum(
            region.business.traffic_share * state.availability.get(region.id, 1.0)
            for region in regions
        ) / total_weight
    else:
        availability = 1.0

    traffic_impact = max(0.0, 1.0 - availability)

    customers_affected = sum(
        region.business.customer_count
        for region in regions
        if state.availability.get(region.id, 1.0) < threshold
    )
    revenue_at_risk = sum(
        region.business.revenue_per_hour * (1.0 - state.availability.get(region.id, 1.0))
        for region in regions
    )

    sla_breaches: list[str] = []
    for entity in graph.entities:
        floor = SLA_FLOORS.get(entity.business.sla_tier)
        if floor is None:
            continue
        if state.availability.get(entity.id, 1.0) < floor:
            sla_breaches.append(entity.id)

    critical_services = sum(
        1
        for entity in graph.entities
        if entity.type in SERVICE_TYPES
        and entity.criticality is Criticality.CRITICAL
        and state.availability.get(entity.id, 1.0) < threshold
    )
    regions_impacted = sum(
        1 for region in regions if state.availability.get(region.id, 1.0) < threshold
    )

    return BusinessImpact(
        availability=round(max(0.0, min(1.0, availability)), 5),
        traffic_impact=round(min(1.0, traffic_impact), 5),
        customers_affected=customers_affected,
        revenue_at_risk_per_hour=round(revenue_at_risk, 2),
        sla_breaches=sorted(sla_breaches),
        critical_services_impacted=critical_services,
        customer_regions_impacted=regions_impacted,
    )


#: Entity types that carry AtlasPay's own load and therefore have a meaningful
#: "regional capacity". Cloud regions are deliberately excluded: a provider's region is
#: not AtlasPay's capacity, and counting it dilutes the figure with infrastructure the
#: company does not own or scale. Databases are excluded for the same reason a database
#: is sized for its data, not for request headroom.
CAPACITY_BEARING_TYPES = frozenset(
    {
        EntityType.KUBERNETES_CLUSTER,
        EntityType.MICROSERVICE,
    }
)

#: Regions that are aggregates rather than places; excluded from the per-region table.
_AGGREGATE_REGIONS = frozenset({"GLOBAL", ""})

#: Site-level region labels (from the fixture's ``SITES``) rolled up into operating
#: regions, so the UI shows "APAC 63%" rather than four separate city rows.
_REGION_ROLLUP: dict[str, str] = {
    "SINGAPORE": "APAC",
    "MUMBAI": "APAC",
    "TOKYO": "APAC",
    "APAC": "APAC",
    "FRANKFURT": "EMEA",
    "EMEA": "EMEA",
    "VIRGINIA": "AMER",
    "AMER": "AMER",
    "BENGALURU": "APAC",
}


def rollup_region(label: str) -> str:
    """Map a site label onto its operating region, falling back to the label itself."""
    return _REGION_ROLLUP.get(label.upper(), label.upper())


def regional_capacity(graph: WorldGraph, state: PropagationState) -> dict[str, float]:
    """Serviceable capacity per operating region, 0-1.

    Uses the *capacity* solve, not availability: this row answers "how much load could
    this region take", which is the question a supply-chain disruption actually changes.
    Weighted by traffic share where entities declare one, so the number tracks load
    rather than box count.
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    for entity in graph.entities:
        region = entity.business.region
        if region in _AGGREGATE_REGIONS:
            continue
        if entity.type not in CAPACITY_BEARING_TYPES:
            continue
        weight = entity.business.traffic_share or 0.05
        buckets.setdefault(rollup_region(region), []).append(
            (weight, state.capacity.get(entity.id, 1.0))
        )

    out: dict[str, float] = {}
    for region, rows in buckets.items():
        total = sum(weight for weight, _ in rows)
        if total <= 0:
            continue
        out[region] = round(sum(weight * value for weight, value in rows) / total, 4)
    return dict(sorted(out.items()))


def snapshot_metrics(
    graph: WorldGraph,
    state: PropagationState,
    *,
    risk_score: float | None = None,
) -> WorldSnapshotMetrics:
    """Headline metrics for one world state, for the CURRENT vs SIMULATION table."""
    impact = business_impact(graph, state)
    if risk_score is None:
        # Derive a material-risk band from the impact itself when no dedicated risk score
        # was computed: traffic loss and critical-service count are the two dimensions an
        # operator would use to eyeball severity.
        derived = min(
            100.0,
            impact.traffic_impact * 120.0 + impact.critical_services_impacted * 12.0,
        )
        material = severity_from_score(derived)
    else:
        material = severity_from_score(risk_score)
    return WorldSnapshotMetrics(
        availability=impact.availability,
        regional_capacity=regional_capacity(graph, state),
        critical_services_impacted=impact.critical_services_impacted,
        customer_regions_impacted=impact.customer_regions_impacted,
        customers_affected=impact.customers_affected,
        revenue_at_risk_per_hour=impact.revenue_at_risk_per_hour,
        material_risk=material,
    )


def worst_material_risk(*severities: Severity) -> Severity:
    """The most severe of several bands."""
    order = [Severity.INFO, Severity.LOW, Severity.MODERATE, Severity.HIGH, Severity.CRITICAL]
    return max(severities, key=order.index, default=Severity.LOW)
