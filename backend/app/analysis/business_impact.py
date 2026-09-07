"""Business impact from a settled propagation state.

Every figure produced here is a **MODELLED ESTIMATE**. The schema carries that literal in
:class:`BusinessImpact.disclaimer` so it cannot be dropped by a careless serializer, and
the UI renders it beside the numbers.

And every figure that cannot be computed is ``None``, with a reason. The Reality Pass
found this module returning ``availability = 1.0`` and ``revenue_at_risk = 0`` for an
estate it had just modelled as entirely degraded, because the estate declared no customer
regions and the arithmetic quietly fell through to its identity
(docs/REALITY_PASS_AUDIT.md, B1/B2). Zero is a measurement. Unknown is not, and the two
must not share a representation.
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


def is_customer_facing(entity: WorldEntity) -> bool | None:
    """Whether users outside the organisation notice this entity failing.

    Tri-state. ``None`` means nobody said, and **entity type is not evidence**: before the
    Reality Pass this returned ``True`` for every ``APPLICATION``, which silently promoted
    every imported Azure App Service to customer-facing and paid it 25 risk points on no
    evidence at all (docs/REALITY_PASS_AUDIT.md, B5).

    A ``CUSTOMER_REGION`` is the one structural exception: the type *is* the declaration.
    """
    if entity.type is EntityType.CUSTOMER_REGION:
        return True
    if entity.customer_facing is not None:
        return entity.customer_facing
    # Legacy metadata form, still honoured because it is an explicit declaration.
    declared = entity.metadata.get("customer_facing")
    if isinstance(declared, bool):
        return declared
    return None


def customer_exposure(
    graph: WorldGraph, state: PropagationState, *, threshold: float = IMPACT_THRESHOLD
) -> list[CustomerExposure]:
    """Per-customer-region exposure, worst first.

    Empty when the estate declares no customer regions — which is a true statement about
    an imported cloud inventory, not a claim that no customers were affected.
    """
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
                # A customer region with no declared count still has real exposure; the
                # count is reported as 0 here and the aggregate reports UNKNOWN instead of
                # summing zeros into a confident total.
                customer_count=entity.business.customer_count or 0,
                traffic_impact=round(min(1.0, max(0.0, 1.0 - availability)), 4),
                projected_availability=round(availability, 4),
                via_service_ids=sorted(set(via)),
            )
        )
    rows.sort(key=lambda row: (-row.traffic_impact, row.entity_id))
    return rows


def infrastructure_availability(
    graph: WorldGraph, state: PropagationState
) -> float:
    """Availability of the estate itself, independent of any business metadata.

    This is the figure a cloud import *can* support: it needs only the graph. Weighted by
    declared traffic share where one exists, and counted evenly where none does, so an
    estate that declares nothing still gets an honest number rather than a null.
    """
    load_bearing = [
        entity
        for entity in graph.entities
        if entity.type in SERVICE_TYPES | CAPACITY_BEARING_TYPES
        or entity.type in {EntityType.CLOUD_REGION, EntityType.DATACENTER}
    ]
    if not load_bearing:
        load_bearing = graph.entities
    if not load_bearing:
        return 1.0

    total_weight = 0.0
    weighted = 0.0
    for entity in load_bearing:
        weight = entity.business.traffic_share if entity.business.has_traffic else None
        weight = weight if weight else 1.0
        total_weight += weight
        weighted += weight * state.availability.get(entity.id, 1.0)
    return round(weighted / total_weight, 5) if total_weight else 1.0


def business_impact(
    graph: WorldGraph,
    state: PropagationState,
    *,
    threshold: float = IMPACT_THRESHOLD,
    causal_only: bool = False,
) -> BusinessImpact:
    """Aggregate the settled state into headline business figures.

    Each aggregate is computed only where the estate supplies the evidence for it; where
    it does not, the field is ``None`` and ``unknown_reasons`` names what is missing.
    """
    regions = graph.entities_of_type(EntityType.CUSTOMER_REGION)
    unknown: list[str] = []

    # -- customer-experienced availability ---------------------------------------------
    # Weighted by traffic across customer regions: what users experience, not how many
    # boxes are green. Both inputs must exist for the figure to mean anything.
    weighted_regions = [r for r in regions if r.business.has_traffic]
    total_weight = sum(r.business.traffic_share or 0.0 for r in weighted_regions)

    availability: float | None = None
    traffic_impact: float | None = None
    if not regions:
        unknown.append(
            "Customer-experienced availability is unknown: this workspace declares no "
            "customer regions, so there is no population to compute an experience for."
        )
    elif total_weight <= 0:
        unknown.append(
            "Customer-experienced availability is unknown: customer regions exist but "
            "none declares a traffic share to weight them by."
        )
    else:
        availability = sum(
            (r.business.traffic_share or 0.0) * state.availability.get(r.id, 1.0)
            for r in weighted_regions
        ) / total_weight
        availability = round(max(0.0, min(1.0, availability)), 5)
        traffic_impact = round(max(0.0, 1.0 - availability), 5)

    # -- customers affected -------------------------------------------------------------
    impacted_regions = [r for r in regions if state.availability.get(r.id, 1.0) < threshold]
    customers_affected: int | None = None
    if any(r.business.has_customers for r in regions):
        customers_affected = sum(
            r.business.customer_count or 0 for r in impacted_regions if r.business.has_customers
        )
        if any(not r.business.has_customers for r in impacted_regions):
            unknown.append(
                "Customer count is partial: some impacted regions declare no customer "
                "count, so the total is a lower bound."
            )
    else:
        unknown.append(
            "Customers affected is unknown: no entity in this workspace declares a "
            "customer count."
        )

    # -- revenue --------------------------------------------------------------------------
    revenue_at_risk: float | None = None
    revenue_regions = [r for r in regions if r.business.has_revenue]
    if revenue_regions:
        revenue_at_risk = round(
            sum(
                (r.business.revenue_per_hour or 0.0)
                * (1.0 - state.availability.get(r.id, 1.0))
                for r in revenue_regions
            ),
            2,
        )
    else:
        unknown.append(
            "Revenue exposure is unknown: no revenue metadata is available for this "
            "workspace."
        )

    # -- SLA ------------------------------------------------------------------------------
    sla_breaches = [
        entity.id
        for entity in graph.entities
        if (floor := SLA_FLOORS.get(entity.business.sla_tier)) is not None
        and state.availability.get(entity.id, 1.0) < floor
    ]
    if not any(e.business.sla_tier for e in graph.entities):
        unknown.append(
            "SLA exposure is unknown: no entity declares an SLA tier."
        )

    # -- countable facts, which need no business metadata ---------------------------------
    critical_services = sum(
        1
        for entity in graph.entities
        if entity.type in SERVICE_TYPES
        and entity.criticality is Criticality.CRITICAL
        and state.availability.get(entity.id, 1.0) < threshold
    )
    # "How many did this cause" for an attributed analysis; "how many are degraded" for a
    # dashboard. Counting absolutely inside a blast radius reported every entity in an
    # imported estate as impacted regardless of what failed.
    impacted_count = (
        len(state.caused_ids(threshold=threshold))
        if causal_only
        else sum(1 for value in state.availability.values() if value < threshold)
    )

    return BusinessImpact(
        availability=availability,
        traffic_impact=traffic_impact,
        customers_affected=customers_affected,
        revenue_at_risk_per_hour=revenue_at_risk,
        infrastructure_availability=infrastructure_availability(graph, state),
        sla_breaches=sorted(sla_breaches),
        critical_services_impacted=critical_services,
        customer_regions_impacted=len(impacted_regions),
        impacted_entity_count=impacted_count,
        unknown_reasons=unknown,
    )


#: Entity types that carry the organisation's own load and therefore have a meaningful
#: "regional capacity". Cloud regions are deliberately excluded: a provider's region is
#: not the customer's capacity, and counting it dilutes the figure with infrastructure the
#: organisation neither owns nor scales. Databases are excluded for the same reason a
#: database is sized for its data, not for request headroom.
CAPACITY_BEARING_TYPES = frozenset(
    {
        EntityType.KUBERNETES_CLUSTER,
        EntityType.MICROSERVICE,
    }
)

#: Regions that are aggregates rather than places; excluded from the per-region table.
_AGGREGATE_REGIONS = frozenset({"GLOBAL", ""})

#: Operating regions WorldGraph knows how to group site labels into.
#:
#: This is *derivation*, not a lookup table of one fixture's cities. The Reality Pass
#: found a hardcoded SINGAPORE/MUMBAI/FRANKFURT map here through which every Azure region
#: fell straight to its own uppercase name (docs/REALITY_PASS_AUDIT.md, B6). The rules
#: below are substring matches over geography that hold for any source; anything they do
#: not recognise keeps its own label rather than being forced into a bucket.
_REGION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "APAC",
        (
            "SINGAPORE", "MUMBAI", "TOKYO", "BENGALURU", "APAC", "ASIA", "INDIA",
            "JAPAN", "KOREA", "AUSTRALIA", "SYDNEY", "MELBOURNE", "HONGKONG",
            "TAIWAN", "JIO", "PUNE", "CHENNAI",
        ),
    ),
    (
        "EMEA",
        (
            "FRANKFURT", "EMEA", "EUROPE", "UK", "LONDON", "FRANCE", "PARIS",
            "GERMANY", "SWEDEN", "NORWAY", "SWITZERLAND", "ITALY", "SPAIN",
            "POLAND", "UAE", "QATAR", "ISRAEL", "SOUTHAFRICA", "AFRICA",
        ),
    ),
    (
        "AMER",
        (
            "VIRGINIA", "AMER", "US", "USA", "CANADA", "BRAZIL", "MEXICO",
            "IOWA", "TEXAS", "ARIZONA", "CALIFORNIA", "WASHINGTON", "CHILE",
        ),
    ),
)


def rollup_region(label: str) -> str:
    """Group a site or cloud-region label into an operating region.

    Falls back to the label's own uppercase form when no rule matches, which is the honest
    outcome: an unrecognised region is reported under its real name rather than guessed
    into a continent.
    """
    if not label:
        return ""
    upper = label.upper().replace("-", "").replace("_", "").replace(" ", "")
    for region, needles in _REGION_RULES:
        if any(needle in upper for needle in needles):
            return region
    return label.upper()


def regional_capacity(graph: WorldGraph, state: PropagationState) -> dict[str, float]:
    """Serviceable capacity per operating region, 0-1.

    Uses the *capacity* solve, not availability: this row answers "how much load could
    this region take", which is the question a supply-chain disruption actually changes.
    Weighted by traffic share where declared, evenly otherwise.
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    for entity in graph.entities:
        region = entity.business.region
        if region in _AGGREGATE_REGIONS:
            continue
        if entity.type not in CAPACITY_BEARING_TYPES:
            continue
        # An undeclared traffic share weighs 1.0 — an equal vote — rather than 0, which
        # would silently drop the entity out of its own region's figure.
        weight = entity.business.traffic_share if entity.business.has_traffic else None
        weight = weight if weight else 1.0
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
        # was computed. Prefer the customer-experienced view; fall back to infrastructure
        # availability when the estate cannot supply one, so an imported workspace still
        # gets a defensible band rather than a null.
        traffic_term = (
            impact.traffic_impact
            if impact.traffic_impact is not None
            else 1.0 - impact.infrastructure_availability
        )
        derived = min(100.0, traffic_term * 120.0 + impact.critical_services_impacted * 12.0)
        material = severity_from_score(derived)
    else:
        material = severity_from_score(risk_score)
    return WorldSnapshotMetrics(
        availability=impact.availability,
        infrastructure_availability=impact.infrastructure_availability,
        regional_capacity=regional_capacity(graph, state),
        critical_services_impacted=impact.critical_services_impacted,
        customer_regions_impacted=impact.customer_regions_impacted,
        customers_affected=impact.customers_affected,
        revenue_at_risk_per_hour=impact.revenue_at_risk_per_hour,
        material_risk=material,
        unknown_reasons=impact.unknown_reasons,
    )


def worst_material_risk(*severities: Severity) -> Severity:
    """The most severe of several bands."""
    order = [Severity.INFO, Severity.LOW, Severity.MODERATE, Severity.HIGH, Severity.CRITICAL]
    return max(severities, key=order.index, default=Severity.LOW)
