"""Event → enterprise correlation.

Two independent correlation paths, both deterministic:

* **Geospatial** — an earthquake, wildfire or storm has coordinates and a modelled
  exposure radius. Assets with a physical footprint inside that radius are exposed, and
  the strength of that exposure falls off linearly to the radius edge.
* **Software inventory** — a CVE has no location. It correlates by matching affected
  product names against each asset's declared software components, then by reachability.

Neither path asks a language model anything. Spatial arithmetic and string matching are
cheap, exact and testable; an LLM doing either would be slower, more expensive and
occasionally wrong in ways nobody could reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..geo.spatial import bearing_degrees, compass_point, haversine_km, proximity_factor
from ..graph.world_graph import WorldGraph
from ..models.analysis import InventoryCoverage, VulnerabilityAssessment
from ..models.core import (
    DependencyType,
    EntityType,
    EventCategory,
    HealthState,
    Severity,
    WorldEntity,
    WorldEvent,
)

#: Availability an asset is modelled to retain at the very centre of a CRITICAL event.
#: Not zero: a facility inside a severe earthquake radius is disrupted, not vaporised, and
#: claiming certain destruction from a magnitude number alone would be dishonest.
_SEVERITY_FLOOR: dict[Severity, float] = {
    Severity.CRITICAL: 0.10,
    Severity.HIGH: 0.30,
    Severity.MODERATE: 0.60,
    Severity.LOW: 0.85,
    Severity.INFO: 0.97,
}


@dataclass(slots=True)
class SpatialMatch:
    """One asset found inside an event's exposure radius."""

    entity: WorldEntity
    distance_km: float
    #: 1.0 at the epicentre, 0.0 at the radius edge.
    proximity: float
    bearing_deg: float

    @property
    def direction(self) -> str:
        """8-point compass direction from the event to the asset."""
        return compass_point(self.bearing_deg)

    def describe(self) -> str:
        return (
            f"{self.entity.name} — {self.distance_km:.0f} km {self.direction} of the event"
        )


def find_assets_near_event(
    graph: WorldGraph,
    event: WorldEvent,
    *,
    radius_km: float | None = None,
    physical_only: bool = True,
) -> list[SpatialMatch]:
    """Assets inside the event's exposure radius, closest first.

    ``physical_only`` restricts matching to entities with a real footprint. A microservice
    is not "near" an earthquake in any useful sense — it inherits impact through the site
    that hosts it, which is the graph's job, not geography's.
    """
    if event.location is None:
        return []
    radius = radius_km if radius_km is not None else event.exposure_radius_km
    if radius <= 0:
        return []

    matches: list[SpatialMatch] = []
    for entity in graph.located_entities():
        if physical_only and not entity.is_physical:
            continue
        assert entity.location is not None  # located_entities guarantees this
        distance = haversine_km(event.location, entity.location)
        if distance > radius:
            continue
        matches.append(
            SpatialMatch(
                entity=entity,
                distance_km=distance,
                proximity=proximity_factor(distance, radius),
                bearing_deg=bearing_degrees(event.location, entity.location),
            )
        )
    matches.sort(key=lambda m: (m.distance_km, m.entity.id))
    return matches


def modelled_availability(event: WorldEvent, proximity: float) -> float:
    """Availability an exposed asset is modelled to retain.

    Interpolates between untouched (1.0) at the radius edge and the severity floor at the
    epicentre. Documented in ``docs/IMPACT_MODEL.md``.
    """
    floor = _SEVERITY_FLOOR.get(event.severity, 0.8)
    return round(max(0.0, min(1.0, 1.0 - proximity * (1.0 - floor))), 4)


def correlate_event(
    graph: WorldGraph, event: WorldEvent
) -> tuple[dict[str, float], list[SpatialMatch], float]:
    """Turn an event into pinned availabilities for the blast-radius engine.

    Returns:
        ``(initial_availability, spatial_matches, peak_proximity)``.

    Entities the adapter named explicitly (``directly_named_entity_ids`` — for example a
    cloud status page naming a region) are pinned at the severity floor directly, since a
    provider saying "this region is impaired" is stronger evidence than a distance
    calculation.
    """
    matches = find_assets_near_event(graph, event)
    pinned: dict[str, float] = {}
    peak = 0.0

    for match in matches:
        pinned[match.entity.id] = modelled_availability(event, match.proximity)
        peak = max(peak, match.proximity)

    for entity_id in event.directly_named_entity_ids:
        if entity_id not in graph:
            continue
        # An explicit naming outranks a computed distance for the same entity.
        pinned[entity_id] = min(
            pinned.get(entity_id, 1.0), _SEVERITY_FLOOR.get(event.severity, 0.8)
        )
        peak = max(peak, 1.0)

    return pinned, matches, peak


