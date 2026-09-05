"""Analysis, simulation and response-plan schemas.

Everything the analysis engines emit is explainable by construction: no score exists
without the list of contributions that produced it, and no impacted entity exists without
the path that reached it. "Risk is HIGH because the model said so" is not a permitted
output shape in this codebase.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .core import (
    Criticality,
    DataMode,
    DependencyType,
    HealthState,
    Severity,
    utcnow,
)

# --------------------------------------------------------------------------------------
# Explanation primitives
# --------------------------------------------------------------------------------------


class ScoreContribution(BaseModel):
    """One additive term in a risk score. The audit trail of a number."""

    model_config = ConfigDict(extra="forbid")

    #: Machine-stable key, e.g. ``asset_criticality``. Tests assert on this, not on label.
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    points: float
    detail: str = Field(default="", max_length=512)


class PathHop(BaseModel):
    """One step along an impact path."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_name: str
    edge_type: DependencyType | None = None
    #: Availability of this hop after propagation, 0-1.
    availability: float = Field(ge=0.0, le=1.0)


class ImpactPath(BaseModel):
    """A concrete, walkable explanation of how an impact reached an entity."""

    model_config = ConfigDict(extra="forbid")

    hops: list[PathHop]
    #: Availability at the far end of the path.
    terminal_availability: float = Field(ge=0.0, le=1.0)

    @property
    def depth(self) -> int:
        """Number of edges traversed."""
        return max(0, len(self.hops) - 1)

    def as_text(self) -> str:
        """Human-readable arrow chain, used in explanations and the timeline."""
        return " → ".join(hop.entity_name for hop in self.hops)


class ImpactedEntity(BaseModel):
    """An entity the propagation reached, with why and how badly."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_name: str
    entity_type: str
    criticality: Criticality
    #: 0 = the origin of the impact, 1 = direct dependent, ≥2 = transitive.
    depth: int = Field(ge=0)
    #: Modelled availability after propagation, 0-1.
    availability: float = Field(ge=0.0, le=1.0)
    #: Availability lost relative to this entity's own baseline health.
    availability_delta: float
    projected_health: HealthState
    #: Shortest/worst path that explains this entity's state.
    path: ImpactPath
    customer_facing: bool = False


class CustomerExposure(BaseModel):
    """Modelled effect on one customer region. MODELLED ESTIMATE, never accounting."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    region: str
    customer_count: int
    #: Fraction of this region's traffic degraded, 0-1.
    traffic_impact: float = Field(ge=0.0, le=1.0)
    #: Availability the region's users would see, 0-1.
    projected_availability: float = Field(ge=0.0, le=1.0)
    via_service_ids: list[str] = Field(default_factory=list)


class BusinessImpact(BaseModel):
    """Aggregate modelled business consequence. Always labelled as an estimate."""

    model_config = ConfigDict(extra="forbid")

    #: Organisation-wide availability, weighted by traffic share, 0-1.
    availability: float = Field(ge=0.0, le=1.0)
    #: Fraction of total organisation traffic degraded, 0-1.
    traffic_impact: float = Field(ge=0.0, le=1.0)
    customers_affected: int = Field(ge=0)
    #: Modelled revenue at risk per hour while the condition holds.
    revenue_at_risk_per_hour: float = Field(ge=0.0)
    #: Ids of entities whose SLA tier is breached under the modelled availability.
    sla_breaches: list[str] = Field(default_factory=list)
    critical_services_impacted: int = Field(ge=0)
    customer_regions_impacted: int = Field(ge=0)
    #: Present on every instance; the API contract guarantees the caller sees it.
    disclaimer: Literal["MODELLED ESTIMATE"] = "MODELLED ESTIMATE"


class Confidence(BaseModel):
    """Honest uncertainty attached to every analysis."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    strong_evidence: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class RiskScore(BaseModel):
    """A 0-100 score with its full derivation."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=100.0)
    severity: Severity
    contributions: list[ScoreContribution]

    def as_text(self) -> str:
        """``+30 critical asset`` style breakdown used verbatim in the UI."""
        lines = [f"{self.severity.value} ({self.score:.0f}/100)"]
        for item in self.contributions:
            lines.append(f"{item.points:+.0f} {item.label}")
        return "\n".join(lines)


class BlastRadiusResult(BaseModel):
    """The output of the deterministic blast-radius engine."""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: What kicked this off — an event id, an entity id, or a scenario id.
    origin_kind: Literal["EVENT", "ENTITY", "SCENARIO", "SECURITY_FINDING"]
    origin_ids: list[str]
    origin_label: str

    severity: Severity
    risk: RiskScore

    direct_impact: list[ImpactedEntity] = Field(default_factory=list)
    indirect_impact: list[ImpactedEntity] = Field(default_factory=list)
    critical_paths: list[ImpactPath] = Field(default_factory=list)
    customer_exposure: list[CustomerExposure] = Field(default_factory=list)
    business_impact: BusinessImpact

    explanations: list[str] = Field(default_factory=list)
    confidence: Confidence

    #: Truncation is visible, never silent: if traversal hit its depth or node budget
    #: the caller must be able to say so.
    truncated: bool = False
    truncation_reason: str | None = None
    #: Entity ids participating in a dependency cycle that traversal had to break.
    cycles_detected: list[list[str]] = Field(default_factory=list)

    mode: DataMode = DataMode.SYNTHETIC
    computed_at: datetime = Field(default_factory=utcnow)
    #: Wall-clock cost of the calculation, for the performance budget in the README.
    duration_ms: float = 0.0

    @property
    def total_impacted(self) -> int:
        """Entities reached, excluding the origin itself."""
        return len(self.direct_impact) + len(self.indirect_impact)


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------


