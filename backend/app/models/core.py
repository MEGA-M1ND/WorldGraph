"""WorldGraph core domain model.

Everything WorldGraph reasons about is one of three things:

  * a ``WorldEntity``      — a node in the world (a cluster, a supplier, an office…)
  * a ``DependencyEdge``   — a typed, weighted relationship between two entities
  * a ``WorldEvent``       — something that happened (an earthquake, a CVE, an outage)

Entities and edges are stored *separately* on purpose. A marker on a map cannot answer
"what breaks if this fails"; a graph can. Keeping edges out of the entity record is what
lets the blast-radius engine traverse without loading presentation state.

Provenance is not optional. Every entity and event carries a ``DataSourceInfo`` naming
where it came from and — critically — whether it is LIVE, REPLAY, SIMULATED or SYNTHETIC.
WorldGraph mixes real world signals with a synthetic enterprise estate, so a record that
cannot say which one it is would be a lie waiting to happen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes are banned across the domain."""
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class EntityType(str, Enum):
    """Kinds of node WorldGraph models.

    Deliberately a closed set: an open string field would let adapters invent types that
    the traversal, scoring and UI layers have no rules for.
    """

    ORGANIZATION = "ORGANIZATION"
    BUSINESS_SERVICE = "BUSINESS_SERVICE"
    APPLICATION = "APPLICATION"
    MICROSERVICE = "MICROSERVICE"
    DATABASE = "DATABASE"
    KUBERNETES_CLUSTER = "KUBERNETES_CLUSTER"
    CLOUD_REGION = "CLOUD_REGION"
    DATACENTER = "DATACENTER"
    OFFICE = "OFFICE"
    SUPPLIER = "SUPPLIER"
    FACTORY = "FACTORY"
    CUSTOMER_REGION = "CUSTOMER_REGION"
    NETWORK_NODE = "NETWORK_NODE"
    EXTERNAL_API = "EXTERNAL_API"
    SECURITY_FINDING = "SECURITY_FINDING"
    WORLD_EVENT = "WORLD_EVENT"


#: Entity types that represent something with a physical footprint an earthquake,
#: wildfire or flood can actually hit. Cloud regions count: a region is a real building.
PHYSICAL_ENTITY_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.CLOUD_REGION,
        EntityType.DATACENTER,
        EntityType.OFFICE,
        EntityType.SUPPLIER,
        EntityType.FACTORY,
        EntityType.NETWORK_NODE,
    }
)

#: Entity types that are logical/software. A quake does not hit a microservice directly;
#: it hits the site that hosts it, and the effect arrives through the graph.
LOGICAL_ENTITY_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.BUSINESS_SERVICE,
        EntityType.APPLICATION,
        EntityType.MICROSERVICE,
        EntityType.DATABASE,
        EntityType.KUBERNETES_CLUSTER,
        EntityType.EXTERNAL_API,
    }
)


class HealthState(str, Enum):
    """Observed operational health.

    The numeric equivalents used by the impact model live in
    :data:`HEALTH_VALUES`; see ``docs/IMPACT_MODEL.md``.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SEVERELY_DEGRADED = "SEVERELY_DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


#: WorldGraph V1 Impact Model — health as an availability multiplier in [0, 1].
#: UNKNOWN is deliberately optimistic-but-not-perfect (0.9): treating unknown as healthy
#: hides risk, treating it as down invents outages.
HEALTH_VALUES: dict[HealthState, float] = {
    HealthState.HEALTHY: 1.0,
    HealthState.DEGRADED: 0.7,
    HealthState.SEVERELY_DEGRADED: 0.3,
    HealthState.DOWN: 0.0,
    HealthState.UNKNOWN: 0.9,
}


def health_from_value(value: float) -> HealthState:
    """Map a continuous availability back onto the nearest discrete health state.

    Thresholds are the midpoints between the :data:`HEALTH_VALUES` anchors, so the
    round-trip ``health_from_value(HEALTH_VALUES[h]) == h`` holds for every real state.
    """
    if value <= 0.15:
        return HealthState.DOWN
    if value <= 0.5:
        return HealthState.SEVERELY_DEGRADED
    if value <= 0.85:
        return HealthState.DEGRADED
    return HealthState.HEALTHY


class Criticality(str, Enum):
    """How much the business cares about this entity.

    ``UNKNOWN`` is not a low criticality — it is the absence of a judgement. An imported
    cloud inventory knows a cluster exists; it does not know whether the business would
    notice losing it. Defaulting such an entity to ``MEDIUM`` would let WorldGraph invent
    a business classification and then score points for it, which is exactly the
    fabrication the Reality Pass audit found (docs/REALITY_PASS_AUDIT.md, B4).
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


