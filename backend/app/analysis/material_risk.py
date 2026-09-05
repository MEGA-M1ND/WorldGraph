"""Standing structural risks for executive mode.

A material risk is not an incident. It exists whether or not anything is currently on fire,
and it is derived from the *shape* of the estate: concentration, single sourcing, single
points of failure, reachable vulnerable surface. These are the three lines an executive
sees, and clicking one flies the camera to the infrastructure that causes it.

Everything here is computed from the graph. Nothing is hand-authored, so a change to the
estate changes the risk list without anyone editing a constant.
"""

from __future__ import annotations

from ..graph.world_graph import WorldGraph
from ..models.analysis import MaterialRisk, ScoreContribution
from ..models.core import (
    Criticality,
    EntityType,
    Severity,
    WorldEvent,
    severity_from_score,
)
from .propagation import propagate

#: Traffic share above which a single region counts as a concentration risk.
_CONCENTRATION_THRESHOLD = 0.25

#: How many risks to surface. Executive mode is a summary, not a register.
MAX_RISKS = 6


def material_risks(graph: WorldGraph, events: list[WorldEvent] | None = None) -> list[MaterialRisk]:
    """Compute the standing risk register, most severe first."""
    risks: list[MaterialRisk] = []
    risks.extend(_concentration_risks(graph))
    risks.extend(_single_source_risks(graph))
    risks.extend(_spof_risks(graph))
    risks.extend(_vulnerability_risks(graph, events or []))
    risks.sort(key=lambda risk: (-risk.score, risk.id))
    return risks[:MAX_RISKS]


def _concentration_risks(graph: WorldGraph) -> list[MaterialRisk]:
    """Regions carrying a disproportionate share of critical traffic."""
    out: list[MaterialRisk] = []
    for region in graph.entities_of_type(EntityType.CLOUD_REGION):
        hosted = graph.hosted_entities(region.id)
        critical = [e for e in hosted if e.criticality is Criticality.CRITICAL]
        traffic = sum(e.business.traffic_share for e in hosted)
        if traffic < _CONCENTRATION_THRESHOLD or not critical:
            continue

        # Score the concentration: how much traffic sits here, how much of it is
        # CRITICAL, and how far the loss would travel.
        reach = graph.traverse_dependents([region.id], max_depth=6)
        downstream = len(reach.steps) - 1
        contributions = [
            ScoreContribution(
                code="traffic_concentration",
                label=f"{traffic * 100:.0f}% of modelled traffic in one region",
                points=round(min(45.0, traffic * 110.0), 1),
                detail=", ".join(sorted(e.name for e in hosted)[:4]),
            ),
            ScoreContribution(
                code="critical_workloads",
                label=f"{len(critical)} critical workloads co-located",
                points=round(min(25.0, len(critical) * 9.0), 1),
                detail=", ".join(sorted(e.name for e in critical)[:3]),
            ),
            ScoreContribution(
                code="downstream_reach",
                label=f"loss reaches {downstream} downstream entities",
                points=round(min(20.0, downstream * 1.6), 1),
            ),
        ]
        score = min(100.0, sum(c.points for c in contributions))
        out.append(
            MaterialRisk(
                id=f"risk-concentration-{region.id}",
                title=f"{_short_region(region.name)} payments concentration",
                severity=severity_from_score(score),
                summary=(
                    f"{traffic * 100:.0f}% of modelled traffic and {len(critical)} critical "
                    f"workloads sit in {region.name}. Losing the region reaches "
                    f"{downstream} downstream entities."
                ),
                focus_entity_ids=[region.id, *sorted(e.id for e in critical)[:3]],
                contributions=contributions,
                score=round(score, 1),
            )
        )
    return out


def _single_source_risks(graph: WorldGraph) -> list[MaterialRisk]:
    """Suppliers with no qualified second source."""
    out: list[MaterialRisk] = []
    for supplier in graph.entities_of_type(EntityType.SUPPLIER):
        if supplier.metadata.get("second_source_qualified") is not False:
            continue
        # What would actually be constrained: solve the world with this supplier gone.
        state = propagate(graph, initial_availability={supplier.id: 0.0})
        constrained = [
            eid
            for eid in state.capacity_constrained_ids()
            if eid != supplier.id and state.capacity[eid] < 0.8
        ]
        lead_time = supplier.metadata.get("lead_time_weeks")
        contributions = [
            ScoreContribution(
                code="sole_source",
                label="sole-source supplier, no qualified alternative",
                points=35.0,
                detail=supplier.name,
            ),
            ScoreContribution(
                code="capacity_exposure",
                label=f"{len(constrained)} assets fall below 80% capacity without it",
                points=round(min(30.0, len(constrained) * 10.0), 1),
                detail=", ".join(sorted(constrained)[:3]),
            ),
        ]
        if isinstance(lead_time, (int, float)):
            contributions.append(
                ScoreContribution(
                    code="lead_time",
                    label=f"{lead_time:.0f}-week replacement lead time",
                    points=round(min(25.0, float(lead_time) * 1.4), 1),
                )
            )
        score = min(100.0, sum(c.points for c in contributions))
        out.append(
            MaterialRisk(
                id=f"risk-sole-source-{supplier.id}",
                title=f"{_short_region(supplier.name)} continuity",
                severity=severity_from_score(score),
                summary=(
                    f"{supplier.name} has no qualified second source. Losing it constrains "
                    f"{len(constrained)} assets' replacement capacity"
                    + (f" against a {lead_time:.0f}-week lead time." if isinstance(lead_time, (int, float)) else ".")
                ),
                focus_entity_ids=[supplier.id, *sorted(constrained)[:3]],
                contributions=contributions,
                score=round(score, 1),
            )
        )
    return out