def proximity_summary(graph: WorldGraph, event: WorldEvent) -> dict[str, int]:
    """Counts for the event card: facilities, suppliers, dependent services."""
    matches = find_assets_near_event(graph, event)
    facilities = sum(
        1
        for m in matches
        if m.entity.type.value in {"DATACENTER", "OFFICE", "CLOUD_REGION", "FACTORY", "NETWORK_NODE"}
    )
    suppliers = sum(1 for m in matches if m.entity.type.value == "SUPPLIER")
    exposed_ids = [m.entity.id for m in matches]
    dependent = 0
    if exposed_ids:
        reach = graph.traverse_dependents(exposed_ids, max_depth=4)
        dependent = sum(
            1
            for step in reach.steps
            if step.depth > 0
            and (entity := graph.entity(step.entity_id)) is not None
            and entity.type.value
            in {"MICROSERVICE", "APPLICATION", "BUSINESS_SERVICE", "DATABASE"}
        )
    return {
        "critical_facilities": facilities,
        "suppliers": suppliers,
        "dependent_services": dependent,
        "assets_in_radius": len(matches),
    }


# --------------------------------------------------------------------------------------
# Security correlation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class VulnerabilityMatch:
    """One asset running software affected by a vulnerability."""

    entity: WorldEntity
    component_name: str
    component_version: str
    internet_facing: bool
    #: Shortest path from the public internet to this asset, if one exists.
    internet_path: list[str] | None = None
    #: How this asset was matched. A CVE id carried by the asset's own inventory is
    #: evidence; a product-name collision is a candidate for triage. Conflating them is
    #: how a vulnerability dashboard becomes noise nobody reads.
    assessment: VulnerabilityAssessment = VulnerabilityAssessment.POTENTIALLY_AFFECTED


#: Entity types that can plausibly run software, and therefore can carry an inventory.
#: A cloud region, a customer segment or an organisation cannot; counting them would
#: understate coverage and make a thin estate look well-inventoried.
SOFTWARE_BEARING_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.APPLICATION,
        EntityType.MICROSERVICE,
        EntityType.DATABASE,
        EntityType.KUBERNETES_CLUSTER,
        EntityType.NETWORK_NODE,
        EntityType.EXTERNAL_API,
    }
)


def inventory_coverage(graph: WorldGraph) -> InventoryCoverage:
    """How much of this estate WorldGraph could search for vulnerable software.

    Reported alongside every negative conclusion, because "we found nothing" means
    something entirely different on an estate with no inventory than on one with full
    inventory, and the two used to be indistinguishable in the output.
    """
    assessable = [e for e in graph.entities if e.type in SOFTWARE_BEARING_TYPES]
    return InventoryCoverage(
        assessable_entities=len(assessable),
        entities_with_inventory=sum(1 for e in assessable if e.software),
    )


def assess_vulnerability(
    graph: WorldGraph, matches: list[VulnerabilityMatch]
) -> tuple[VulnerabilityAssessment, InventoryCoverage]:
    """The honest conclusion for one vulnerability against one estate.

    A negative requires inventory to back it. Without that, the answer is
    ``INSUFFICIENT_DATA`` — which is not a softer way of saying "safe", and must never be
    rendered as one.
    """
    coverage = inventory_coverage(graph)
    if any(m.assessment is VulnerabilityAssessment.CONFIRMED_AFFECTED for m in matches):
        return VulnerabilityAssessment.CONFIRMED_AFFECTED, coverage
    if matches:
        return VulnerabilityAssessment.POTENTIALLY_AFFECTED, coverage
    if not coverage.supports_negative_conclusion:
        return VulnerabilityAssessment.INSUFFICIENT_DATA, coverage
    return VulnerabilityAssessment.NOT_AFFECTED, coverage


def match_vulnerable_assets(
    graph: WorldGraph, *, cve_id: str = "", product_names: list[str] | None = None
) -> list[VulnerabilityMatch]:
    """Assets whose software inventory matches a vulnerability.

    Matching is by explicit CVE id first (the asset's inventory already carries it), then
    by case-insensitive product name. Version-range matching is deliberately out of scope
    for V1 — WorldGraph is not a scanner, and pretending to do range logic on a synthetic
    inventory would be theatre.
    """
    wanted_products = {name.strip().lower() for name in (product_names or []) if name.strip()}
    cve = cve_id.strip().upper()
    matches: list[VulnerabilityMatch] = []

    for entity in graph.entities:
        for component in entity.software:
            # An explicit CVE id on the asset's own inventory is evidence. A product-name
            # collision is not: it ignores version and vendor, so "nginx" matches every
            # nginx ever installed, including one patched years ago.
            if cve and cve in {c.upper() for c in component.cve_ids}:
                assessment = VulnerabilityAssessment.CONFIRMED_AFFECTED
            elif wanted_products and component.name.lower() in wanted_products:
                assessment = VulnerabilityAssessment.POTENTIALLY_AFFECTED
            else:
                continue
            matches.append(
                VulnerabilityMatch(
                    entity=entity,
                    component_name=component.name,
                    component_version=component.version,
                    internet_facing=entity.exposure.internet_facing,
                    assessment=assessment,
                )
            )
            break  # one row per asset, not per package
    matches.sort(key=lambda m: (not m.internet_facing, m.entity.id))
    return matches