#: Weight used when a blast radius is scored. Multiplicative, not additive, so a long
#: chain of LOW entities never out-scores one CRITICAL customer-facing service.
#:
#: ``UNKNOWN`` weighs 0: an undeclared criticality contributes nothing to a risk score.
#: It is surfaced as a *coverage gap* instead, which is information the operator can act
#: on, rather than a number they cannot audit.
CRITICALITY_WEIGHTS: dict[Criticality, float] = {
    Criticality.CRITICAL: 1.0,
    Criticality.HIGH: 0.75,
    Criticality.MEDIUM: 0.45,
    Criticality.LOW: 0.2,
    Criticality.UNKNOWN: 0.0,
}

#: Ordering for "which of these is most critical". UNKNOWN sorts last: it is not a
#: severity, so it must never win a "worst affected" comparison against a declared one.
CRITICALITY_ORDER: tuple[Criticality, ...] = (
    Criticality.CRITICAL,
    Criticality.HIGH,
    Criticality.MEDIUM,
    Criticality.LOW,
    Criticality.UNKNOWN,
)


class DataMode(str, Enum):
    """Provenance mode. Rendered as a badge on every record in the UI.

    This is the single most important honesty control in the product: it is what stops
    a synthetic estate or a what-if scenario from ever reading as measured reality.
    """

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    SIMULATED = "SIMULATED"
    SYNTHETIC = "SYNTHETIC"


class FeedState(str, Enum):
    """Honest state of a data feed.

    Modelled on God's Eye View's ``layerFeedState`` normalization (MIT) — the insight
    worth keeping is that a feed has exactly one state and guidance states are not faults.
    """

    LOADING = "LOADING"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    FALLBACK = "FALLBACK"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


class EventCategory(str, Enum):
    """What kind of world signal an event is."""

    EARTHQUAKE = "EARTHQUAKE"
    WILDFIRE = "WILDFIRE"
    SEVERE_WEATHER = "SEVERE_WEATHER"
    FLOOD = "FLOOD"
    POWER_OUTAGE = "POWER_OUTAGE"
    NETWORK_OUTAGE = "NETWORK_OUTAGE"
    CLOUD_INCIDENT = "CLOUD_INCIDENT"
    SERVICE_INCIDENT = "SERVICE_INCIDENT"
    SECURITY_VULNERABILITY = "SECURITY_VULNERABILITY"
    SUPPLY_CHAIN = "SUPPLY_CHAIN"
    OTHER = "OTHER"


