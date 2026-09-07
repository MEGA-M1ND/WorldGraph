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
    #: Tri-state. ``None`` means nobody declared it — see business_impact.is_customer_facing.
    customer_facing: bool | None = None


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
    """Aggregate modelled business consequence. Always labelled as an estimate.

    **``None`` means WorldGraph does not know, and it is never rendered as a number.**
    The Reality Pass found this class returning ``availability = 1.0`` and
    ``revenue_at_risk = 0`` for an estate it had just modelled as entirely degraded,
    purely because the estate declared no customer regions
    (docs/REALITY_PASS_AUDIT.md, B1/B2). Every nullable field below is one WorldGraph used
    to invent, and :attr:`unknown_reasons` says why each is missing.
    """

    model_config = ConfigDict(extra="forbid")

    #: Availability *as customers experience it* — traffic-weighted across customer
    #: regions. ``None`` when no customer regions or traffic shares are declared, because
    #: there is then nothing to weight and no customer to speak for.
    availability: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Fraction of total organisation traffic degraded, 0-1. ``None`` when undeclared.
    traffic_impact: float | None = Field(default=None, ge=0.0, le=1.0)
    customers_affected: int | None = Field(default=None, ge=0)
    #: Modelled revenue at risk per hour while the condition holds. ``None`` when the
    #: estate declares no revenue metadata.
    revenue_at_risk_per_hour: float | None = Field(default=None, ge=0.0)

    #: Availability of the *infrastructure itself*, weighted by declared traffic where
    #: available and otherwise counted evenly. Always computable, because it needs only
    #: the graph — which is precisely what a cloud import does give us.
    infrastructure_availability: float = Field(ge=0.0, le=1.0)

    #: Ids of entities whose SLA tier is breached under the modelled availability.
    sla_breaches: list[str] = Field(default_factory=list)
    #: Countable facts. These need no business metadata, so they are never ``None``.
    critical_services_impacted: int = Field(ge=0)
    customer_regions_impacted: int = Field(ge=0)
    impacted_entity_count: int = Field(default=0, ge=0)

    #: One entry per figure WorldGraph could not compute, naming the missing input.
    #: The UI renders these beside the UNKNOWNs so a gap is actionable, not mysterious.
    unknown_reasons: list[str] = Field(default_factory=list)

    #: Present on every instance; the API contract guarantees the caller sees it.
    disclaimer: Literal["MODELLED ESTIMATE"] = "MODELLED ESTIMATE"

    @property
    def has_customer_view(self) -> bool:
        """Whether a customer-experienced availability could be computed at all."""
        return self.availability is not None


class VulnerabilityAssessment(str, Enum):
    """What WorldGraph can honestly conclude about a vulnerability and an estate.

    The distinction that matters is between the last two. Before this existed, an estate
    with **no software inventory at all** produced the same answer as an estate that had
    been searched and found clean: risk 0, severity LOW, and "deterministic correlation
    found no exposed asset" offered as *strong evidence*. For a security product that is
    the most dangerous output shape available — a confident all-clear derived from having
    looked at nothing.

    This is the Reality Pass thesis applied where it was missed: absent evidence and
    negative evidence are different facts and must not share a representation.
    """

    #: An asset's own inventory names this CVE. The strongest claim available.
    CONFIRMED_AFFECTED = "CONFIRMED_AFFECTED"
    #: A product name matched, but nothing confirmed the version or vendor. A candidate
    #: for triage, not a finding — see ``docs/SECURITY.md``.
    POTENTIALLY_AFFECTED = "POTENTIALLY_AFFECTED"
    #: Adequate inventory was searched and nothing matched. A real negative.
    NOT_AFFECTED = "NOT_AFFECTED"
    #: WorldGraph could not reach a conclusion, because it has too little inventory to
    #: support one. **Never rendered as "safe".**
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    @property
    def is_conclusive_negative(self) -> bool:
        return self is VulnerabilityAssessment.NOT_AFFECTED


class InventoryCoverage(BaseModel):
    """How much of an estate WorldGraph could actually search for software.

    A negative vulnerability conclusion is only as good as the inventory behind it, so the
    inventory is reported alongside every such conclusion rather than assumed.
    """

    model_config = ConfigDict(extra="forbid")

    #: Entities whose type could plausibly run software (a region or a customer segment
    #: cannot, and counting them would understate coverage).
    assessable_entities: int = Field(ge=0)
    #: Of those, how many declare any software component at all.
    entities_with_inventory: int = Field(ge=0)

    @property
    def ratio(self) -> float:
        if self.assessable_entities <= 0:
            return 0.0
        return self.entities_with_inventory / self.assessable_entities

    @property
    def supports_negative_conclusion(self) -> bool:
        """Whether "nothing is affected" is a statement this estate can support.

        Deliberately strict: without inventory on a clear majority of the assets that
        could run software, "we found nothing" describes the search, not the estate.
        """
        return self.assessable_entities > 0 and self.ratio >= 0.5

    def describe(self) -> str:
        if self.assessable_entities == 0:
            return "no entity in this workspace could run software"
        return (
            f"{self.entities_with_inventory} of {self.assessable_entities} assets that "
            f"could run software declare any software inventory"
        )


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

    #: For security analyses, what WorldGraph could actually conclude. ``None`` for
    #: non-security events. A structured field rather than prose, because a consumer that
    #: renders severity as a colour needs to know that ``LOW`` here means "no conclusion",
    #: not "no risk" — INSUFFICIENT_DATA must never be painted green.
    assessment: VulnerabilityAssessment | None = None
    #: The inventory behind a security conclusion, so a negative can be weighed.
    inventory_coverage: InventoryCoverage | None = None

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
    """The headline numbers for one world state (baseline or simulated).

    Nullable for the same reason :class:`BusinessImpact` is: an estate with no business
    metadata has no customer-experienced availability, and printing 100 % would be a
    fabrication rather than a reassurance.
    """

    model_config = ConfigDict(extra="forbid")

    #: Customer-experienced availability. ``None`` when undeclared.
    availability: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Availability of the infrastructure itself. Always computable from the graph.
    infrastructure_availability: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Serviceable share of capacity in each named region, 0-1.
    regional_capacity: dict[str, float] = Field(default_factory=dict)
    critical_services_impacted: int = 0
    customer_regions_impacted: int = 0
    customers_affected: int | None = None
    revenue_at_risk_per_hour: float | None = None
    material_risk: Severity = Severity.LOW
    unknown_reasons: list[str] = Field(default_factory=list)
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
