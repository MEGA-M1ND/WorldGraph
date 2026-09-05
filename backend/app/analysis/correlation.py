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
from ..models.core import (
    DependencyType,
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
            hit = False
            if (cve and cve in {c.upper() for c in component.cve_ids}) or (wanted_products and component.name.lower() in wanted_products):
                hit = True
            if not hit:
                continue
            matches.append(
                VulnerabilityMatch(
                    entity=entity,
                    component_name=component.name,
                    component_version=component.version,
                    internet_facing=entity.exposure.internet_facing,
                )
            )
            break  # one row per asset, not per package
    matches.sort(key=lambda m: (not m.internet_facing, m.entity.id))
    return matches


#: Edge types an attacker can traverse *forwards*, from a foothold to what it talks to.
_EGRESS_TYPES = frozenset(
    {DependencyType.DEPENDS_ON, DependencyType.CONNECTS_TO, DependencyType.HOSTED_IN}
)


def _attacker_successors(graph: WorldGraph, node_id: str) -> list[str]:
    """Where an attacker sitting on ``node_id`` can go next.

    Two movements, and the second is the one naive models miss:

    * **Egress** — what this asset talks to. ``admin-api DEPENDS_ON internal-auth`` means
      a foothold on ``admin-api`` can reach ``internal-auth``.
    * **Trust** — who accepts this asset's word. ``payments-api DEPENDS_ON internal-auth``
      means ``payments-api`` trusts ``internal-auth``, so an attacker who owns the auth
      service can move *against the arrow* into payments. Following egress alone would
      report the payments path as unreachable, which is exactly the wrong answer.

    ``SUPPLIED_BY``, ``SERVES`` and ``REPLICATES_TO`` are excluded: a supply contract and a
    customer relationship are not network adjacency.
    """
    successors: list[str] = []
    for edge in graph.dependencies_of(node_id):
        if edge.type in _EGRESS_TYPES:
            successors.append(edge.target_entity_id)
    for edge in graph.dependents_of(node_id):
        # Trust flows to whoever declared the dependency, but a shared host is not a trust
        # relationship in itself, so HOSTED_IN is not followed backwards here.
        if edge.type is DependencyType.DEPENDS_ON:
            successors.append(edge.source_entity_id)
    return successors


def attack_paths(
    graph: WorldGraph,
    *,
    from_entity_id: str = "internet",
    to_entity_ids: list[str] | None = None,
    max_depth: int = 6,
) -> list[list[str]]:
    """Reachability paths from an origin (usually the internet) to sensitive assets.

    The internet is modelled as an ordinary entity that internet-facing services declare a
    ``CONNECTS_TO`` edge against, so "what can the internet reach" is a normal graph query
    rather than a special case.

    This is **reachability, not exploitability**. WorldGraph knows what is adjacent to what;
    it does not test authentication, network policy or whether an exploit works. The tool
    result says so, and the UI repeats it.
    """
    if from_entity_id not in graph:
        return []
    targets = set(to_entity_ids or [])
    entry_points = [
        edge.source_entity_id
        for edge in graph.dependents_of(from_entity_id)
        if (entry := graph.entity(edge.source_entity_id)) is not None
        and entry.exposure.internet_facing
    ]

    paths: list[list[str]] = []
    for entry in entry_points:
        # Breadth-first over attacker-traversable edges, tracking the route so the answer
        # is a path an operator can read rather than a set of ids.
        queue: list[list[str]] = [[entry]]
        seen: set[str] = {entry}
        while queue:
            path = queue.pop(0)
            if len(path) > max_depth:
                continue
            for next_id in _attacker_successors(graph, path[-1]):
                if next_id in path or next_id == from_entity_id:
                    continue
                extended = [*path, next_id]
                if not targets or next_id in targets:
                    paths.append([from_entity_id, *extended])
                if next_id not in seen:
                    seen.add(next_id)
                    queue.append(extended)

    # Shortest first, then deterministic: the same graph must always yield the same order.
    paths.sort(key=lambda path: (len(path), path))
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