class Severity(str, Enum):
    """Normalized severity band shared by events, analyses and risks."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    INFO = "INFO"


#: Risk score → band. Boundaries are inclusive-lower, exclusive-upper except CRITICAL.
#: Documented in ``docs/IMPACT_MODEL.md`` and pinned by boundary tests.
RISK_BANDS: tuple[tuple[float, float, Severity], ...] = (
    (0.0, 25.0, Severity.LOW),
    (25.0, 50.0, Severity.MODERATE),
    (50.0, 75.0, Severity.HIGH),
    (75.0, 100.01, Severity.CRITICAL),
)


def severity_from_score(score: float) -> Severity:
    """Map a 0-100 risk score onto its band. See ``docs/IMPACT_MODEL.md``."""
    clamped = max(0.0, min(100.0, float(score)))
    for low, high, band in RISK_BANDS:
        if low <= clamped < high:
            return band
    return Severity.CRITICAL


class DependencyType(str, Enum):
    """Typed edges. Direction is always *dependent → dependency*.

    ``payments-api DEPENDS_ON postgres-singapore`` means the edge's source is
    ``payments-api``. Failure therefore propagates **backwards** along edges: losing the
    target degrades the source. Traversal code names this explicitly rather than relying
    on the reader's intuition.
    """

    DEPENDS_ON = "DEPENDS_ON"
    HOSTED_IN = "HOSTED_IN"
    CONNECTS_TO = "CONNECTS_TO"
    SUPPLIED_BY = "SUPPLIED_BY"
    SERVES = "SERVES"
    REPLICATES_TO = "REPLICATES_TO"


#: Edges along which failure propagates from target to source. ``SERVES`` is excluded:
#: it points from provider to consumer, so it is followed in the *forward* direction and
#: handled separately by the traversal layer.
FAILURE_PROPAGATING_TYPES: frozenset[DependencyType] = frozenset(
    {
        DependencyType.DEPENDS_ON,
        DependencyType.HOSTED_IN,
        DependencyType.CONNECTS_TO,
        DependencyType.SUPPLIED_BY,
    }
)


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


class GeoPoint(BaseModel):
    """A point on Earth. Altitude is metres above the WGS84 ellipsoid."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    altitude: float | None = None


class DataSourceInfo(BaseModel):
    """Where a record came from and how much we should trust it."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=128)
    source_url: str | None = Field(default=None, max_length=512)
    mode: DataMode = DataMode.SYNTHETIC
    #: 0-1. How much the *source* is trusted, before analysis-specific confidence.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: When the source observed the fact (may predate ingestion by a long way).
    observed_at: datetime | None = None
    #: When WorldGraph took delivery of it.
    ingested_at: datetime = Field(default_factory=utcnow)

    def freshness_seconds(self, *, now: datetime | None = None) -> float | None:
        """Age in seconds against ``observed_at``, or ``None`` if unobserved."""
        if self.observed_at is None:
            return None
        reference = now or utcnow()
        return max(0.0, (reference - self.observed_at).total_seconds())


class SoftwareComponent(BaseModel):
    """One installed software package on an entity — the join key for CVE matching.

    Kept minimal on purpose: WorldGraph is not an SBOM tool. It only needs enough to
    answer "which of my assets run the thing this CVE affects, and can it be reached?"
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="", max_length=64)
    vendor: str = Field(default="", max_length=128)
    #: Vulnerability identifiers known to affect this component on this asset.
    cve_ids: list[str] = Field(default_factory=list)


class ExposureProfile(BaseModel):
    """Network reachability facts used by the security blast radius."""

    model_config = ConfigDict(extra="forbid")

    internet_facing: bool = False
    #: Free-form zone label (``dmz``, ``internal``, ``restricted``). Compared as a string;
    #: WorldGraph does not model a full network policy engine in V1.
    network_zone: str = Field(default="internal", max_length=64)
    #: Whether the asset requires authentication before reaching sensitive functions.
    #:
    #: Tri-state, and ``None`` is the default because for most sources it is the truth.
    #: Cloud inventory does not report whether a workload authenticates its callers, so an
    #: importer that set ``True`` would be asserting a security property from nothing —
    #: and asserting it in the direction that makes an estate look safer. Establishing
    #: this needs configuration or telemetry WorldGraph does not currently read.
    authenticated: bool | None = None


