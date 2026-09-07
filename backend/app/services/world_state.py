"""WorldState — the single object the API and the AI tool layer read from.

Responsibilities:

* own the in-memory :class:`WorldGraph` and the event store;
* run the adapter registry and expose each feed's honest status;
* hold simulation scenarios;
* run the incident-analysis pipeline and record its timeline;
* persist through the repository.

It deliberately contains no scoring, traversal or correlation logic of its own — those live
in ``analysis/``, ``graph/`` and ``geo/``, and this class calls them. Keeping orchestration
separate from computation is what stops this file from turning into the 10,000-line god
object the baseline audit warned about.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from ..adapters.base import WorldDataAdapter
from ..adapters.fixtures import REPLAY_SCENARIOS, ReplayEventAdapter
from ..adapters.kev import CisaKevAdapter
from ..adapters.usgs import UsgsEarthquakeAdapter
from ..analysis.blast_radius import calculate_blast_radius
from ..analysis.business_impact import business_impact, snapshot_metrics
from ..analysis.correlation import (
    SpatialMatch,
    assess_vulnerability,
    correlate_event,
    find_assets_near_event,
    match_vulnerable_assets,
    proximity_summary,
)
from ..analysis.material_risk import material_risks
from ..analysis.propagation import propagate
from ..analysis.response_plan import generate_response_plan
from ..config import RunMode, Settings
from ..fixtures.atlaspay import build_atlaspay
from ..graph.world_graph import WorldGraph
from ..models.analysis import (
    BlastRadiusResult,
    Confidence,
    InventoryCoverage,
    MaterialRisk,
    ResponsePlan,
    SimulationComparison,
    SimulationScenario,
    TimelineEntry,
    VulnerabilityAssessment,
    WorldSnapshotMetrics,
)
from ..models.core import (
    DependencyEdge,
    FeedStatus,
    Severity,
    WorldEntity,
    WorldEvent,
    utcnow,
)
from ..models.workspace import Workspace, atlaspay_workspace
from ..simulation.engine import compare as compare_scenario
from ..storage.repository import Repository, SqliteRepository

logger = logging.getLogger("worldgraph.state")

#: How many timeline entries to keep in memory. The repository holds the durable record.
TIMELINE_MEMORY_LIMIT = 500


def _availability_move(
    baseline: WorldSnapshotMetrics, simulated: WorldSnapshotMetrics
) -> str:
    """"availability 100.00% → 90.45%", against whichever figure the estate supports.

    Prefers the customer-experienced availability and falls back to the infrastructure
    one, *relabelled* — reporting an infrastructure number under a customer heading is the
    same fabrication in a different sentence.
    """
    if baseline.availability is not None and simulated.availability is not None:
        return (
            f"availability {baseline.availability * 100:.2f}% → "
            f"{simulated.availability * 100:.2f}%"
        )
    return (
        f"infrastructure availability "
        f"{baseline.infrastructure_availability * 100:.2f}% → "
        f"{simulated.infrastructure_availability * 100:.2f}% "
        "(customer-experienced availability UNKNOWN for this workspace)"
    )


def _no_impact_explanations(
    event: WorldEvent,
    assessment: VulnerabilityAssessment | None,
    coverage: InventoryCoverage | None,
) -> list[str]:
    """What to tell the operator when nothing was found."""
    if assessment is VulnerabilityAssessment.INSUFFICIENT_DATA and coverage is not None:
        return [
            f"INSUFFICIENT DATA — WorldGraph cannot assess {event.title} against this "
            "workspace.",
            f"Software matching requires a software inventory, and {coverage.describe()}.",
            "This is not a finding that the estate is unaffected. Import scanner findings "
            "or an SBOM to make this question answerable.",
        ]
    lines = [f"{event.title} does not correlate with any asset in this workspace."]
    if event.location is not None:
        lines.append(
            "No facility lies inside the modelled exposure radius and no asset runs "
            "affected software."
        )
    else:
        lines.append("This event carries no location and matched no software inventory.")
    if coverage is not None:
        lines.append(f"Inventory searched: {coverage.describe()}.")
    return lines


def _no_impact_confidence(
    event: WorldEvent,
    assessment: VulnerabilityAssessment | None,
    coverage: InventoryCoverage | None,
) -> Confidence:
    """Confidence in a no-impact result.

    A negative conclusion is only as strong as the inventory behind it. Where there is no
    inventory there is no conclusion, so there is nothing to be confident *in* — the score
    collapses and the missing input is named rather than hedged around.
    """
    if assessment is VulnerabilityAssessment.INSUFFICIENT_DATA:
        return Confidence(
            score=0.0,
            strong_evidence=[],
            uncertainties=[
                "No conclusion was reached: "
                + (coverage.describe() if coverage else "no software inventory is available"),
                "A negative result requires inventory coverage this workspace does not have.",
            ],
        )
    strong = ["deterministic correlation found no exposed asset"]
    if coverage is not None and coverage.supports_negative_conclusion:
        strong.append(f"inventory searched: {coverage.describe()}")
    return Confidence(
        score=round(event.source.confidence * 0.9, 2),
        strong_evidence=strong,
        uncertainties=[
            "This workspace may not contain every asset the organisation "
            "operates; WorldGraph can only reason about what it was given."
        ],
    )


class WorldState:
    """The live world model."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        *,
        workspace: Workspace | None = None,
        estate_loader: Callable[[], tuple[list[WorldEntity], list[DependencyEdge]]] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or SqliteRepository(settings.database_path)
        # A world belongs to exactly one workspace. Defaulting to the demo keeps every
        # existing caller working; the registry always passes one explicitly.
        self.workspace = workspace or atlaspay_workspace()
        self._estate_loader = estate_loader or build_atlaspay
        self.graph = WorldGraph()
        self._events: dict[str, WorldEvent] = {}
        self._scenarios: dict[str, SimulationScenario] = {}
        self._timeline: list[TimelineEntry] = []
        self._adapters: list[WorldDataAdapter] = []
        self._analyses: dict[str, BlastRadiusResult] = {}
        self._started = False
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------------------

    async def startup(self) -> None:
        """Load the estate, register adapters, and take a first refresh."""
        if self._started:
            return
        self._started = True
        self._load_enterprise_estate()
        self._register_adapters()

        for adapter in self._adapters:
            await adapter.initialize()
            try:
                await adapter.start()
            except Exception as error:
                logger.warning("adapter_start_failed adapter=%s error=%s", adapter.id, error)
            self._ingest_from(adapter)

        self._record_timeline(
            stage="ingest",
            message=(
                f"{self.workspace.name} online in {self.settings.run_mode.value} mode — "
                f"{len(self.graph)} entities, {len(self._events)} events."
            ),
        )

    async def shutdown(self) -> None:
        """Stop every adapter."""
        for adapter in self._adapters:
            await adapter.stop()
        self._started = False

    def _load_enterprise_estate(self) -> None:
        """Load this workspace's estate, preferring anything already persisted.

        The loader is the source of truth on a fresh store; a persisted estate wins
        afterwards so an operator's edits survive a restart. Which loader runs is the
        workspace's business, not this class's — before the Reality Pass this method
        always built AtlasPay regardless of what was asked for
        (docs/REALITY_PASS_AUDIT.md, B14).
        """
        stored_entities = self.repository.load_entities()
        stored_edges = self.repository.load_edges()
        if stored_entities:
            self.graph = WorldGraph(stored_entities, stored_edges)
            logger.info(
                "estate_loaded workspace=%s source=repository entities=%d",
                self.workspace.id,
                len(stored_entities),
            )
            return
        entities, edges = self._estate_loader()
        self.graph = WorldGraph(entities, edges)
        self.repository.save_entities(entities)
        self.repository.save_edges(edges)
        logger.info(
            "estate_loaded workspace=%s source=loader entities=%d",
            self.workspace.id,
            len(entities),
        )

    def _register_adapters(self) -> None:
        """Build the adapter set for the current run mode.

        Replay fixtures are always registered — even in LIVE mode — so the demo scenarios
        remain one click away and a live feed outage never leaves the globe empty.
        """
        self._adapters = [ReplayEventAdapter()]
        if self.settings.run_mode is RunMode.LIVE:
            self._adapters.extend([UsgsEarthquakeAdapter(), CisaKevAdapter()])

    def _ingest_from(self, adapter: WorldDataAdapter) -> None:
        """Take normalized records from one adapter into the world."""
        events = adapter.get_events()
        for event in events:
            self._events[event.id] = event
        for entity in adapter.get_entities():
            self.graph.add_entity(entity)
        if events:
            self.repository.save_events(events)

    async def refresh_feeds(self) -> list[FeedStatus]:
        """Refresh every adapter and re-ingest. Returns the resulting statuses."""
        async with self._lock:
            for adapter in self._adapters:
                await adapter.refresh()
                self._ingest_from(adapter)
        return self.feed_statuses()

    # -- reads -------------------------------------------------------------------------

    def feed_statuses(self) -> list[FeedStatus]:
        return [adapter.get_status() for adapter in self._adapters]

    def entities(self) -> list[WorldEntity]:
        return self.graph.entities

    def entity(self, entity_id: str) -> WorldEntity | None:
        return self.graph.entity(entity_id)

    def events(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
        category: str | None = None,
    ) -> list[WorldEvent]:
        """Recent events, newest first."""
        rows = list(self._events.values())
        if since is not None:
            rows = [e for e in rows if e.occurred_at >= since]
        if category:
            wanted = category.upper()
            rows = [e for e in rows if e.category.value == wanted]
        rows.sort(key=lambda e: e.occurred_at, reverse=True)
        return rows[:limit]

    def event(self, event_id: str) -> WorldEvent | None:
        return self._events.get(event_id)

    def replay_scenarios(self) -> list[dict[str, object]]:
        return list(REPLAY_SCENARIOS)

    def timeline(self, *, limit: int = 100, event_id: str | None = None) -> list[TimelineEntry]:
        rows = self._timeline
        if event_id is not None:
            rows = [entry for entry in rows if entry.event_id == event_id]
        return sorted(rows, key=lambda entry: entry.at, reverse=True)[:limit]

    def baseline_metrics(self) -> WorldSnapshotMetrics:
        """Headline metrics for the untouched world."""
        return snapshot_metrics(self.graph, propagate(self.graph))

    def material_risks(self) -> list[MaterialRisk]:
        """Standing structural risks for executive mode."""
        return material_risks(self.graph, self.events(limit=50))

    def dashboard(self) -> dict[str, object]:
        """The numbers on the top bar and the first-run dashboard."""
        state = propagate(self.graph)
        impact = business_impact(self.graph, state)
        risks = self.material_risks()
        active_incidents = [
            event
            for event in self.events(limit=100)
            if event.severity in {Severity.HIGH, Severity.CRITICAL}
            and self._correlates(event)
        ]
        return {
            "workspace_id": self.workspace.id,
            "workspace_name": self.workspace.name,
            "workspace_kind": self.workspace.kind.value,
            "read_only": self.workspace.read_only,
            "organization": self.workspace.organization or self.workspace.name,
            "critical_services": sum(
                1
                for entity in self.graph.entities
                if entity.criticality.value == "CRITICAL"
                and entity.type.value
                in {"BUSINESS_SERVICE", "APPLICATION", "MICROSERVICE", "DATABASE"}
            ),
            "infrastructure_assets": sum(
                1
                for entity in self.graph.entities
                if entity.type.value
                not in {"ORGANIZATION", "CUSTOMER_REGION", "SECURITY_FINDING", "WORLD_EVENT"}
            ),
            "active_incidents": len(active_incidents),
            "material_risks": len(risks),
            # Two availabilities, deliberately. The customer-experienced figure is None
            # for an estate that declares no customers, and the infrastructure figure is
            # always computable from the graph — so the top bar can show a real number
            # without either inventing a customer view or going blank.
            "availability": impact.availability,
            "infrastructure_availability": impact.infrastructure_availability,
            "unknown_reasons": impact.unknown_reasons,
            "entities": len(self.graph),
            "edges": len(self.graph.edges),
            "events": len(self._events),
            "mode": self.settings.run_mode.value,
            "data_disclaimer": self.workspace.data_disclaimer(),
        }

    def _correlates(self, event: WorldEvent) -> bool:
        """Whether an event touches this estate at all — the filter for "active incidents"."""
        if event.directly_named_entity_ids:
            return any(eid in self.graph for eid in event.directly_named_entity_ids)
        if event.location is not None and event.exposure_radius_km > 0:
            return bool(find_assets_near_event(self.graph, event))
        cve_id = str(event.metadata.get("cve_id") or "")
        product_names = event.metadata.get("product_names")
        if cve_id or isinstance(product_names, list):
            return bool(
                match_vulnerable_assets(
                    self.graph,
                    cve_id=cve_id,
                    product_names=[str(p) for p in (product_names or [])],
                )
            )
        return False

    # -- incident analysis pipeline ----------------------------------------------------

    def analyze_event(self, event_id: str) -> BlastRadiusResult:
        """The deterministic incident-analysis pipeline.

        ``normalize → correlate → traverse → impact → rank → store → (AI explains)``

        Every stage writes a timeline entry, so a developer can inspect exactly why an
        analysis reached its severity rather than being told a number.
        """
        event = self._events.get(event_id)
        if event is None:
            raise KeyError(f"unknown event '{event_id}'")

        self._record_timeline(
            stage="normalize",
            message=f"{event.title} normalized from {event.source.source_name}.",
            event_id=event.id,
            severity=event.severity,
        )

        if event.category.value == "SECURITY_VULNERABILITY":
            return self._analyze_vulnerability(event)

        pinned, matches, proximity = correlate_event(self.graph, event)
        self._record_correlation(event, matches, pinned)

        if not pinned:
            # An honest empty result beats a fabricated one. The engine needs an origin, so
            # rather than inventing one we return a zero-impact analysis that says why.
            return self._empty_analysis(event)

        result = calculate_blast_radius(
            self.graph,
            origin_ids=sorted(pinned.keys()),
            origin_kind="EVENT",
            origin_label=event.title,
            initial_availability=pinned,
            event=event,
            proximity=proximity,
            mode=event.source.mode,
        )
        self._store_analysis(result, event)
        return result

    def _analyze_vulnerability(self, event: WorldEvent) -> BlastRadiusResult:
        """Security path: correlate by software inventory, not geography."""
        cve_id = str(event.metadata.get("cve_id") or "")
        raw_products = event.metadata.get("product_names")
        products = [str(p) for p in raw_products] if isinstance(raw_products, list) else []
        matches = match_vulnerable_assets(self.graph, cve_id=cve_id, product_names=products)
        assessment, coverage = assess_vulnerability(self.graph, matches)

        if not matches:
            # Two very different facts used to produce the same output here. "We searched
            # and found nothing" is a finding; "we have nothing to search" is not, and
            # reporting the second as the first is a confident all-clear derived from
            # having looked at nothing.
            if assessment is VulnerabilityAssessment.INSUFFICIENT_DATA:
                self._record_timeline(
                    stage="correlate",
                    message=(
                        f"{cve_id or event.title}: INSUFFICIENT DATA — {coverage.describe()}. "
                        "WorldGraph cannot say whether this estate is affected."
                    ),
                    event_id=event.id,
                    severity=event.severity,
                )
            else:
                self._record_timeline(
                    stage="correlate",
                    message=(
                        f"{cve_id or event.title}: no asset in this workspace runs the "
                        f"affected software ({coverage.describe()})."
                    ),
                    event_id=event.id,
                )
            return self._empty_analysis(event, assessment=assessment, coverage=coverage)

        internet_facing = [m for m in matches if m.internet_facing]
        self._record_timeline(
            stage="correlate",
            message=(
                f"{cve_id or 'Vulnerability'} matches {len(matches)} assets "
                f"({len(internet_facing)} internet-facing)."
            ),
            event_id=event.id,
            entity_ids=[m.entity.id for m in matches],
            severity=event.severity,
        )

        # The origin is the *reachable* attack surface, not every vulnerable asset. An
        # internal service running vulnerable code that nothing can reach is a patching
        # task, not a blast radius — conflating the two is what makes CVE dashboards noise.
        origins = [m.entity.id for m in internet_facing] or [matches[0].entity.id]
        result = calculate_blast_radius(
            self.graph,
            origin_ids=origins,
            origin_kind="SECURITY_FINDING",
            origin_label=f"{cve_id or event.title} — reachable exposure",
            event=event,
            proximity=1.0 if internet_facing else 0.0,
            mode=event.source.mode,
        )
        # Carried onto the result so a consumer can tell a confirmed finding from a
        # product-name collision. Matching "nginx" ignores version and vendor, so it
        # nominates candidates for triage; presenting that as a confirmed exposure is how
        # a vulnerability queue becomes noise nobody works.
        result.assessment = assessment
        result.inventory_coverage = coverage
        if assessment is VulnerabilityAssessment.POTENTIALLY_AFFECTED:
            result.explanations.append(
                "POTENTIALLY AFFECTED — matched on product name only. No asset's inventory "
                f"names {cve_id or 'this CVE'}, and WorldGraph does not compare versions or "
                "vendors. Confirm against a scanner finding before treating this as exposure."
            )
            result.confidence.uncertainties.append(
                "match is by product name, not by a confirmed CVE association"
            )
        self._store_analysis(result, event)
        return result

    def _empty_analysis(
        self,
        event: WorldEvent,
        *,
        assessment: VulnerabilityAssessment | None = None,
        coverage: InventoryCoverage | None = None,
    ) -> BlastRadiusResult:
        """A no-impact analysis that states why nothing was found.

        ``assessment`` distinguishes "searched and clean" from "could not search". The
        second must not be presented as reassurance.
        """
        from ..models.analysis import (
            RiskScore,
        )

        state = propagate(self.graph)
        result = BlastRadiusResult(
            id=f"blast-{uuid.uuid4().hex[:12]}",
            origin_kind="EVENT",
            origin_ids=[],
            origin_label=event.title,
            severity=Severity.LOW,
            risk=RiskScore(score=0.0, severity=Severity.LOW, contributions=[]),
            business_impact=business_impact(self.graph, state),
            explanations=_no_impact_explanations(event, assessment, coverage),
            confidence=_no_impact_confidence(event, assessment, coverage),
            assessment=assessment,
            inventory_coverage=coverage,
            mode=event.source.mode,
        )
        self._store_analysis(result, event)
        return result

    def _record_correlation(
        self, event: WorldEvent, matches: list[SpatialMatch], pinned: dict[str, float]
    ) -> None:
        summary = proximity_summary(self.graph, event)
        if matches:
            self._record_timeline(
                stage="correlate",
                message=(
                    f"{len(matches)} assets inside the "
                    f"{event.exposure_radius_km:.0f} km exposure radius "
                    f"({summary['critical_facilities']} facilities, "
                    f"{summary['suppliers']} suppliers)."
                ),
                event_id=event.id,
                entity_ids=[m.entity.id for m in matches],
                severity=event.severity,
                metadata={"closest_km": round(matches[0].distance_km, 1), **summary},
            )
        elif pinned:
            self._record_timeline(
                stage="correlate",
                message=f"{len(pinned)} assets named directly by the source.",
                event_id=event.id,
                entity_ids=sorted(pinned.keys()),
                severity=event.severity,
            )
        else:
            self._record_timeline(
                stage="correlate",
                message="No asset in this workspace correlates with this event.",
                event_id=event.id,
            )

    def _store_analysis(self, result: BlastRadiusResult, event: WorldEvent | None) -> None:
        self._analyses[result.id] = result
        self.repository.save_analysis(result, event_id=event.id if event else None)
        self._record_timeline(
            stage="analyze",
            message=(
                f"Dependency analysis complete — {len(result.direct_impact)} direct, "
                f"{len(result.indirect_impact)} indirect, "
                f"{result.duration_ms:.0f} ms."
            ),
            event_id=event.id if event else None,
            entity_ids=[r.entity_id for r in result.direct_impact],
        )
        self._record_timeline(
            stage="risk",
            message=f"Risk scored {result.risk.score:.0f}/100 → {result.severity.value}.",
            event_id=event.id if event else None,
            severity=result.severity,
            metadata={"contributions": [c.model_dump() for c in result.risk.contributions]},
        )

    def analysis(self, analysis_id: str) -> BlastRadiusResult | None:
        return self._analyses.get(analysis_id) or self.repository.load_analysis(analysis_id)

    def recent_analyses(self, *, limit: int = 20) -> list[BlastRadiusResult]:
        rows = sorted(self._analyses.values(), key=lambda r: r.computed_at, reverse=True)
        return rows[:limit]

    # -- simulation --------------------------------------------------------------------

    def scenarios(self) -> list[SimulationScenario]:
        return sorted(self._scenarios.values(), key=lambda s: s.updated_at, reverse=True)

    def scenario(self, scenario_id: str) -> SimulationScenario | None:
        return self._scenarios.get(scenario_id) or self.repository.load_scenario(scenario_id)

    def put_scenario(self, scenario: SimulationScenario) -> SimulationScenario:
        self._scenarios[scenario.id] = scenario
        self.repository.save_scenario(scenario)
        return scenario

    def drop_scenario(self, scenario_id: str) -> None:
        self._scenarios.pop(scenario_id, None)
        self.repository.delete_scenario(scenario_id)

    def compare_scenario(self, scenario: SimulationScenario) -> SimulationComparison:
        comparison = compare_scenario(self.graph, scenario)
        self._record_timeline(
            stage="simulate",
            # Quote whichever availability this estate can actually support. The
            # customer-experienced figure is None wherever no customer regions are
            # declared, and multiplying that by 100 crashed the compare endpoint on an
            # imported estate (docs/REALITY_PASS_REPORT.md §7).
            message=(
                f"Simulation '{scenario.name}' — "
                f"{_availability_move(comparison.baseline, comparison.simulated)}, "
                f"risk {comparison.simulated.material_risk.value}."
            ),
            event_id=scenario.origin_event_id,
            severity=comparison.simulated.material_risk,
            metadata={"scenario_id": scenario.id, "duration_ms": comparison.duration_ms},
        )
        if comparison.blast_radius is not None:
            self._analyses[comparison.blast_radius.id] = comparison.blast_radius
        return comparison

    # -- plans -------------------------------------------------------------------------

    def build_plan(
        self, result: BlastRadiusResult, *, scenario_name: str | None = None
    ) -> ResponsePlan:
        plan = generate_response_plan(self.graph, result, scenario_name=scenario_name)
        self.repository.save_plan(plan, analysis_id=result.id)
        self._record_timeline(
            stage="plan",
            message=f"Response plan generated — {len(plan.actions)} recommended actions, none executed.",
            severity=result.severity,
            metadata={"plan_id": plan.id, "analysis_id": result.id},
        )
        return plan

    # -- timeline ----------------------------------------------------------------------

    def _record_timeline(
        self,
        *,
        stage: str,
        message: str,
        event_id: str | None = None,
        entity_ids: list[str] | None = None,
        severity: Severity = Severity.INFO,
        metadata: dict[str, object] | None = None,
    ) -> TimelineEntry:
        entry = TimelineEntry(
            id=f"tl-{uuid.uuid4().hex[:12]}",
            at=utcnow(),
            stage=stage,
            message=message[:512],
            severity=severity,
            entity_ids=entity_ids or [],
            event_id=event_id,
            metadata=metadata or {},
        )
        self._timeline.append(entry)
        if len(self._timeline) > TIMELINE_MEMORY_LIMIT:
            self._timeline = self._timeline[-TIMELINE_MEMORY_LIMIT:]
        self.repository.append_timeline([entry])
        return entry

    def record_ai_timeline(self, message: str, *, event_id: str | None = None) -> None:
        """Public hook so the AI layer can log tool use without reaching into internals."""
        self._record_timeline(stage="ai", message=message, event_id=event_id)

    def changes_since(self, window: timedelta) -> dict[str, object]:
        """What changed recently — backs "what changed in the last hour?"."""
        cutoff = utcnow() - window
        events = [e for e in self._events.values() if e.source.ingested_at >= cutoff]
        entries = [entry for entry in self._timeline if entry.at >= cutoff]
        return {
            "window_minutes": int(window.total_seconds() // 60),
            "new_events": sorted(events, key=lambda e: e.occurred_at, reverse=True)[:20],
            "timeline": sorted(entries, key=lambda e: e.at, reverse=True)[:30],
            "feed_statuses": self.feed_statuses(),
        }
