"""The deterministic intent router.

This is the analyst WorldGraph ships with. It maps a natural-language command onto tool
calls using explicit patterns, runs those tools, and composes an answer from their
structured results.

Two reasons it exists rather than being a fallback nobody tests:

1. **The product must work without a model.** ``docs/BASELINE_AUDIT.md`` ADR-3. A demo that
   needs an API key and a network round-trip is a demo that fails in the room where it
   matters. Every hero-flow phrase in the specification is routed here.
2. **It is the honest floor.** When a model *is* configured it uses the same tools and the
   same deterministic results; the model adds language, not facts. If the router can
   answer a question, the answer is identical either way.

The router does not pretend to understand arbitrary English. It recognizes the vocabulary
of this product, and when it does not recognize something it says so and offers what it can
do — which is far better than confidently answering the wrong question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models.core import HealthState
from .tools import ToolContext, ToolError, run_tool


@dataclass(slots=True)
class RouterResult:
    """What the router produced."""

    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    directives: list[dict[str, Any]] = field(default_factory=list)
    #: True when the router matched an intent. False means it declined rather than guessed.
    matched: bool = True


#: Phrases that mean "run the whole hero analysis on this event".
_INVESTIGATE = re.compile(
    r"\b(investigate|analy[sz]e|look into|what happened|tell me about)\b", re.IGNORECASE
)
_BLAST = re.compile(r"\b(blast radius|impact\w*|exposed|exposure|affected|who is hurt)\b", re.IGNORECASE)
_SIMULATE = re.compile(
    r"\b(simulate|what if|what happens if|scenario|goes offline|goes down|fails?|losing|lose)\b",
    re.IGNORECASE,
)
_COMPARE = re.compile(r"\b(compare|versus|vs\.?|against (normal|baseline)|difference)\b", re.IGNORECASE)
# Patterns that end in a word STEM use `\w*` rather than `\b`: `\bmitigat\b` never
# matches "mitigation", because the boundary requires a non-word character after "t".
_PLAN = re.compile(
    r"\b(what should we do|response plan|recommend\w*|mitigat\w*|remediat\w*|next steps?|what now)\b",
    re.IGNORECASE,
)
# Direction matters and English is genuinely ambiguous here. "What depends on X" asks for
# X's DEPENDENTS; "what does X depend on" asks for its DEPENDENCIES. A single "depends on"
# keyword answers the wrong question half the time, so the two are matched separately and
# the dependents form (an interrogative subject before the verb) is tested first.
_DEPENDENTS = re.compile(
    r"\b((what|who|which\s+[\w-]+)\s+(depends?|relies|rely)\s+(on|upon)"
    r"|dependents?\b|downstream|who uses|what uses|impacted by|affected by)",
    re.IGNORECASE,
)
_DEPENDS = re.compile(
    r"\b(does\s+[\w.:-]+\s+depend|dependenc\w*|upstream|relies? (on|upon)|depends? (on|upon))\b",
    re.IGNORECASE,
)
_CRITICAL = re.compile(r"\b(critical infrastructure|critical (services|assets)|show me our)\b", re.IGNORECASE)
_THREATS = re.compile(
    r"\b(what can hurt us|what.s (our )?risk|material risks?|biggest risks?|worried|threats?)\b",
    re.IGNORECASE,
)
_CHANGES = re.compile(r"\b(what changed|recent(ly)?|last (hour|day)|new events?|anything new)\b", re.IGNORECASE)
_VULN = re.compile(r"\b(vulnerabilit\w*|cve|patch\w*|exploit\w*)\b", re.IGNORECASE)
_REACH = re.compile(r"\b(reach\w*|attack paths?|internet.facing|compromis\w*)\b", re.IGNORECASE)
_CUSTOMERS = re.compile(r"\b(customers?|which customers|customer impact)\b", re.IGNORECASE)
_WHY = re.compile(r"\b(why|explain|justify|reason)\b", re.IGNORECASE)

#: Region keywords mapped to the entity most operators mean by them.
_REGION_HINTS: dict[str, str] = {
    "singapore": "cloud-region-singapore",
    "mumbai": "cloud-region-mumbai",
    "frankfurt": "cloud-region-frankfurt",
    "virginia": "cloud-region-virginia",
    "tokyo": "cloud-region-tokyo",
    "taiwan": "supplier-taiwan-hardware",
}


class IntentRouter:
    """Maps operator language onto deterministic tool calls."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.calls: list[dict[str, Any]] = []

    # -- entry point --------------------------------------------------------------------

    def handle(self, message: str) -> RouterResult:
        """Route one operator message."""
        text = (message or "").strip()
        if not text:
            return RouterResult(answer="Ask me about AtlasPay's infrastructure, an event, or a what-if scenario.", matched=False)

        try:
            for matcher in (
                self._route_threats,
                self._route_changes,
                self._route_plan,
                self._route_compare,
                self._route_simulate,
                self._route_vulnerability,
                self._route_reachability,
                self._route_customers,
                self._route_why,
                self._route_investigate,
                self._route_blast,
                self._route_dependencies,
                self._route_critical,
                self._route_entity_lookup,
            ):
                result = matcher(text)
                if result is not None:
                    result.tool_calls = self.calls
                    result.directives = list(self.ctx.directives)
                    return result
        except ToolError as error:
            return RouterResult(
                answer=str(error), tool_calls=self.calls, directives=list(self.ctx.directives)
            )

        return RouterResult(
            answer=self._help_text(),
            tool_calls=self.calls,
            directives=list(self.ctx.directives),
            matched=False,
        )

    # -- tool plumbing ------------------------------------------------------------------

    def _call(self, tool_name: str, /, **args: Any) -> dict[str, Any]:
        """Run one tool and record the call.

        ``tool_name`` is positional-only: several tools take an argument called ``name``,
        and without the marker ``_call("create_simulation", name=...)`` collides with it.
        """
        result = run_tool(self.ctx, tool_name, args)
        self.calls.append({"tool": tool_name, "args": args, "ok": True})
        return result

    # -- routes -------------------------------------------------------------------------

    def _route_threats(self, text: str) -> RouterResult | None:
        if not _THREATS.search(text):
            return None
        status = self._call("get_world_status")
        risks = status["material_risks"]
        dashboard = status["dashboard"]
        lines = [
            f"{len(risks)} standing material risks against AtlasPay "
            f"(modelled availability {dashboard['availability'] * 100:.2f}%, "
            f"{dashboard['active_incidents']} active incidents).",
            "",
        ]
        for risk in risks:
            lines.append(f"{risk['severity']} — {risk['title']}")
            lines.append(f"  {risk['summary']}")
        lines.append("")
        lines.append(self._feed_note(status["feeds"]))
        if risks:
            self.ctx.emit("annotate", entity_ids=risks[0]["focus_entity_ids"], label=risks[0]["title"])
        return RouterResult(answer="\n".join(lines))

    def _route_changes(self, text: str) -> RouterResult | None:
        if not _CHANGES.search(text) or _SIMULATE.search(text):
            return None
        minutes = 60
        match = re.search(r"(\d+)\s*(minute|min|hour|hr|day)", text, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            unit = match.group(2).lower()
            minutes = value * (60 if unit.startswith("h") else 1440 if unit.startswith("d") else 1)
        changes = self._call("get_recent_changes", minutes=minutes)
        events = changes["new_events"]
        lines = [f"In the last {changes['window_minutes']} minutes:"]
        if events:
            for event in events[:6]:
                proximity = event.get("enterprise_proximity") or {}
                near = proximity.get("assets_in_radius", 0)
                lines.append(
                    f"  [{event['mode']}] {event['severity']} — {event['title']}"
                    + (f" ({near} AtlasPay assets in radius)" if near else "")
                )
        else:
            lines.append("  No new world events were ingested.")
        timeline = changes["timeline"]
        if timeline:
            lines.append("")
            lines.append("System activity:")
            for entry in timeline[:6]:
                lines.append(f"  {entry['at'][11:19]} {entry['stage']}: {entry['message']}")
        lines.append("")
        lines.append(self._feed_note(changes["feeds"]))
        return RouterResult(answer="\n".join(lines))

    def _route_plan(self, text: str) -> RouterResult | None:
        if not _PLAN.search(text):
            return None
        args: dict[str, Any] = {}
        if self.ctx.active_scenario_id:
            args["scenario_id"] = self.ctx.active_scenario_id
        elif self.ctx.selected_event_id:
            args["event_id"] = self.ctx.selected_event_id
        elif not self.ctx.state.recent_analyses(limit=1):
            # Asked cold, with nothing selected and nothing analysed. Rather than refusing,
            # plan against the most severe incident that actually correlates with AtlasPay
            # — which is what an operator opening the product means by the question.
            candidate = self._most_severe_incident()
            if candidate is not None:
                args["event_id"] = candidate
        plan = self._call("generate_response_plan", **args)
        lines = ["RESPONSE PLAN", "", plan["summary"], ""]
        for index, action in enumerate(plan["actions"], start=1):
            lines.append(f"{index}. [{action['urgency']}] {action['action']}")
            lines.append(f"   Why: {action['rationale']}")
            lines.append(
                f"   Confidence {action['confidence'] * 100:.0f}%"
                + ("  ·  requires approval" if action["requires_approval"] else "")
            )
            lines.append("")
        if plan["assumptions"]:
            lines.append("Assumptions:")
            lines.extend(f"  - {item}" for item in plan["assumptions"])
            lines.append("")
        if plan["unresolved_questions"]:
            lines.append("Open questions:")
            lines.extend(f"  - {item}" for item in plan["unresolved_questions"][:5])
            lines.append("")
        lines.append(plan["note"])
        return RouterResult(answer="\n".join(lines))

    def _most_severe_incident(self) -> str | None:
        """The highest-severity event that correlates with AtlasPay, if any."""
        order = ["CRITICAL", "HIGH", "MODERATE", "LOW", "INFO"]
        candidates = [
            event
            for event in self.ctx.state.events(limit=50)
            if self.ctx.state._correlates(event)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda e: (order.index(e.severity.value), -e.occurred_at.timestamp()))
        return candidates[0].id

    def _route_compare(self, text: str) -> RouterResult | None:
        if not _COMPARE.search(text):
            return None
        scenario_id = self.ctx.active_scenario_id
        if not scenario_id:
            return RouterResult(
                answer=(
                    "There is no active simulation to compare. Ask me to simulate a failure "
                    "first — for example: \"What happens if Singapore goes offline?\""
                )
            )
        comparison = self._call("compare_simulation", scenario_id=scenario_id)
        return RouterResult(answer=self._format_comparison(comparison))

    def _route_simulate(self, text: str) -> RouterResult | None:
        if not _SIMULATE.search(text):
            return None
        targets = self._resolve_targets(text)
        if not targets:
            return RouterResult(
                answer=(
                    "I could not tell which entity to fail. Name a region, cluster, supplier "
                    "or service — for example \"simulate losing the Singapore region\"."
                )
            )

        health = HealthState.DEGRADED if re.search(r"\bdegrad", text, re.IGNORECASE) else HealthState.DOWN

        scenario_id = self.ctx.active_scenario_id
        adding = bool(re.search(r"\b(too|also|as well|add|and)\b", text, re.IGNORECASE))
        if not scenario_id or not adding:
            names = ", ".join(self._name(t) for t in targets)
            created = self._call("create_simulation", name=f"What-if: {names} {health.value}")
            scenario_id = created["scenario_id"]
            self.ctx.active_scenario_id = scenario_id

        for target in targets:
            self._call(
                "add_simulation_override",
                scenario_id=scenario_id,
                target_id=target,
                health=health.value,
            )

        comparison = self._call("compare_simulation", scenario_id=scenario_id)
        header = (
            "SIMULATION — hypothetical world state. Nothing real has changed.\n"
            f"Scenario: {comparison['scenario_name']}\n"
            f"Failures: {', '.join(comparison['overrides'])}\n"
        )
        return RouterResult(answer=header + "\n" + self._format_comparison(comparison))

    def _route_vulnerability(self, text: str) -> RouterResult | None:
        # "Show the blast radius if admin-api is compromised" names a vulnerability *and*
        # asks for a blast radius. The explicit ask wins.
        if not _VULN.search(text) or re.search(r"blast radius", text, re.IGNORECASE):
            return None
        cve_match = re.search(r"CVE-[0-9]{4}-[A-Z0-9-]+", text, re.IGNORECASE)
        cve_id = cve_match.group(0).upper() if cve_match else ""
        if not cve_id:
            events = self._call("list_recent_events", category="SECURITY_VULNERABILITY", limit=5)
            if not events["events"]:
                return RouterResult(answer="No vulnerability events are currently ingested.")
            cve_id = str(events["events"][0]["id"]).split(":")[-1].upper()
        exposure = self._call("get_vulnerability_exposure", cve_id=cve_id)
        if exposure["count"] == 0:
            return RouterResult(answer=f"{cve_id}: {exposure['note']}")
        lines = [
            f"{cve_id} — {exposure['count']} AtlasPay assets run the affected software, "
            f"{exposure['internet_facing_count']} of them internet-facing.",
            "",
        ]
        for asset in exposure["assets"]:
            marker = "INTERNET-FACING" if asset["internet_facing"] else asset["network_zone"]
            lines.append(f"  {asset['name']} — {asset['component']} [{marker}]")
        lines.append("")
        lines.append(
            "Ask \"which vulnerable systems can reach payments?\" to trace the attack path, "
            "or \"show the blast radius if admin-api is compromised\"."
        )
        self.ctx.emit("annotate", entity_ids=[a["id"] for a in exposure["assets"]], label=cve_id)
        return RouterResult(answer="\n".join(lines))

    def _route_reachability(self, text: str) -> RouterResult | None:
        if not _REACH.search(text) or re.search(r"blast radius", text, re.IGNORECASE):
            return None
        targets = self._resolve_targets(text)
        target_id = targets[0] if targets else "payments-api"
        paths = self._call("get_attack_paths", to_entity_id=target_id)
        if paths["count"] == 0:
            return RouterResult(
                answer=(
                    f"No declared network path reaches {self._name(target_id)} from the public "
                    "internet in this model."
                )
            )
        lines = [
            f"{paths['count']} reachability paths from the public internet to "
            f"{self._name(target_id)}:",
            "",
        ]
        for path in paths["paths"][:5]:
            lines.append("  " + " → ".join(path["names"]))
        lines.append("")
        lines.append(paths["note"])
        if paths["paths"]:
            self.ctx.emit("show_path", path=paths["paths"][0]["ids"])
        return RouterResult(answer="\n".join(lines))

    def _route_customers(self, text: str) -> RouterResult | None:
        if not _CUSTOMERS.search(text):
            return None
        result = self._latest_analysis(text)
        if result is None:
            return RouterResult(
                answer=(
                    "Nothing has been analysed yet. Select an event and ask me to investigate "
                    "it, or simulate a failure."
                )
            )
        exposure = result.get("customer_exposure") or []
        if not exposure:
            return RouterResult(
                answer=f"{result['origin']}: no customer region falls below nominal availability."
            )
        lines = [f"Customer exposure for {result['origin']} — MODELLED ESTIMATE:", ""]
        for row in exposure:
            lines.append(
                f"  {row['region']} — {row['traffic_impact'] * 100:.0f}% traffic impact, "
                f"{row['customers']:,} modelled customers"
            )
        return RouterResult(answer="\n".join(lines))

    def _route_why(self, text: str) -> RouterResult | None:
        """Explain an existing conclusion rather than computing a new one.

        "Why is payments high risk?" is a request for the derivation the engines already
        produced. Re-running a fresh analysis would answer a subtly different question and
        could return a different number than the one on screen.
        """
        if not _WHY.search(text):
            return None
        targets = self._resolve_targets(text)
        if not targets and self.ctx.selected_entity_id:
            targets = [self.ctx.selected_entity_id]

        recent = self.ctx.state.recent_analyses(limit=8)
        chosen = None
        if targets:
            for analysis in recent:
                touched = {r.entity_id for r in [*analysis.direct_impact, *analysis.indirect_impact]}
                if touched & set(targets):
                    chosen = analysis
                    break
        if chosen is None and recent:
            chosen = recent[0]
        if chosen is None:
            if not targets:
                return None
            result = self._call("calculate_blast_radius", entity_ids=targets[:1])
            return RouterResult(answer=self._format_blast(result))

        lines = [
            f"{chosen.severity.value} — {chosen.origin_label}",
            "",
            f"Risk {chosen.risk.score:.0f}/100 is the sum of:",
        ]
        for item in chosen.risk.contributions:
            lines.append(f"  {item.points:+.0f}  {item.label}")
            if item.detail:
                lines.append(f"        {item.detail}")
        if targets:
            for record in [*chosen.direct_impact, *chosen.indirect_impact]:
                if record.entity_id not in targets:
                    continue
                lines.append("")
                lines.append(
                    f"{record.entity_name} specifically: modelled at "
                    f"{record.availability * 100:.0f}% availability, reached via"
                )
                lines.append(f"  {record.path.as_text()}")
                break
        lines.append("")
        lines.append(f"Confidence {chosen.confidence.score * 100:.0f}%")
        for item in chosen.confidence.strong_evidence[:3]:
            lines.append(f"  + {item}")
        for item in chosen.confidence.uncertainties[:3]:
            lines.append(f"  ? {item}")
        return RouterResult(answer="\n".join(lines))

    def _route_investigate(self, text: str) -> RouterResult | None:
        if not _INVESTIGATE.search(text):
            return None
        event_id = self._resolve_event(text)
        if event_id is None:
            return RouterResult(
                answer=(
                    "I could not tell which event to investigate. Select one in the event feed, "
                    "or name it — for example \"investigate the Taiwan earthquake\"."
                )
            )
        self._call("focus_event", event_id=event_id)
        result = self._call("calculate_blast_radius", event_id=event_id)
        return RouterResult(answer=self._format_blast(result))

    def _route_blast(self, text: str) -> RouterResult | None:
        if not _BLAST.search(text):
            return None
        event_id = self._resolve_event(text)
        if event_id is not None:
            result = self._call("calculate_blast_radius", event_id=event_id)
            return RouterResult(answer=self._format_blast(result))
        targets = self._resolve_targets(text)
        if not targets and self.ctx.selected_entity_id:
            targets = [self.ctx.selected_entity_id]
        if not targets:
            return RouterResult(
                answer=(
                    "Select an entity or event first, or name one — "
                    "\"show the blast radius for payments-k8s-singapore\"."
                )
            )
        result = self._call("calculate_blast_radius", entity_ids=targets)
        return RouterResult(answer=self._format_blast(result))

    def _route_dependencies(self, text: str) -> RouterResult | None:
        if not (_DEPENDS.search(text) or _DEPENDENTS.search(text)):
            return None
        targets = self._resolve_targets(text)
        if not targets and self.ctx.selected_entity_id:
            targets = [self.ctx.selected_entity_id]
        if not targets:
            return RouterResult(answer="Name or select an entity and I will trace its dependencies.")
        entity_id = targets[0]
        # The dependents form wins when both match: "what depends on X" contains the
        # substring "depends on", so testing dependencies first would always lose.
        downstream = bool(_DEPENDENTS.search(text))
        tool = "trace_dependents" if downstream else "trace_dependencies"
        result = self._call(tool, entity_id=entity_id, max_depth=4)
        heading = (
            f"What depends on {self._name(entity_id)}"
            if downstream
            else f"What {self._name(entity_id)} depends on"
        )
        lines = [f"{heading} ({len(result['nodes']) - 1} entities):", ""]
        for node in result["nodes"]:
            if node["depth"] == 0:
                continue
            lines.append(f"  {'  ' * (node['depth'] - 1)}{node['name']}  (depth {node['depth']})")
        if result.get("truncated"):
            lines.append("")
            lines.append("Traversal was truncated at the depth limit; there may be more.")
        self.ctx.emit("annotate", entity_ids=[n["id"] for n in result["nodes"]], label=heading)
        return RouterResult(answer="\n".join(lines))

    def _route_critical(self, text: str) -> RouterResult | None:
        if not _CRITICAL.search(text):
            return None
        result = self._call("search_entities", criticality="CRITICAL", limit=40)
        lines = [f"{result['count']} CRITICAL AtlasPay entities:", ""]
        for entity in result["entities"]:
            location = entity["region"] or "—"
            lines.append(f"  {entity['name']}  [{entity['type']}]  {location}")
        self.ctx.emit(
            "annotate",
            entity_ids=[e["id"] for e in result["entities"]],
            label="Critical infrastructure",
        )
        self.ctx.emit("show_layer", layer="dependencies", visible=True)
        return RouterResult(answer="\n".join(lines))

    def _route_entity_lookup(self, text: str) -> RouterResult | None:
        targets = self._resolve_targets(text)
        if not targets:
            return None
        entity = self._call("get_entity", entity_id=targets[0])
        lines = [
            f"{entity['name']} — {entity['type']}",
            f"Criticality {entity['criticality']}  ·  health {entity['health']}  ·  "
            f"{entity['mode']} data",
        ]
        if entity["description"]:
            lines.append(entity["description"])
        if entity["depends_on"]:
            lines.append("")
            lines.append("Depends on: " + ", ".join(self._name(d["id"]) for d in entity["depends_on"]))
        if entity["dependents"]:
            lines.append("Depended on by: " + ", ".join(self._name(d["id"]) for d in entity["dependents"]))
        self._call("focus_entity", entity_id=targets[0]) if entity["location"] else None
        return RouterResult(answer="\n".join(lines))

    # -- formatting ---------------------------------------------------------------------

    def _format_blast(self, result: dict[str, Any]) -> str:
        impact = result["business_impact"]
        lines = [
            f"{result['severity']} MATERIAL RISK — {result['origin']}",
            "",
            f"Risk {result['risk_score']:.0f}/100:",
        ]
        for row in result["risk_breakdown"]:
            lines.append(f"  {row['points']:+.0f}  {row['label']}")
        lines.append("")
        if result["direct_impact"]:
            lines.append(f"Directly exposed ({len(result['direct_impact'])}):")
            for row in result["direct_impact"][:6]:
                lines.append(f"  {row['name']} — {row['availability'] * 100:.0f}% availability")
        if result["indirect_impact"]:
            lines.append("")
            lines.append(f"Indirectly exposed ({len(result['indirect_impact'])}):")
            for row in result["indirect_impact"][:6]:
                lines.append(
                    f"  {row['name']} — {row['availability'] * 100:.0f}% "
                    f"(depth {row['depth']})"
                )
        if result["critical_paths"]:
            lines.append("")
            lines.append("Critical paths:")
            lines.extend(f"  {path}" for path in result["critical_paths"][:3])
        if result["customer_exposure"]:
            lines.append("")
            worst = result["customer_exposure"][0]
            lines.append(
                f"Estimated customer exposure: {worst['traffic_impact'] * 100:.0f}% of "
                f"{worst['region']} traffic ({worst['customers']:,} modelled customers)."
            )
        lines.append("")
        lines.append(
            f"Modelled availability {impact['availability'] * 100:.2f}%, "
            f"{impact['critical_services_impacted']} critical services impacted. "
            f"{impact['disclaimer']}."
        )
        confidence = result["confidence"]
        lines.append("")
        lines.append(f"Confidence {confidence['score'] * 100:.0f}%")
        for item in confidence["strong_evidence"][:3]:
            lines.append(f"  + {item}")
        for item in confidence["uncertainties"][:3]:
            lines.append(f"  ? {item}")
        if result.get("truncated"):
            lines.append("")
            lines.append("Traversal truncated — impact may extend beyond what is listed.")
        return "\n".join(lines)

    def _format_comparison(self, comparison: dict[str, Any]) -> str:
        lines = ["CURRENT vs SIMULATION", ""]
        width = max(len(row["label"]) for row in comparison["deltas"])
        for row in comparison["deltas"]:
            marker = {"worse": "▼", "better": "▲", "same": " "}[row["direction"]]
            lines.append(
                f"  {row['label']:<{width}}  {row['baseline']:>10}  →  {row['simulated']:>10}  {marker}"
            )
        if comparison["cascade_paths"]:
            lines.append("")
            lines.append("Cascading failure paths:")
            lines.extend(f"  {path}" for path in comparison["cascade_paths"][:4])
        if comparison["newly_impacted"]:
            lines.append("")
            lines.append(
                f"{len(comparison['newly_impacted'])} entities degraded by this scenario that "
                "are healthy in the baseline."
            )
        lines.append("")
        lines.append(comparison["note"])
        return "\n".join(lines)

    def _feed_note(self, feeds: list[dict[str, Any]]) -> str:
        parts = [f"{feed['adapter_name']}: {feed['state']}" for feed in feeds]
        return "Feeds — " + "  ·  ".join(parts)

    # -- resolution ---------------------------------------------------------------------

    def _resolve_targets(self, text: str) -> list[str]:
        """Find entity ids named in free text.

        Exact ids first, then whole-name matches, then region keywords. Ordered strictly so
        that "Singapore" in a sentence that also names ``payments-k8s-singapore`` resolves
        to the cluster the operator actually said.
        """
        lowered = text.lower()
        found: list[str] = []
        entities = self.ctx.state.entities()

        for entity in entities:
            if entity.id.lower() in lowered:
                found.append(entity.id)
        if found:
            return _dedupe(found)

        for entity in entities:
            name = entity.name.lower()
            if len(name) >= 6 and name in lowered:
                found.append(entity.id)
        if found:
            return _dedupe(found)

        for keyword, entity_id in _REGION_HINTS.items():
            if re.search(rf"\b{keyword}\b", lowered) and entity_id in self.ctx.state.graph:
                found.append(entity_id)
        return _dedupe(found)

    def _resolve_event(self, text: str) -> str | None:
        """Find the event a message refers to."""
        lowered = text.lower()
        events = self.ctx.state.events(limit=50)
        for event in events:
            if event.id.lower() in lowered:
                return event.id
        keywords = [
            ("taiwan", "earthquake"),
            ("earthquake", ""),
            ("quake", ""),
            ("singapore", "outage"),
            ("region", "outage"),
            ("cve", ""),
            ("vulnerab", ""),
        ]
        for primary, secondary in keywords:
            if primary not in lowered:
                continue
            for event in events:
                haystack = f"{event.title} {event.category.value}".lower()
                if primary in haystack and (not secondary or secondary in haystack):
                    return event.id
        return self.ctx.selected_event_id

    def _name(self, entity_id: str) -> str:
        entity = self.ctx.state.entity(entity_id)
        return entity.name if entity else entity_id

    def _latest_analysis(self, text: str) -> dict[str, Any] | None:
        event_id = self._resolve_event(text)
        if event_id is not None:
            return self._call("calculate_blast_radius", event_id=event_id)
        recent = self.ctx.state.recent_analyses(limit=1)
        if not recent:
            return None
        from .tools import _blast_result_row

        return _blast_result_row(recent[0])

    def _help_text(self) -> str:
        return (
            "I did not recognise that. I can:\n"
            "  · show critical infrastructure\n"
            "  · tell you what can hurt us right now\n"
            "  · investigate an event (\"investigate the Taiwan earthquake\")\n"
            "  · trace dependencies (\"what depends on payments-k8s-singapore?\")\n"
            "  · calculate a blast radius\n"
            "  · simulate a failure (\"what happens if Singapore goes offline?\")\n"
            "  · compare a scenario against baseline\n"
            "  · show vulnerability exposure and attack paths\n"
            "  · generate a response plan (\"what should we do?\")"
        )


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
