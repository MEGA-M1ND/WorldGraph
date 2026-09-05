"""USGS earthquake adapter.

Source: the USGS realtime GeoJSON summary feeds. US Government work — public domain, no
key, no rate-limit agreement required. Citation is courtesy, and WorldGraph shows it in
the provenance panel.

The endpoint and the depth banding are the one implementation detail carried over from
God's Eye View's earthquake layer (MIT); everything else here is WorldGraph's own
normalization into :class:`WorldEvent`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..geo.spatial import exposure_radius_for
from ..models.core import (
    DataMode,
    DataSourceInfo,
    EventCategory,
    GeoPoint,
    Severity,
    WorldEvent,
    utcnow,
)
from ..security.sanitize import contains_injection_attempt, sanitize_identifier, sanitize_text
from .base import AdapterError, WorldDataAdapter

#: M2.5+ in the last day. Large enough to be interesting, small enough to always have
#: something on the globe.
FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"

#: A single event's detail page, for the provenance link.
EVENT_URL_TEMPLATE = "https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}"

#: Magnitude → severity band. Chosen against the practical infrastructure question
#: ("would an operator care?"), not against a seismological intensity scale.
_MAGNITUDE_BANDS: tuple[tuple[float, Severity], ...] = (
    (7.0, Severity.CRITICAL),
    (6.0, Severity.HIGH),
    (5.0, Severity.MODERATE),
    (4.0, Severity.LOW),
)


def severity_for_magnitude(magnitude: float) -> Severity:
    """Band an earthquake magnitude."""
    for floor, severity in _MAGNITUDE_BANDS:
        if magnitude >= floor:
            return severity
    return Severity.INFO


def depth_band(depth_km: float) -> str:
    """USGS depth classification, used for the globe's colour ramp."""
    if depth_km < 70:
        return "shallow"
    if depth_km < 300:
        return "intermediate"
    return "deep"


def normalize_feature(feature: object, *, mode: DataMode = DataMode.LIVE) -> WorldEvent | None:
    """Turn one GeoJSON feature into a :class:`WorldEvent`.

    Returns ``None`` for a feature that cannot be trusted — missing geometry, an
    unparseable magnitude, a nonsense timestamp. A partially-understood earthquake is not
    a smaller earthquake; it is an unusable record, and inventing defaults for it would put
    a fictional event on an operator's globe.
    """
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None

    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
        depth_km = float(coordinates[2]) if len(coordinates) > 2 else 10.0
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    magnitude_raw = properties.get("mag")
    if magnitude_raw is None:
        return None
    try:
        magnitude = float(magnitude_raw)
    except (TypeError, ValueError):
        return None

    time_ms = properties.get("time")
    try:
        occurred_at = datetime.fromtimestamp(float(time_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None

    raw_place = properties.get("place") or "Unknown location"
    place = sanitize_text(raw_place, max_length=200)
    event_id = sanitize_identifier(feature.get("id") or properties.get("code") or place)

    severity = severity_for_magnitude(magnitude)
    metadata = {
        "magnitude": round(magnitude, 2),
        "depth_km": round(depth_km, 1),
        "depth_band": depth_band(depth_km),
        "place": place,
        "tsunami": bool(properties.get("tsunami")),
        "felt_reports": properties.get("felt"),
        "usgs_status": sanitize_text(properties.get("status"), max_length=32),
    }
    if contains_injection_attempt(str(raw_place)):
        # Surfaced, not swallowed: an operator should know a feed string looked hostile.
        metadata["sanitized"] = True

    return WorldEvent(
        id=f"usgs:{event_id}",
        category=EventCategory.EARTHQUAKE,
        title=f"M{magnitude:.1f} earthquake — {place}",
        description=sanitize_text(properties.get("title") or place, max_length=512),
        severity=severity,
        location=GeoPoint(lat=lat, lon=lon, altitude=-depth_km * 1000.0),
        exposure_radius_km=round(exposure_radius_for(EventCategory.EARTHQUAKE, metadata), 1),
        occurred_at=occurred_at,
        source=DataSourceInfo(
            source_id="usgs",
            source_name="USGS Earthquake Hazards Program",
            source_url=EVENT_URL_TEMPLATE.format(event_id=event_id),
            mode=mode,
            # USGS automatic solutions get revised; a reviewed one is firmer. Encoding
            # that here means the risk score inherits real epistemic humility rather
            # than a constant.
            confidence=0.95 if metadata.get("usgs_status") == "reviewed" else 0.85,
            observed_at=occurred_at,
            ingested_at=utcnow(),
        ),
        metadata=metadata,
    )


class UsgsEarthquakeAdapter(WorldDataAdapter):
    """Live M2.5+ earthquakes from USGS, refreshed every five minutes."""

    id = "usgs-earthquakes"
    name = "USGS Earthquakes"
    source_url = FEED_URL
    mode = DataMode.LIVE
    refresh_interval_seconds = 300.0
    stale_after_seconds = 900.0

    def __init__(self, *, feed_url: str = FEED_URL, enabled: bool = True) -> None:
        super().__init__()
        self.feed_url = feed_url
        self.source_url = feed_url
        self._enabled = enabled

    async def fetch(self):
        if not self._enabled:
            raise AdapterError("USGS feed is disabled in this deployment")
        payload = await self.http_get_json(self.feed_url)
        if not isinstance(payload, dict):
            raise AdapterError("USGS returned an unexpected payload shape")
        features = payload.get("features")
        if not isinstance(features, list):
            raise AdapterError("USGS response contained no feature collection")

        events: list[WorldEvent] = []
        for feature in features:
            event = normalize_feature(feature, mode=self.mode)
            if event is not None:
                events.append(event)
        if not events and features:
            raise AdapterError("USGS returned features but none could be normalized")
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events, []
