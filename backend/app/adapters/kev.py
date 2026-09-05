"""CISA Known Exploited Vulnerabilities adapter.

Source: the CISA KEV catalog. US Government work in the public domain, no key required.

KEV entries are the highest-signal vulnerability feed available for free: every entry is
a vulnerability *known to be exploited in the wild*, which is a far stronger statement than
a CVSS score. WorldGraph only surfaces the subset that matches AtlasPay's declared software
inventory — the product's whole thesis is that a CVE nobody runs is not a risk, and shipping
the raw catalog would just be another CVE dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from ..models.core import (
    DataMode,
    DataSourceInfo,
    EventCategory,
    Severity,
    WorldEvent,
    utcnow,
)
from ..security.sanitize import sanitize_identifier, sanitize_text
from .base import AdapterError, WorldDataAdapter

FEED_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

NVD_URL_TEMPLATE = "https://nvd.nist.gov/vuln/detail/{cve_id}"

#: Cap on entries held in memory. The catalog is well over a thousand entries and the
#: product only needs the recent tail plus whatever matches the inventory.
MAX_ENTRIES = 400


def normalize_vulnerability(
    entry: object, *, mode: DataMode = DataMode.LIVE
) -> WorldEvent | None:
    """Turn one KEV catalog entry into a :class:`WorldEvent`."""
    if not isinstance(entry, dict):
        return None
    cve_id = sanitize_identifier(entry.get("cveID"), max_length=32).upper()
    if not cve_id.startswith("CVE-"):
        return None

    vendor = sanitize_text(entry.get("vendorProject"), max_length=128)
    product = sanitize_text(entry.get("product"), max_length=128)
    name = sanitize_text(entry.get("vulnerabilityName"), max_length=256)

    added_raw = entry.get("dateAdded")
    try:
        added = datetime.combine(
            datetime.strptime(str(added_raw), "%Y-%m-%d").date(), time.min, tzinfo=UTC
        )
    except (TypeError, ValueError):
        added = utcnow()

    ransomware = str(entry.get("knownRansomwareCampaignUse", "")).strip().lower() == "known"

    return WorldEvent(
        id=f"kev:{cve_id}",
        category=EventCategory.SECURITY_VULNERABILITY,
        title=f"{cve_id} — {name or product or 'Known exploited vulnerability'}",
        # The catalog's own prose. Untrusted like any external text, sanitized on the way in.
        description=sanitize_text(entry.get("shortDescription"), max_length=1024),
        # Every KEV entry is known-exploited. That is a stronger signal than a CVSS band,
        # so KEV membership alone earns HIGH, and confirmed ransomware use earns CRITICAL.
        severity=Severity.CRITICAL if ransomware else Severity.HIGH,
        location=None,
        exposure_radius_km=0.0,  # correlates by software inventory, not geography
        occurred_at=added,
        source=DataSourceInfo(
            source_id="cisa-kev",
            source_name="CISA Known Exploited Vulnerabilities",
            source_url=NVD_URL_TEMPLATE.format(cve_id=cve_id),
            mode=mode,
            confidence=0.95,
            observed_at=added,
            ingested_at=utcnow(),
        ),
        metadata={
            "cve_id": cve_id,
            "vendor": vendor,
            "product": product,
            "product_names": [name for name in {product.lower(), vendor.lower()} if name],
            "known_ransomware_use": ransomware,
            "required_action": sanitize_text(entry.get("requiredAction"), max_length=512),
            "due_date": sanitize_text(entry.get("dueDate"), max_length=32),
        },
    )


class CisaKevAdapter(WorldDataAdapter):
    """The CISA KEV catalog, refreshed hourly."""

    id = "cisa-kev"
    name = "CISA Known Exploited Vulnerabilities"
    source_url = FEED_URL
    mode = DataMode.LIVE
    refresh_interval_seconds = 3600.0
    stale_after_seconds = 6 * 3600.0

    def __init__(self, *, feed_url: str = FEED_URL, enabled: bool = True) -> None:
        super().__init__()
        self.feed_url = feed_url
        self.source_url = feed_url
        self._enabled = enabled

    async def fetch(self):
        if not self._enabled:
            raise AdapterError("CISA KEV feed is disabled in this deployment")
        payload = await self.http_get_json(self.feed_url, timeout=20.0)
        if not isinstance(payload, dict):
            raise AdapterError("CISA KEV returned an unexpected payload shape")
        entries = payload.get("vulnerabilities")
        if not isinstance(entries, list):
            raise AdapterError("CISA KEV response contained no vulnerability list")

        events: list[WorldEvent] = []
        for entry in entries:
            event = normalize_vulnerability(entry, mode=self.mode)
            if event is not None:
                events.append(event)
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events[:MAX_ENTRIES], []
