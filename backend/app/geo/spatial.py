"""Deterministic geospatial utilities.

No LLM is asked "is Taipei near this earthquake". Spatial matching is arithmetic, it is
cheap, and getting it wrong silently is exactly the failure mode this product cannot
afford. Everything here is pure and unit-tested.
"""

from __future__ import annotations

import math

from ..models.core import EventCategory, GeoPoint

#: Mean Earth radius (IUGG). Haversine on a sphere is accurate to ~0.5 % — far inside the
#: uncertainty of an "exposure radius", so an ellipsoidal solution would be false precision.
EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    # clamp guards against a float overshoot of 1.0 for antipodal-ish inputs
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def within_radius(centre: GeoPoint, point: GeoPoint, radius_km: float) -> bool:
    """True when ``point`` lies inside ``radius_km`` of ``centre`` (inclusive)."""
    if radius_km <= 0:
        return False
    return haversine_km(centre, point) <= radius_km


def bearing_degrees(origin: GeoPoint, target: GeoPoint) -> float:
    """Initial great-circle bearing from ``origin`` to ``target``, 0-360° clockwise from N."""
    lat1, lat2 = math.radians(origin.lat), math.radians(target.lat)
    dlon = math.radians(target.lon - origin.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_point(bearing: float) -> str:
    """Bearing → 8-point compass label, for human-readable proximity text."""
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int(((bearing % 360.0) + 22.5) // 45.0) % 8]


# --------------------------------------------------------------------------------------
# Exposure radius model
# --------------------------------------------------------------------------------------

#: Baseline exposure radius per event category, in km, for events that carry no explicit
#: footprint. These are coarse planning figures, not hazard-model outputs — the product
#: labels correlation confidence accordingly.
_CATEGORY_BASE_RADIUS_KM: dict[EventCategory, float] = {
    EventCategory.EARTHQUAKE: 100.0,
    EventCategory.WILDFIRE: 30.0,
    EventCategory.SEVERE_WEATHER: 150.0,
    EventCategory.FLOOD: 60.0,
    EventCategory.POWER_OUTAGE: 50.0,
    EventCategory.NETWORK_OUTAGE: 250.0,
    EventCategory.CLOUD_INCIDENT: 0.0,  # named-region events, not geographic ones
    EventCategory.SERVICE_INCIDENT: 0.0,
    EventCategory.SECURITY_VULNERABILITY: 0.0,  # correlated by software, not geography
    EventCategory.SUPPLY_CHAIN: 200.0,
    EventCategory.OTHER: 50.0,
}


def earthquake_exposure_radius_km(magnitude: float, depth_km: float = 10.0) -> float:
    """Radius of plausible infrastructure disruption for an earthquake.

    WorldGraph V1 uses a deliberately simple, monotonic model rather than a real ground
    motion prediction equation (a GMPE needs site conditions and a regional attenuation
    model that a synthetic estate cannot supply):

        radius_km = 10 * 10 ** (0.5 * (M - 4))      capped to [10, 900]
        deep quakes (>70 km) shed 25 % of that radius

    It reproduces the right orders of magnitude — M4 ≈ 10 km, M6.8 ≈ 79 km,
    M7.5 ≈ 178 km — and it is monotonic in magnitude, which is the property the
    correlation and risk layers actually rely on. Documented in ``docs/IMPACT_MODEL.md``.
    """
    magnitude = max(0.0, float(magnitude))
    radius = 10.0 * (10.0 ** (0.5 * (magnitude - 4.0)))
    if depth_km > 70.0:
        # Deep-focus events shake a wider area more weakly; for *infrastructure
        # disruption* the net effect in this model is a smaller damage footprint.
        radius *= 0.75
    return max(10.0, min(900.0, radius))


def exposure_radius_for(
    category: EventCategory,
    metadata: dict[str, object] | None = None,
) -> float:
    """Best-effort exposure radius for an event, in km.

    Uses a category-specific model where one exists (earthquakes), otherwise the coarse
    category baseline. Returns 0 for categories that are not geographic at all — those
    correlate by named region or by software inventory instead.
    """
    meta = metadata or {}
    if category is EventCategory.EARTHQUAKE:
        magnitude = _as_float(meta.get("magnitude"), default=4.0)
        depth = _as_float(meta.get("depth_km"), default=10.0)
        return earthquake_exposure_radius_km(magnitude, depth)
    explicit = meta.get("radius_km")
    if explicit is not None:
        return max(0.0, _as_float(explicit, default=0.0))
    return _CATEGORY_BASE_RADIUS_KM.get(category, 50.0)


def proximity_factor(distance_km: float, radius_km: float) -> float:
    """How strongly an asset at ``distance_km`` is exposed by a ``radius_km`` event.

    Linear falloff from 1.0 at the epicentre to 0.0 at the radius edge. Linear rather
    than inverse-square because the radius already encodes the attenuation; stacking two
    decay models would double-count and make everything look safe.
    """
    if radius_km <= 0:
        return 0.0
    if distance_km >= radius_km:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (distance_km / radius_km)))


def _as_float(value: object, *, default: float) -> float:
    """Coerce untrusted feed metadata to a float without raising."""
    try:
        if value is None:
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