#: The pseudo-entity representing the public internet. Internet-facing services declare a
#: route to it, which makes "what can the outside reach" an ordinary graph query.
INTERNET_ENTITY_ID = "internet"


@dataclass(slots=True)
class AttackHop:
    """One step of a reachability path, carrying why it is traversable."""

    from_entity_id: str
    to_entity_id: str
    edge_type: DependencyType
    #: EGRESS (this asset talks to that one) or INFERRED_TRUST (that one relies on this).
    movement: str
    confidence: float
    evidence: str

    @property
    def is_inferred(self) -> bool:
        return self.movement == MOVEMENT_INFERRED_TRUST


@dataclass(slots=True)
class AttackPath:
    """A route from an origin to a target, and how much of it is established.

    ``confidence`` is the weakest hop, not an average: a path is only as good as its most
    doubtful step, and averaging lets four solid hops disguise one invented one.
    """

    nodes: list[str]
    hops: list[AttackHop]

    @property
    def confidence(self) -> float:
        return min((hop.confidence for hop in self.hops), default=0.0)

    @property
    def relies_on_inference(self) -> bool:
        return any(hop.is_inferred for hop in self.hops)

    @property
    def basis(self) -> str:
        return "INFERRED" if self.relies_on_inference else "ESTABLISHED"


#: Edge types an attacker can traverse *forwards*, from a foothold to what it talks to.
#:
#: ``HOSTED_IN`` is deliberately absent. Being in a datacenter is not network adjacency to
#: the datacenter, and WorldGraph models no hypervisor, host OS or control plane through
#: which "move into the region you run in" would mean anything. It was a hop that added
#: nodes to paths without adding evidence.
_EGRESS_TYPES = frozenset({DependencyType.DEPENDS_ON, DependencyType.CONNECTS_TO})

#: How an attacker got from one node to the next, and how much that claim is worth.
MOVEMENT_EGRESS = "EGRESS"
MOVEMENT_INFERRED_TRUST = "INFERRED_TRUST"


def _attacker_successors(graph: WorldGraph, node_id: str) -> list[tuple[str, AttackHop]]:
    """Where an attacker on ``node_id`` can go next, and on what evidence.

    Two movements, and they are not worth the same.

    **Egress** is what this asset talks to. ``admin-api DEPENDS_ON internal-auth`` means a
    foothold on ``admin-api`` can reach ``internal-auth``: the dependency itself is the
    evidence that a route exists.

    **Trust** is the reverse direction — an attacker who owns a service moving into what
    relies on it. That is real for an authentication service and false for a database, and
    an operational dependency edge does not distinguish them. WorldGraph cannot tell which
    it is holding, so it still traverses the edge and marks the hop ``INFERRED_TRUST``:
    dropping it would hide genuine identity-pivot paths, and presenting it as established
    fact would manufacture them. Two applications that merely share a database produce such
    a path, and the caller is told which hop made it so.

    Establishing this properly needs edges inventory cannot supply — who may assume which
    role, who accepts whose tokens, what network policy permits. Until a source for those
    exists, an inferred hop is labelled rather than believed.
    """
    successors: list[tuple[str, AttackHop]] = []
    for edge in graph.dependencies_of(node_id):
        if edge.type in _EGRESS_TYPES:
            successors.append(
                (
                    edge.target_entity_id,
                    AttackHop(
                        from_entity_id=node_id,
                        to_entity_id=edge.target_entity_id,
                        edge_type=edge.type,
                        movement=MOVEMENT_EGRESS,
                        confidence=0.9,
                        evidence=(
                            f"{node_id} declares a {edge.type.value} on "
                            f"{edge.target_entity_id}, so a route exists."
                        ),
                    ),
                )
            )
    for edge in graph.dependents_of(node_id):
        # HOSTED_IN is not followed backwards either: a shared host is not a trust
        # relationship, and treating it as one would make every workload in a region
        # reachable from every other.
        if edge.type is DependencyType.DEPENDS_ON:
            successors.append(
                (
                    edge.source_entity_id,
                    AttackHop(
                        from_entity_id=node_id,
                        to_entity_id=edge.source_entity_id,
                        edge_type=edge.type,
                        movement=MOVEMENT_INFERRED_TRUST,
                        confidence=0.35,
                        evidence=(
                            f"{edge.source_entity_id} depends on {node_id}. Whether owning "
                            f"{node_id} yields access to {edge.source_entity_id} depends on "
                            "a trust relationship WorldGraph has no evidence for."
                        ),
                    ),
                )
            )
    return successors


