"""Deterministic fixture adapters — the demo and replay scenarios.

Everything this module produces is stamped ``REPLAY`` (a recorded real-world shape,
replayed on a fixed clock) or ``SYNTHETIC`` (invented outright). Neither is ever presented
as LIVE. The UI badges both, and the feed status reads ``SIMULATED`` rather than ``LIVE``.

These exist so the hero demo runs identically on a plane, in a conference room with hostile
wifi, and in CI. A great deterministic demo beats twenty flaky integrations.
"""

from __future__ import annotations

from datetime import timedelta

from ..fixtures.atlaspay import DEMO_CVE_ID, REPLAY_BASE_TIME
from ..geo.spatial import exposure_radius_for
from ..models.core import (
    DataMode,
    DataSourceInfo,
    EventCategory,
    GeoPoint,
    Severity,
    WorldEvent,
)
from .base import WorldDataAdapter

#: The Taiwan earthquake at the centre of hero scenario #1. The magnitude, depth and
#: location are plausible for the Hsinchu region; the event itself is invented.
TAIWAN_EVENT_ID = "replay:taiwan-m68"

#: The Singapore region outage used by replay scenario #2.
SINGAPORE_EVENT_ID = "replay:singapore-region-outage"

#: The synthetic critical vulnerability used by hero scenario #2.
CVE_EVENT_ID = f"replay:{DEMO_CVE_ID.lower()}"


def _replay_source(name: str, *, url: str | None = None, confidence: float = 0.9) -> DataSourceInfo:
    return DataSourceInfo(
        source_id="worldgraph-replay",
        source_name=name,
        source_url=url,
        mode=DataMode.REPLAY,
        confidence=confidence,
        observed_at=REPLAY_BASE_TIME,
        ingested_at=REPLAY_BASE_TIME,
    )


def taiwan_earthquake() -> WorldEvent:
    """M6.8 near Hsinchu — the hero scenario's originating event."""
    metadata = {
        "magnitude": 6.8,
        "depth_km": 18.0,
        "depth_band": "shallow",
        "place": "24 km SSE of Hsinchu, Taiwan",
        "tsunami": False,
        "replay": True,
    }
    return WorldEvent(
        id=TAIWAN_EVENT_ID,
        category=EventCategory.EARTHQUAKE,
        title="M6.8 earthquake — 24 km SSE of Hsinchu, Taiwan",
        description=(
            "Shallow magnitude 6.8 earthquake in the Hsinchu region. Replayed fixture "
            "used for the WorldGraph hero demonstration — not a live observation."
        ),
        severity=Severity.HIGH,
        location=GeoPoint(lat=24.6100, lon=121.0300, altitude=-18_000.0),
        exposure_radius_km=round(exposure_radius_for(EventCategory.EARTHQUAKE, metadata), 1),
        occurred_at=REPLAY_BASE_TIME,
        source=_replay_source("WorldGraph replay — Taiwan earthquake", confidence=0.9),
        metadata=metadata,
    )


def singapore_region_outage() -> WorldEvent:
    """A cloud-provider region impairment, named rather than geolocated.

    Provider status pages name a region; they do not give coordinates and a radius. This
    event therefore correlates through ``directly_named_entity_ids``, which exercises the
    non-geographic correlation path.
    """
    return WorldEvent(
        id=SINGAPORE_EVENT_ID,
        category=EventCategory.CLOUD_INCIDENT,
        title="Cloud provider: elevated error rates in ap-southeast-1 (Singapore)",
        description=(
            "Replayed provider incident. Increased API error rates and instance launch "
            "failures in a single region. Synthetic fixture — not a live provider status."
        ),
        severity=Severity.CRITICAL,
        location=GeoPoint(lat=1.3521, lon=103.8198),
        exposure_radius_km=0.0,
        occurred_at=REPLAY_BASE_TIME + timedelta(hours=2),
        source=_replay_source("WorldGraph replay — provider status", confidence=0.85),
        metadata={"provider": "aws", "region": "ap-southeast-1", "replay": True},
        directly_named_entity_ids=["cloud-region-singapore"],
    )