class BusinessProfile(BaseModel):
    """Business facts attached to an entity, used by the impact model.

    Every number here is an *input to a model*, never an accounting figure. The API and
    UI label all derived outputs ``MODELLED ESTIMATE``.

    **``None`` means "not declared", and it is not the same as zero.** Zero revenue is a
    measurement; unknown revenue is the absence of one. Before the Reality Pass these
    fields defaulted to ``0``, so an adapter that knew nothing was forced to assert five
    business facts and the engines could not tell an empty estate from a worthless one
    (docs/REALITY_PASS_AUDIT.md, B3). A synthetic estate like AtlasPay declares all of
    them; an imported cloud inventory declares almost none.
    """

    model_config = ConfigDict(extra="forbid")

    #: Share of total organisation traffic this entity carries, 0-1. ``None`` = undeclared.
    traffic_share: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Modelled revenue attributable to this entity per hour. ``None`` = undeclared.
    revenue_per_hour: float | None = Field(default=None, ge=0.0)
    #: Customers served. ``None`` = undeclared.
    customer_count: int | None = Field(default=None, ge=0)
    region: str = Field(default="", max_length=64)
    sla_tier: str = Field(default="", max_length=32)
    #: Normalized capacity headroom, 0-1. 1.0 = can absorb its own full load again.
    #: Unlike the business fields this keeps a concrete default: capacity is a *physical*
    #: property the propagation solver must have a value for, and "runs at its own rated
    #: load" is the only defensible assumption for an asset that is up.
    capacity: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Number of independent replicas/sites. ``None`` = undeclared; 1 means a single
    #: point of failure, which is a finding, so it must not be the value we invent.
    redundancy: int | None = Field(default=None, ge=0)

    @property
    def has_traffic(self) -> bool:
        return self.traffic_share is not None

    @property
    def has_revenue(self) -> bool:
        return self.revenue_per_hour is not None

    @property
    def has_customers(self) -> bool:
        return self.customer_count is not None

    @property
    def is_single_point_of_failure(self) -> bool | None:
        """True / False / ``None`` when redundancy was never declared."""
        if self.redundancy is None:
            return None
        return self.redundancy <= 1


# --------------------------------------------------------------------------------------
# Aggregate records
# --------------------------------------------------------------------------------------


class WorldEntity(BaseModel):
    """A node in the world model."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    type: EntityType
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=1024)

    location: GeoPoint | None = None
    health: HealthState = HealthState.UNKNOWN
    #: UNKNOWN by default. A criticality WorldGraph chose is a business judgement it was
    #: not entitled to make; the demo fixture declares one for every entity.
    criticality: Criticality = Criticality.UNKNOWN

    business: BusinessProfile = Field(default_factory=BusinessProfile)
    exposure: ExposureProfile = Field(default_factory=ExposureProfile)
    software: list[SoftwareComponent] = Field(default_factory=list)

    #: Whether users outside the organisation notice this entity failing. ``None`` means
    #: nobody declared it. Entity *type* is not evidence: before the Reality Pass every
    #: APPLICATION was assumed customer-facing, which silently promoted every imported
    #: Azure App Service and was worth 25 risk points on no evidence at all
    #: (docs/REALITY_PASS_AUDIT.md, B5).
    customer_facing: bool | None = None

    #: Free-form adapter-specific detail. Never used by scoring — anything the engines
    #: read gets a typed home above, so a scoring rule is always greppable.
    metadata: dict[str, Any] = Field(default_factory=dict)

    source: DataSourceInfo
    observed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Ids are slugs. Anything else is an adapter bug, and ids end up in URLs."""
        if not all(ch.isalnum() or ch in "-_.:" for ch in value):
            raise ValueError(
                "entity id must contain only alphanumerics, '-', '_', '.' or ':'"
            )
        return value

    @property
    def is_physical(self) -> bool:
        """True when this entity has a footprint a physical event can strike."""
        return self.type in PHYSICAL_ENTITY_TYPES

    @property
    def health_value(self) -> float:
        """Availability multiplier for the V1 impact model."""
        return HEALTH_VALUES[self.health]