def attack_paths(
    graph: WorldGraph,
    *,
    from_entity_id: str = "internet",
    to_entity_ids: list[str] | None = None,
    max_depth: int = 6,
    include_inferred: bool = True,
) -> list[AttackPath]:
    """Reachability paths from an origin to sensitive assets, with per-hop evidence.

    Two origins behave differently, and conflating them was a bug. When the origin is the
    internet pseudo-entity, the paths start at the internet-facing services that declare a
    route to it — "what can the outside reach". When the origin is any other entity, **that
    entity is the foothold**: the question is "this host is owned, where can they go", and
    it does not require the foothold to be internet-facing.

    Previously the internet-facing filter was applied unconditionally, so every non-internet
    origin returned an empty list — a silent false negative on the most common question a
    responder asks, answered as reassurance.

    This is **reachability, not exploitability**. WorldGraph knows what is adjacent to what;
    it does not test authentication, network policy, or whether an exploit works. Each hop
    says whether it is established by a declared route or inferred from an operational
    dependency, and each path is only as strong as its weakest hop.
    """
    if from_entity_id not in graph:
        return []
    targets = set(to_entity_ids or [])

    internet_origin = from_entity_id == INTERNET_ENTITY_ID
    if internet_origin:
        # Entry points are the internet-facing services that declare a route to the
        # internet node. The path is rendered as starting at the internet itself.
        starts = [
            (
                edge.source_entity_id,
                AttackHop(
                    from_entity_id=from_entity_id,
                    to_entity_id=edge.source_entity_id,
                    edge_type=edge.type,
                    movement=MOVEMENT_EGRESS,
                    confidence=0.95,
                    evidence=(
                        f"{edge.source_entity_id} is internet-facing and declares a route "
                        "to the public internet."
                    ),
                ),
            )
            for edge in graph.dependents_of(from_entity_id)
            if (entry := graph.entity(edge.source_entity_id)) is not None
            and entry.exposure.internet_facing
        ]
    else:
        # The foothold is the origin. No entry hop, because the attacker is already there.
        starts = [(from_entity_id, None)]

    paths: list[AttackPath] = []
    for entry_id, entry_hop in starts:
        opening = [entry_hop] if entry_hop is not None else []
        queue: list[tuple[list[str], list[AttackHop]]] = [
            ([from_entity_id, entry_id] if entry_hop is not None else [entry_id], opening)
        ]
        seen: set[str] = {entry_id}

        while queue:
            nodes, hops = queue.pop(0)
            if len(nodes) > max_depth:
                continue
            for next_id, hop in _attacker_successors(graph, nodes[-1]):
                if next_id in nodes:
                    continue
                if not include_inferred and hop.is_inferred:
                    continue
                extended_nodes = [*nodes, next_id]
                extended_hops = [*hops, hop]
                if not targets or next_id in targets:
                    paths.append(AttackPath(nodes=extended_nodes, hops=extended_hops))
                if next_id not in seen:
                    seen.add(next_id)
                    queue.append((extended_nodes, extended_hops))

    # Established paths first, then shortest, then deterministic: an operator reading a
    # list top-down should meet the routes that are actually proved before the speculative
    # ones. The same graph must always yield the same order.
    paths.sort(key=lambda p: (p.relies_on_inference, len(p.nodes), p.nodes))
    return paths


def security_pins(
    matches: list[VulnerabilityMatch], *, compromised_id: str, availability: float = 0.0
) -> dict[str, float]:
    """Pinned availabilities for a "what if this asset is compromised" analysis.

    Only the named asset is pinned. WorldGraph does not assume lateral movement succeeds —
    it shows what the compromised asset can *reach* and lets the operator decide, which is
    a materially different claim from "everything downstream is owned".
    """
    known = {match.entity.id for match in matches}
    if compromised_id not in known:
        return {compromised_id: availability}
    return {compromised_id: availability}


def health_for_availability(value: float) -> HealthState:
    """Convenience re-export so callers do not import from two modules."""
    from ..models.core import health_from_value

    return health_from_value(value)


#: Event categories that correlate geographically at all.
GEOGRAPHIC_CATEGORIES = frozenset(
    {
        EventCategory.EARTHQUAKE,
        EventCategory.WILDFIRE,
        EventCategory.SEVERE_WEATHER,
        EventCategory.FLOOD,
        EventCategory.POWER_OUTAGE,
        EventCategory.NETWORK_OUTAGE,
        EventCategory.SUPPLY_CHAIN,
        EventCategory.OTHER,
    }
)