def demo_vulnerability() -> WorldEvent:
    """The synthetic critical vulnerability behind hero scenario #2.

    Deliberately fictional (a ``DEMO`` identifier that cannot collide with a real CVE) so
    nothing in this repository can be mistaken for a claim about a real product.
    """
    return WorldEvent(
        id=CVE_EVENT_ID,
        category=EventCategory.SECURITY_VULNERABILITY,
        title=f"{DEMO_CVE_ID} — unauthenticated RCE in atlas-gateway 3.4.x",
        description=(
            "SYNTHETIC vulnerability created for the WorldGraph security demonstration. "
            "Neither the identifier nor the affected product is real. Pre-authentication "
            "remote code execution in the request-routing layer of atlas-gateway 3.4.x."
        ),
        severity=Severity.CRITICAL,
        location=None,
        exposure_radius_km=0.0,
        occurred_at=REPLAY_BASE_TIME - timedelta(days=2),
        source=DataSourceInfo(
            source_id="worldgraph-synthetic",
            source_name="WorldGraph synthetic vulnerability",
            source_url=None,
            mode=DataMode.SYNTHETIC,
            confidence=1.0,
            observed_at=REPLAY_BASE_TIME - timedelta(days=2),
            ingested_at=REPLAY_BASE_TIME - timedelta(days=2),
        ),
        metadata={
            "cve_id": DEMO_CVE_ID,
            "cvss": 9.8,
            "vendor": "AtlasPay Platform",
            "product": "atlas-gateway",
            "product_names": ["atlas-gateway"],
            "affected_versions": "3.4.0 – 3.4.1",
            "fixed_version": "3.5.0",
            "known_ransomware_use": False,
            "synthetic": True,
        },
    )


def ambient_events() -> list[WorldEvent]:
    """Lower-severity background events so the feed is never empty in offline mode."""
    return [
        WorldEvent(
            id="replay:ambient-typhoon-luzon",
            category=EventCategory.SEVERE_WEATHER,
            title="Tropical storm warning — Luzon Strait",
            description="Replayed severe-weather advisory. Fixture data, not a live warning.",
            severity=Severity.MODERATE,
            location=GeoPoint(lat=20.5, lon=121.0),
            exposure_radius_km=exposure_radius_for(EventCategory.SEVERE_WEATHER, {}),
            occurred_at=REPLAY_BASE_TIME - timedelta(hours=6),
            source=_replay_source("WorldGraph replay — weather advisory", confidence=0.7),
            metadata={"replay": True},
        ),
        WorldEvent(
            id="replay:ambient-fiber-cut-frankfurt",
            category=EventCategory.NETWORK_OUTAGE,
            title="Regional transit degradation — Frankfurt metro",
            description=(
                "Replayed transit-provider degradation affecting one carrier's metro ring. "
                "Fixture data."
            ),
            severity=Severity.LOW,
            location=GeoPoint(lat=50.1109, lon=8.6821),
            exposure_radius_km=40.0,
            occurred_at=REPLAY_BASE_TIME - timedelta(hours=14),
            source=_replay_source("WorldGraph replay — transit status", confidence=0.6),
            metadata={"replay": True},
        ),
    ]


class ReplayEventAdapter(WorldDataAdapter):
    """Serves the deterministic demo events.

    Never polls: fixtures do not change, and a poll loop would only add a timer to the
    event loop for no benefit.
    """

    id = "worldgraph-replay"
    name = "WorldGraph demo scenarios"
    source_url = None
    mode = DataMode.REPLAY
    refresh_interval_seconds = 0.0
    stale_after_seconds = float("inf")

    async def fetch(self):
        events = [
            taiwan_earthquake(),
            singapore_region_outage(),
            demo_vulnerability(),
            *ambient_events(),
        ]
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events, []


#: Replay scenarios offered in the UI. Each names the event that starts it and the
#: overrides an operator would reach for next, so "Replay incident" is one click.
REPLAY_SCENARIOS: list[dict[str, object]] = [
    {
        "id": "taiwan-earthquake",
        "name": "Taiwan Earthquake Scenario",
        "summary": (
            "M6.8 near Hsinchu strikes AtlasPay's sole-source hardware supplier and its "
            "co-located edge facility."
        ),
        "event_id": TAIWAN_EVENT_ID,
        "focus_entity_ids": ["supplier-taiwan-hardware", "payments-k8s-singapore"],
        "suggested_overrides": [
            {"target_id": "supplier-taiwan-hardware", "health": "DOWN"},
            {"target_id": "payments-k8s-singapore", "health": "DOWN"},
        ],
    },
    {
        "id": "singapore-region-outage",
        "name": "Singapore Region Outage",
        "summary": (
            "A cloud provider impairs ap-southeast-1, taking the primary APAC payments "
            "stack and its transactional database with it."
        ),
        "event_id": SINGAPORE_EVENT_ID,
        "focus_entity_ids": ["cloud-region-singapore", "postgres-singapore"],
        "suggested_overrides": [
            {"target_id": "cloud-region-singapore", "health": "DOWN"},
        ],
    },
    {
        "id": "critical-cve",
        "name": "Critical CVE Scenario",
        "summary": (
            f"{DEMO_CVE_ID} affects four AtlasPay services. Only one is internet-facing — "
            "and it can reach the payments path."
        ),
        "event_id": CVE_EVENT_ID,
        "focus_entity_ids": ["admin-api", "payments-api"],
        "suggested_overrides": [
            {"target_id": "admin-api", "health": "DOWN"},
        ],
    },
]