class DependencyEdge(BaseModel):
    """A typed relationship between two entities."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    source_entity_id: str = Field(min_length=1, max_length=128)
    target_entity_id: str = Field(min_length=1, max_length=128)
    type: DependencyType

    #: 0-1. How much of the source's function the target actually carries. A 1.0
    #: DEPENDS_ON is a hard dependency; 0.3 is "degrades a feature".
    criticality: float = Field(default=1.0, ge=0.0, le=1.0)
    #: 0-1. Independent alternative capacity for this dependency. 0 = no failover;
    #: 1 = fully redundant, the edge alone cannot take the source down.
    redundancy: float = Field(default=0.0, ge=0.0, le=1.0)
    #: 0-1, or ``None`` to mirror ``criticality``. How much of the source's *serviceable
    #: capacity* erodes when the target is unavailable, as distinct from how much of its
    #: current function stops.
    #:
    #: The two come apart precisely where supply chains do. Losing a hardware supplier
    #: does not switch a running cluster off today (small ``criticality``) but it does
    #: remove the ability to replace failed nodes or grow (large ``capacity_impact``).
    #: Conflating them is how naive models turn "constrained replacement capacity" into a
    #: fictional outage — so WorldGraph keeps them separate and reports both.
    capacity_impact: float | None = Field(default=None, ge=0.0, le=1.0)

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_entity_id")
    @classmethod
    def _no_self_edges(cls, value: str, info: Any) -> str:
        """A self-edge is always a data bug and would poison cycle detection."""
        if value == info.data.get("source_entity_id"):
            raise ValueError("an entity cannot depend on itself")
        return value

    @property
    def propagates_failure(self) -> bool:
        """True when losing the *target* degrades the *source*."""
        return self.type in FAILURE_PROPAGATING_TYPES

    @property
    def effective_capacity_impact(self) -> float:
        """Capacity coupling, defaulting to the availability coupling when unset."""
        return self.criticality if self.capacity_impact is None else self.capacity_impact


class WorldEvent(BaseModel):
    """Something that happened in the world, normalized across adapters."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=192)
    category: EventCategory
    title: str = Field(min_length=1, max_length=512)
    #: Free text from an external feed. **Untrusted.** Sanitized on ingest and never
    #: interpolated into a system prompt — see ``docs/SECURITY.md``.
    description: str = Field(default="", max_length=4096)

    severity: Severity = Severity.INFO
    location: GeoPoint | None = None
    #: Radius in km within which the event can plausibly affect physical assets.
    #: Derived per-category by the adapter; see ``geo/exposure.py``.
    exposure_radius_km: float = Field(default=0.0, ge=0.0)

    occurred_at: datetime
    source: DataSourceInfo

    #: Category-specific normalized facts (magnitude, depth_km, cve_id, cvss…).
    metadata: dict[str, Any] = Field(default_factory=dict)

    #: Entity ids the adapter already knows are affected (e.g. a cloud status page
    #: naming a region). Geospatial correlation adds to this, it does not replace it.
    directly_named_entity_ids: list[str] = Field(default_factory=list)

    updated_at: datetime = Field(default_factory=utcnow)


class FeedStatus(BaseModel):
    """Reported state of one data adapter."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    adapter_name: str
    state: FeedState
    mode: DataMode
    #: Records currently held by the adapter.
    record_count: int = 0
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    #: Present only when the state is DEGRADED / UNAVAILABLE / FALLBACK. Sanitized —
    #: never a raw upstream body, never a stack trace, never a secret.
    message: str | None = Field(default=None, max_length=512)
    source_url: str | None = None

    def freshness_seconds(self, *, now: datetime | None = None) -> float | None:
        """Seconds since the last successful refresh."""
        if self.last_success_at is None:
            return None
        return max(0.0, ((now or utcnow()) - self.last_success_at).total_seconds())