def _spof_risks(graph: WorldGraph) -> list[MaterialRisk]:
    """Unredundant entities whose loss reaches many others."""
    out: list[MaterialRisk] = []
    for entity_id, downstream in graph.single_points_of_failure(min_dependents=5)[:2]:
        entity = graph.entity(entity_id)
        if entity is None or entity.type in {EntityType.ORGANIZATION, EntityType.NETWORK_NODE}:
            continue
        if entity.criticality not in {Criticality.CRITICAL, Criticality.HIGH}:
            continue
        contributions = [
            ScoreContribution(
                code="no_redundancy",
                label="no independent replica or site",
                points=28.0,
                detail=entity.name,
            ),
            ScoreContribution(
                code="downstream_reach",
                label=f"loss reaches {downstream} downstream entities",
                points=round(min(32.0, downstream * 2.2), 1),
            ),
            ScoreContribution(
                code="criticality",
                label=f"{entity.criticality.value.lower()} asset",
                points=20.0 if entity.criticality is Criticality.CRITICAL else 12.0,
            ),
        ]
        score = min(100.0, sum(c.points for c in contributions))
        out.append(
            MaterialRisk(
                id=f"risk-spof-{entity.id}",
                title=f"{entity.name} single point of failure",
                severity=severity_from_score(score),
                summary=(
                    f"{entity.name} has no independent replica and its loss propagates to "
                    f"{downstream} entities."
                ),
                focus_entity_ids=[entity.id],
                contributions=contributions,
                score=round(score, 1),
            )
        )
    return out


def _vulnerability_risks(graph: WorldGraph, events: list[WorldEvent]) -> list[MaterialRisk]:
    """Vulnerable software that is actually reachable from the internet."""
    from .correlation import match_vulnerable_assets

    out: list[MaterialRisk] = []
    seen: set[str] = set()
    for event in events:
        if event.category.value != "SECURITY_VULNERABILITY":
            continue
        cve_id = str(event.metadata.get("cve_id") or "")
        if not cve_id or cve_id in seen:
            continue
        raw = event.metadata.get("product_names")
        products = [str(p) for p in raw] if isinstance(raw, list) else []
        matches = match_vulnerable_assets(graph, cve_id=cve_id, product_names=products)
        if not matches:
            continue
        seen.add(cve_id)
        exposed = [m for m in matches if m.internet_facing]
        contributions = [
            ScoreContribution(
                code="vulnerable_assets",
                label=f"{len(matches)} assets run affected software",
                points=round(min(25.0, len(matches) * 6.0), 1),
                detail=", ".join(sorted(m.entity.name for m in matches)[:4]),
            ),
        ]
        if exposed:
            contributions.append(
                ScoreContribution(
                    code="internet_reachable",
                    label=f"{len(exposed)} of them are internet-facing",
                    points=30.0,
                    detail=", ".join(sorted(m.entity.name for m in exposed)),
                )
            )
        else:
            contributions.append(
                ScoreContribution(
                    code="not_internet_reachable",
                    label="none are internet-facing",
                    points=-10.0,
                    detail="patching task rather than exposure",
                )
            )
        if event.metadata.get("known_ransomware_use") is True:
            contributions.append(
                ScoreContribution(
                    code="known_ransomware",
                    label="known ransomware campaign use",
                    points=20.0,
                )
            )
        score = max(0.0, min(100.0, sum(c.points for c in contributions)))
        out.append(
            MaterialRisk(
                id=f"risk-vuln-{cve_id.lower()}",
                title=f"{cve_id} exposure",
                severity=severity_from_score(score),
                summary=(
                    f"{cve_id} affects {len(matches)} AtlasPay assets; "
                    + (
                        f"{len(exposed)} reachable from the internet."
                        if exposed
                        else "none are reachable from the internet."
                    )
                ),
                focus_entity_ids=[m.entity.id for m in (exposed or matches)][:4],
                contributions=contributions,
                score=round(score, 1),
            )
        )
    return out


def _short_region(name: str) -> str:
    """Trim a verbose entity name down to something an executive line can carry."""
    for token in ("(", " —", " -"):
        if token in name:
            name = name.split(token)[0]
    return name.strip()


#: Re-exported so callers do not need to import Severity separately.
__all__ = ["MaterialRisk", "Severity", "material_risks"]