class OverrideKind(str, Enum):
    """What a single what-if override changes."""

    ENTITY_HEALTH = "ENTITY_HEALTH"
    ENTITY_CAPACITY = "ENTITY_CAPACITY"
    EDGE_DISABLED = "EDGE_DISABLED"


class SimulationOverride(BaseModel):
    """One hypothetical change layered over the real world state.

    Overrides never mutate stored entities. They are applied to a *copy* of the world
    inside the simulation engine, which is what makes "compare against baseline" honest.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: OverrideKind
    #: Entity id for entity overrides; edge id for ``EDGE_DISABLED``.
    target_id: str = Field(min_length=1, max_length=256)
    health: HealthState | None = None
    #: 0-1 capacity for ``ENTITY_CAPACITY``.
    capacity: float | None = Field(default=None, ge=0.0, le=1.0)
    note: str = Field(default="", max_length=256)

    def describe(self) -> str:
        """One-line human summary, e.g. ``supplier-taiwan-hw = DOWN``."""
        if self.kind is OverrideKind.ENTITY_HEALTH and self.health is not None:
            return f"{self.target_id} = {self.health.value}"
        if self.kind is OverrideKind.ENTITY_CAPACITY and self.capacity is not None:
            return f"{self.target_id} capacity = {self.capacity * 100:.0f}%"
        return f"{self.target_id} link disabled"


class SimulationScenario(BaseModel):
    """A named set of overrides."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1024)
    overrides: list[SimulationOverride] = Field(default_factory=list)
    #: The event, if any, that motivated this scenario.
    origin_event_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    mode: Literal[DataMode.SIMULATED] = DataMode.SIMULATED


class WorldSnapshotMetrics(BaseModel):
    """The headline numbers for one world state (baseline or simulated)."""

    model_config = ConfigDict(extra="forbid")

    availability: float = Field(ge=0.0, le=1.0)
    #: Serviceable share of capacity in each named region, 0-1.
    regional_capacity: dict[str, float] = Field(default_factory=dict)
    critical_services_impacted: int = 0
    customer_regions_impacted: int = 0
    customers_affected: int = 0
    revenue_at_risk_per_hour: float = 0.0
    material_risk: Severity = Severity.LOW
    disclaimer: Literal["MODELLED ESTIMATE"] = "MODELLED ESTIMATE"


class MetricDelta(BaseModel):
    """One row of the CURRENT vs SIMULATION table."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    baseline: str
    simulated: str
    #: ``worse`` | ``better`` | ``same`` — drives colour, so it is computed, not guessed.
    direction: Literal["worse", "better", "same"]


class SimulationComparison(BaseModel):
    """Baseline vs simulated world, with cascading failure paths."""

    model_config = ConfigDict(extra="forbid")

    scenario: SimulationScenario
    baseline: WorldSnapshotMetrics
    simulated: WorldSnapshotMetrics
    deltas: list[MetricDelta]
    #: Entities degraded by the scenario that were healthy in the baseline.
    newly_impacted: list[ImpactedEntity] = Field(default_factory=list)
    cascade_paths: list[ImpactPath] = Field(default_factory=list)
    blast_radius: BlastRadiusResult | None = None
    computed_at: datetime = Field(default_factory=utcnow)
    duration_ms: float = 0.0


# --------------------------------------------------------------------------------------
# Response plan
# --------------------------------------------------------------------------------------


class Urgency(str, Enum):
    NOW = "NOW"
    SOON = "SOON"
    MONITOR = "MONITOR"


class ResponseAction(BaseModel):
    """One recommended action. Recommendation only — V1 never executes anything."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=512)
    #: Why this action exists, derived from the analysis. Mandatory by schema: an action
    #: without a rationale is not shippable in this product.
    rationale: str = Field(min_length=1, max_length=1024)
    affected_entities: list[str] = Field(default_factory=list)
    urgency: Urgency
    confidence: float = Field(ge=0.0, le=1.0)
    requires_approval: bool = True
    #: True for every V1 action. Present so the UI can state it per-row rather than
    #: relying on a footnote nobody reads.
    executed: Literal[False] = False


class ResponsePlan(BaseModel):
    """A structured response plan generated from analysis output."""

    model_config = ConfigDict(extra="forbid")

    id: str
    summary: str
    objectives: list[str] = Field(default_factory=list)
    actions: list[ResponseAction] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    #: ``deterministic`` when produced by the rule engine, ``llm-assisted`` when a model
    #: rewrote the prose around the same structured actions.
    generator: Literal["deterministic", "llm-assisted"] = "deterministic"


# --------------------------------------------------------------------------------------
# Timeline & material risk
# --------------------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    """One line in the incident timeline. Written by engines, not by the UI."""

    model_config = ConfigDict(extra="forbid")

    id: str
    at: datetime
    #: ``ingest`` | ``normalize`` | ``correlate`` | ``analyze`` | ``risk`` | ``simulate``
    #: | ``plan`` | ``feed`` | ``ai``
    stage: str
    message: str = Field(max_length=512)
    severity: Severity = Severity.INFO
    entity_ids: list[str] = Field(default_factory=list)
    event_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MaterialRisk(BaseModel):
    """A standing, structural risk surfaced in executive mode.

    Distinct from an incident: these exist whether or not anything is on fire, and are
    derived from graph structure (concentration, single points of failure, exposure).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    severity: Severity
    summary: str
    #: Entities to fly the camera to when the risk is clicked.
    focus_entity_ids: list[str] = Field(default_factory=list)
    contributions: list[ScoreContribution] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=100.0)
