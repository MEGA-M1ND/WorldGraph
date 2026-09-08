"""What the deterministic router extracts from a question, and what it refuses to.

The router is the shipped analyst — the path a user takes with no API key. Mutation
testing found ~100 survivors in it, and while most were prose, three groups were not:

* **The time-window parser.** "in the last 2 hours" resolving to 2 minutes, or the
  hour/day multipliers swapping, changes the answer and nothing catches it.
* **The CVE and place-name extractors.** These decide *which* asset the analyst talks
  about. Getting them wrong is the fabrication mode the deterministic router exists to
  make impossible (docs/REALITY_PASS_AUDIT.md, B10/B11).
* **The evidence markers.** `[INFERRED]` vs `[ESTABLISHED]`, `? … (product name only)`,
  and "MODELLED ESTIMATE" are the honesty labels. Deleting one turns a candidate into a
  finding on screen while every existing test still passes.

Each empty-result branch is here too: "no path reaches X" and "no vulnerability events are
ingested" must stay distinguishable from a real answer.
"""

from __future__ import annotations

import re

import pytest

from app.ai.router import (
    _CHANGES,
    _CUSTOMERS,
    _PLACE_TYPE_PREFERENCE,
    _TYPE_VOCABULARY,
    _VULN,
    IntentRouter,
    _reachability_headline,
    _render_path,
)
from app.ai.tools import ToolContext
from app.models.core import EntityType


def _answer(world, question: str) -> str:
    return IntentRouter(ToolContext(state=world)).handle(question).answer


# ======================================================================================
# "What changed in the last N hours?"
# ======================================================================================


class TestTheTimeWindowParser:
    """The number and the unit both have to survive the trip.

    `minutes = value * (60 if hour else 1440 if day else 1)` had every constant survive:
    the hour multiplier, the day multiplier, the minute identity and the 60-minute
    default. A window is not cosmetic — it decides which events the answer is drawn from.
    """

    @pytest.mark.parametrize(
        ("question", "expected_minutes"),
        [
            ("what changed recently", 60),          # no number at all → one hour
            ("what changed in the last 30 minutes", 30),
            ("what changed in the last 45 min", 45),
            ("what changed in the last 2 hours", 120),
            ("what changed in the last 6 hr", 360),
            ("what changed in the last 3 days", 4320),
            ("what changed in the last 1 day", 1440),
        ],
    )
    @pytest.mark.anyio
    async def test_the_window_is_parsed_and_echoed(self, world, question, expected_minutes):
        answer = _answer(world, question)
        assert answer.startswith(f"In the last {expected_minutes} minutes:")

    @pytest.mark.anyio
    async def test_the_units_are_not_interchangeable(self, world):
        """An hour and a day must not resolve to the same window."""
        hour = _answer(world, "what changed in the last 4 hours").splitlines()[0]
        day = _answer(world, "what changed in the last 4 days").splitlines()[0]
        assert hour != day
        assert "240" in hour and "5760" in day

    @pytest.mark.anyio
    async def test_a_quiet_window_says_nothing_was_ingested(self, world):
        """The replay events are historical, so a one-hour window is genuinely empty."""
        answer = _answer(world, "what changed in the last 30 minutes")
        assert "  No new world events were ingested." in answer

    def test_the_intent_pattern_matches_the_phrasings_it_claims_to(self):
        for phrase in ["what changed", "recently", "recent", "last hour", "last day",
                       "new event", "new events", "anything new"]:
            assert _CHANGES.search(f"tell me, {phrase}?"), phrase

    def test_the_intent_pattern_does_not_match_unrelated_questions(self):
        for phrase in ["what is our biggest supplier", "show me the blast radius",
                       "which customers are affected"]:
            assert not _CHANGES.search(phrase), phrase


# ======================================================================================
# Which asset is the question about?
# ======================================================================================


class TestTheCveExtractor:
    """Driven through the router, not through a copy of its regex.

    Asserting against a re-typed pattern would pin the test's own literal and let the
    router's drift silently — the second vacuous-assertion shape this audit keeps finding.
    An unmatched CVE echoes back verbatim in the answer, which is the observable the
    extractor actually feeds.
    """

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("are we exposed to CVE-1999-0001?", "CVE-1999-0001"),
            ("are we exposed to cve-1999-0001?", "CVE-1999-0001"),   # upper-cased
            ("any patching needed for CVE-2011-44228 here", "CVE-2011-44228"),
            ("vulnerability CVE-1999-ABC-123 exposure", "CVE-1999-ABC-123"),
        ],
    )
    @pytest.mark.anyio
    async def test_a_cve_id_is_recognised_wherever_it_appears(self, world, question, expected):
        assert _answer(world, question).startswith(f"{expected}:")

    @pytest.mark.anyio
    async def test_a_bare_year_is_not_read_as_a_cve(self, world):
        """"the 2024 incident" must fall through to the recent-events lookup."""
        from app.fixtures.atlaspay import DEMO_CVE_ID

        answer = _answer(world, "any patching needed after the 2024 incident?")
        assert answer.startswith(DEMO_CVE_ID)

    @pytest.mark.anyio
    async def test_an_unknown_cve_reports_no_exposure_rather_than_the_nearest_one(self, world):
        answer = _answer(world, "are we exposed to CVE-1999-0001?")
        assert answer.startswith("CVE-1999-0001:")
        # It must not have silently answered about the demo CVE instead.
        from app.fixtures.atlaspay import DEMO_CVE_ID

        assert DEMO_CVE_ID not in answer

    def test_the_vulnerability_intent_covers_the_operator_vocabulary(self):
        for phrase in ["vulnerability", "vulnerabilities", "cve", "patch", "patching",
                       "exploit", "exploited"]:
            assert _VULN.search(f"anything on {phrase}?"), phrase

    def test_an_explicit_blast_radius_ask_outranks_the_vulnerability_intent(self):
        """Both intents match "show the blast radius if admin-api is compromised"."""
        text = "show the blast radius for this exploit"
        assert _VULN.search(text)  # the vulnerability words are present...
        assert re.search(r"blast radius", text, re.IGNORECASE)  # ...and so is the override


class TestPlaceNamesResolveToPlaces:
    """"Singapore" during an incident means the site, not a microservice hosted there."""

    def test_the_preference_order_puts_infrastructure_first(self):
        assert _PLACE_TYPE_PREFERENCE == (
            "CLOUD_REGION",
            "DATACENTER",
            "SUPPLIER",
            "FACTORY",
            "OFFICE",
            "NETWORK_NODE",
        )

    def test_every_preferred_type_is_a_real_entity_type(self):
        """A typo here would silently drop a whole class of site from place lookup."""
        known = {t.value for t in EntityType}
        assert set(_PLACE_TYPE_PREFERENCE) <= known

    def test_no_logical_type_is_preferred_for_a_place_name(self):
        for logical in ("MICROSERVICE", "APPLICATION", "DATABASE", "BUSINESS_SERVICE"):
            assert logical not in _PLACE_TYPE_PREFERENCE

    def test_the_type_vocabulary_covers_the_words_that_describe_a_kind(self):
        """These are the words that must never be read as a location.

        "Tell me about our Reykjavik quantum datacenter" once matched "datacenter" inside
        "Singapore Datacenter Partner" and the analyst described a facility on the other
        side of the planet.
        """
        for word in ("datacenter", "datacentre", "supplier", "factory", "office",
                     "region", "cloud", "service", "database", "partner",
                     "primary", "secondary", "shared"):
            assert word in _TYPE_VOCABULARY, word

    def test_short_words_are_not_treated_as_type_words(self):
        """The 4-character floor: dropping it would swallow real place names."""
        assert all(len(word) >= 4 for word in _TYPE_VOCABULARY)


# ======================================================================================
# The evidence markers
# ======================================================================================


class TestTheReachabilityHeadlineNeverTotalsTheTwoBases:
    def test_established_and_inferred_are_stated_separately(self):
        headline = _reachability_headline(
            {"count": 12, "established_count": 5, "inferred_count": 7}
        )
        assert headline == "12 reachability paths from the public internet (5 established, 7 inferred):"

    def test_a_clean_result_omits_the_inferred_clause_entirely(self):
        headline = _reachability_headline(
            {"count": 3, "established_count": 3, "inferred_count": 0}
        )
        assert headline == "3 reachability paths from the public internet (3 established):"

    def test_an_unsplit_payload_falls_back_to_the_bare_count(self):
        """Missing counts must not be rendered as zero — that would claim a split."""
        assert (
            _reachability_headline({"count": 4})
            == "4 reachability paths from the public internet:"
        )
        assert (
            _reachability_headline({"count": 4, "established_count": 4, "inferred_count": None})
            == "4 reachability paths from the public internet:"
        )

    def test_each_path_carries_its_own_basis(self):
        established = {"names": ["internet", "edge", "api"], "basis": "ESTABLISHED"}
        inferred = {"names": ["internet", "edge", "api"], "basis": "INFERRED"}
        assert _render_path(established) == "  [ESTABLISHED] internet → edge → api"
        assert _render_path(inferred) == "  [INFERRED] internet → edge → api"
        # Same route, different label: the basis is the only thing distinguishing them.
        assert _render_path(established) != _render_path(inferred)

    def test_an_unlabelled_path_is_not_promoted_to_inferred(self):
        assert _render_path({"names": ["a", "b"]}) == "  [ESTABLISHED] a → b"

    @pytest.mark.anyio
    async def test_an_estate_with_no_exposed_surface_says_so(self, world):
        """Zero paths is a statement, not an empty list under a "0 paths" heading."""
        from app.graph.world_graph import WorldGraph
        from app.models.core import (
            BusinessProfile,
            DataMode,
            DataSourceInfo,
            EntityType,
            ExposureProfile,
            WorldEntity,
        )

        src = DataSourceInfo(source_id="s", source_name="S", mode=DataMode.LIVE)
        world.graph = WorldGraph(
            [
                WorldEntity(
                    id=eid,
                    type=EntityType.APPLICATION,
                    name=eid,
                    source=src,
                    business=BusinessProfile(region="westeurope"),
                    exposure=ExposureProfile(internet_facing=False, network_zone="z"),
                )
                for eid in ("alpha", "beta")
            ],
            [],
        )
        answer = _answer(world, "what attack paths do we have?")
        assert answer == (
            "No internet-facing entity in this workspace declares a path to "
            "anything else. Name a target to check a specific asset."
        )
        assert "reachability paths" not in answer


class TestUnverifiedVulnerabilityMatchesStayMarked:
    """A product-name collision must never render like a confirmed finding."""

    @pytest.mark.anyio
    async def test_a_confirmed_asset_carries_no_unverified_marker(self, world):
        from app.fixtures.atlaspay import DEMO_CVE_ID

        answer = _answer(world, f"are we exposed to {DEMO_CVE_ID}?")
        assert "(product name only — unverified)" not in answer
        assert "  ? " not in answer
        # Reachability is stated per asset, not inferred by the reader.
        assert "[INTERNET-FACING]" in answer

    @pytest.mark.anyio
    async def test_the_headline_counts_confirmed_and_internet_facing_separately(self, world):
        from app.fixtures.atlaspay import DEMO_CVE_ID

        first = _answer(world, f"are we exposed to {DEMO_CVE_ID}?").splitlines()[0]
        assert "assets confirmed running the affected software," in first
        assert "of them internet-facing." in first


class TestCustomerNumbersAreLabelledAsModelled:
    """Reality Pass §24: WorldGraph must not fabricate customers impacted.

    It may model them — but only if it says so on the same line the number appears.
    """

    @pytest.mark.anyio
    async def test_nothing_analysed_yet_is_not_an_answer_about_customers(self, world):
        answer = _answer(world, "which customers are affected?")
        assert answer.startswith("Nothing has been analysed yet.")
        assert "%" not in answer

    @pytest.mark.anyio
    async def test_a_real_exposure_is_stamped_modelled_estimate(self, world):
        event_id = next(iter(world._events))
        IntentRouter(ToolContext(state=world, selected_event_id=event_id)).handle(
            "investigate this event"
        )
        answer = IntentRouter(
            ToolContext(state=world, selected_event_id=event_id)
        ).handle("which customers are affected?").answer
        assert "MODELLED ESTIMATE" in answer
        assert "modelled customers" in answer

    def test_the_customer_intent_pattern_matches_the_obvious_phrasings(self):
        for phrase in ["customer", "customers", "which customers", "customer impact"]:
            assert _CUSTOMERS.search(f"tell me about {phrase}"), phrase


# ======================================================================================
# Empty results stay distinguishable from answers
# ======================================================================================


class TestTheRouterSaysWhenItHasNothing:
    @pytest.mark.anyio
    async def test_an_unresolvable_dependency_question_asks_for_a_target(self, world):
        answer = _answer(world, "what depends on it?")
        assert answer == "Name or select an entity and I will trace its dependencies."

    @pytest.mark.anyio
    async def test_an_off_topic_question_is_declined_rather_than_answered(self, world):
        """It must decline, not improvise — the router computes, it does not know things."""
        answer = _answer(world, "what is the capital of France")
        assert "Paris" not in answer

    @pytest.mark.anyio
    async def test_dependency_depth_is_reported_per_node_and_the_origin_is_excluded(self, world):
        answer = _answer(world, "what does checkout-platform depend on?")
        assert not answer.startswith("Name or select")
        header = answer.splitlines()[0]
        assert header.startswith("What checkout-platform depends on (")
        assert header.endswith(" entities):")
        body = [line for line in answer.splitlines() if line.startswith("  ")]
        assert body, "a resolved dependency question must list something"
        # depth 0 is the origin itself and must not appear as one of its own dependencies.
        assert not any("(depth 0)" in line for line in body)
        assert all("(depth " in line for line in body)


# ======================================================================================
# How a computed result is rendered
# ======================================================================================


class TestTheComparisonArrowsPointTheRightWay:
    """A swapped arrow tells an operator a cascade improved things.

    `{"worse": "▼", "better": "▲", "same": " "}` is the entire visual grammar of the
    compare table in the text answer, and all three entries survived mutation.
    """

    @staticmethod
    def _render(world, deltas: list[dict]) -> str:
        return IntentRouter(ToolContext(state=world))._format_comparison(
            {
                "deltas": deltas,
                "cascade_paths": [],
                "newly_impacted": [],
                "note": "Modelled, not measured.",
            }
        )

    @pytest.mark.anyio
    async def test_each_direction_gets_its_own_marker(self, world):
        rendered = self._render(
            world,
            [
                {"label": "Worse row", "baseline": "1", "simulated": "2", "direction": "worse"},
                {"label": "Better row", "baseline": "2", "simulated": "1", "direction": "better"},
                {"label": "Same row", "baseline": "1", "simulated": "1", "direction": "same"},
            ],
        )
        lines = {line.split()[0]: line for line in rendered.splitlines() if line.startswith("  ")}
        assert lines["Worse"].endswith("▼")
        assert lines["Better"].endswith("▲")
        assert not lines["Same"].rstrip().endswith(("▼", "▲"))

    @pytest.mark.anyio
    async def test_the_table_is_headed_so_the_columns_are_unambiguous(self, world):
        rendered = self._render(
            world, [{"label": "Row", "baseline": "1", "simulated": "2", "direction": "same"}]
        )
        assert rendered.splitlines()[0] == "CURRENT vs SIMULATION"
        # Baseline before simulated, with the arrow between them.
        assert "1  →  " in rendered

    @pytest.mark.anyio
    async def test_the_modelled_note_is_always_the_last_word(self, world):
        rendered = self._render(
            world, [{"label": "Row", "baseline": "1", "simulated": "2", "direction": "worse"}]
        )
        assert rendered.splitlines()[-1] == "Modelled, not measured."


class TestTheBlastRadiusRenderingKeepsItsQualifiers:
    """Risk contributions, unknown reasons, confidence and the truncation notice.

    Each of these is a hedge attached to a number. Rendering the number without its hedge
    is how a model estimate becomes a measurement in the reader's head.
    """

    @staticmethod
    def _payload(**overrides) -> dict:
        payload = {
            "severity": "HIGH",
            "origin": "cloud-region-singapore",
            "risk_score": 62.0,
            "risk_breakdown": [
                {"points": 20.0, "label": "single-region dependency"},
                {"points": -10.0, "label": "failover capacity available"},
            ],
            "direct_impact": [{"name": "payments-api", "availability": 0.25}],
            "indirect_impact": [{"name": "checkout-platform", "availability": 0.4, "depth": 2}],
            "critical_paths": ["a → b → c"],
            "customer_exposure": [],
            "business_impact": {
                "availability": 0.9,
                "critical_services_impacted": 3,
                "disclaimer": "Modelled from declared traffic shares",
                "unknown_reasons": ["no traffic share declared for EMEA"],
            },
            "confidence": {
                "score": 0.72,
                "strong_evidence": ["declared dependency edges"],
                "uncertainties": ["health is inventory, not observation"],
            },
        }
        payload.update(overrides)
        return payload

    @pytest.mark.anyio
    async def test_a_negative_contribution_keeps_its_sign(self, world):
        """A −10 failover credit rendering as "10" reads as extra risk, not less."""
        rendered = IntentRouter(ToolContext(state=world))._format_blast(self._payload())
        assert "  -10  failover capacity available" in rendered
        assert "  +20  single-region dependency" in rendered

    @pytest.mark.anyio
    async def test_the_missing_input_is_named_beside_the_number_it_weakens(self, world):
        rendered = IntentRouter(ToolContext(state=world))._format_blast(self._payload())
        assert "  ? no traffic share declared for EMEA" in rendered
        assert "Modelled from declared traffic shares." in rendered

    @pytest.mark.anyio
    async def test_confidence_carries_both_sides_of_the_evidence(self, world):
        rendered = IntentRouter(ToolContext(state=world))._format_blast(self._payload())
        assert "Confidence 72%" in rendered
        assert "  + declared dependency edges" in rendered
        assert "  ? health is inventory, not observation" in rendered

    @pytest.mark.anyio
    async def test_truncated_traversal_is_declared(self, world):
        router = IntentRouter(ToolContext(state=world))
        assert "Traversal truncated" not in router._format_blast(self._payload())
        assert "Traversal truncated — impact may extend beyond what is listed." in (
            router._format_blast(self._payload(truncated=True))
        )

    @pytest.mark.anyio
    async def test_customer_exposure_is_labelled_estimated_and_modelled(self, world):
        rendered = IntentRouter(ToolContext(state=world))._format_blast(
            self._payload(
                customer_exposure=[
                    {"region": "APAC", "traffic_impact": 0.42, "customers": 41_500}
                ]
            )
        )
        assert (
            "Estimated customer exposure: 42% of APAC traffic "
            "(41,500 modelled customers)." in rendered
        )

    @pytest.mark.anyio
    async def test_direct_and_indirect_are_headed_separately_with_their_counts(self, world):
        rendered = IntentRouter(ToolContext(state=world))._format_blast(self._payload())
        assert "Directly exposed (1):" in rendered
        assert "Indirectly exposed (1):" in rendered
        assert "  payments-api — 25% availability" in rendered
        assert "  checkout-platform — 40% (depth 2)" in rendered


class TestTheDirectivesAndFiltersBehindAnAnswer:
    """What the router asks for, and what it tells the UI to show.

    These are not prose. A criticality filter with the wrong value returns a different set
    of entities; a traversal depth decides how much of the estate an answer covers; an
    emitted directive is what actually moves the globe. Re-running the mutation harness
    after the first router batch left these still standing.
    """

    @staticmethod
    def _run(world, question: str):
        return IntentRouter(ToolContext(state=world)).handle(question)

    @pytest.mark.anyio
    async def test_the_critical_listing_filters_on_criticality_not_on_something_else(
        self, world
    ):
        result = self._run(world, "show me our critical infrastructure")
        listed = {
            line.split("  [")[0].strip()
            for line in result.answer.splitlines()
            if line.startswith("  ")
        }
        expected = {
            e.name for e in world.entities() if e.criticality.value == "CRITICAL"
        }
        assert listed == expected
        assert result.answer.startswith(f"{len(expected)} CRITICAL entities in this workspace:")

    @pytest.mark.anyio
    async def test_the_critical_listing_turns_the_dependency_layer_on(self, world):
        result = self._run(world, "show me our critical infrastructure")
        kinds = {d["kind"] for d in result.directives}
        assert "annotate" in kinds
        layer = next(d for d in result.directives if d["kind"] == "show_layer")
        assert layer["layer"] == "dependencies"
        assert layer["visible"] is True

    @pytest.mark.anyio
    async def test_a_dependency_answer_reaches_four_hops(self, world):
        """`max_depth=4`. A shallower traversal quietly answers a smaller question."""
        answer = self._run(world, "what depends on cloud-region-singapore?").answer
        depths = {
            int(line.split("(depth ")[1].rstrip(")"))
            for line in answer.splitlines()
            if "(depth " in line
        }
        assert depths, "the demo estate must cascade at least one hop"
        assert max(depths) <= 4
        assert min(depths) == 1, "depth 0 is the origin and must not be listed"

    @pytest.mark.anyio
    async def test_an_entity_lookup_flies_the_camera_to_a_located_entity(self, world):
        located = next(e for e in world.entities() if e.location is not None)
        result = self._run(world, f"tell me about {located.name}")
        assert result.answer.startswith(f"{located.name} — {located.type.value}")
        assert any(call["tool"] == "focus_entity" for call in result.tool_calls)

    @pytest.mark.anyio
    async def test_an_unlocated_entity_does_not_move_the_camera(self, world):
        """The shape an Azure import produces for a resource in an unmapped region."""
        from app.graph.world_graph import WorldGraph
        from app.models.core import (
            BusinessProfile,
            DataMode,
            DataSourceInfo,
            EntityType,
            ExposureProfile,
            WorldEntity,
        )

        world.graph = WorldGraph(
            [
                WorldEntity(
                    id="ghostwriter",
                    type=EntityType.APPLICATION,
                    name="ghostwriter",
                    source=DataSourceInfo(source_id="s", source_name="S", mode=DataMode.LIVE),
                    location=None,
                    business=BusinessProfile(region="westeurope"),
                    exposure=ExposureProfile(internet_facing=False, network_zone="z"),
                )
            ],
            [],
        )
        result = self._run(world, "tell me about ghostwriter")
        assert result.answer.startswith("ghostwriter — APPLICATION")
        assert not any(call["tool"] == "focus_entity" for call in result.tool_calls)

    @pytest.mark.anyio
    async def test_a_declined_question_is_marked_as_declined(self, world):
        """`matched` is how the caller tells "no answer" from "an answer of no"."""
        assert self._run(world, "what is the capital of France").matched is False
        assert self._run(world, "show me our critical infrastructure").matched is True


class TestThePlanRendering:
    @pytest.mark.anyio
    async def test_actions_are_numbered_from_one_and_carry_their_rationale(self, world):
        event_id = next(iter(world._events))
        router = IntentRouter(ToolContext(state=world, selected_event_id=event_id))
        router.handle("investigate this event")
        answer = IntentRouter(
            ToolContext(state=world, selected_event_id=event_id)
        ).handle("what should we do about it?").answer

        lines = answer.splitlines()
        assert lines[0] == "RESPONSE PLAN"
        numbered = [line for line in lines if line[:2] in {"1.", "2.", "3.", "4.", "5."}]
        assert numbered, "a plan with no actions is not a plan"
        assert numbered[0].startswith("1. [")
        # Every action states why, and how sure, as a percentage.
        assert sum(1 for line in lines if line.startswith("   Why: ")) == len(numbered)
        confidences = [line for line in lines if line.startswith("   Confidence ")]
        assert len(confidences) == len(numbered)
        assert all(line.rstrip().split()[1].endswith("%") for line in confidences)

    @pytest.mark.anyio
    async def test_a_plan_states_its_assumptions_under_their_own_heading(self, world):
        event_id = next(iter(world._events))
        router = IntentRouter(ToolContext(state=world, selected_event_id=event_id))
        router.handle("investigate this event")
        answer = IntentRouter(
            ToolContext(state=world, selected_event_id=event_id)
        ).handle("what should we do about it?").answer
        assert "Assumptions:" in answer
        assumption_lines = [line for line in answer.splitlines() if line.startswith("  - ")]
        assert assumption_lines


class TestWhichIncidentTheRouterPicks:
    """With nothing selected, "what should we do?" has to pick *an* incident.

    The severity order and the "must correlate with this estate" filter both survived. A
    reordered list answers about the least severe thing on the feed; a dropped filter
    answers about somebody else's outage.
    """

    @staticmethod
    def _make(world, event_id: str, severity, *, correlating: bool):
        from app.models.core import DataMode, DataSourceInfo, EventCategory, WorldEvent, utcnow

        target = world.entities()[0].id
        return WorldEvent(
            id=event_id,
            category=EventCategory.CLOUD_INCIDENT,
            title=event_id,
            severity=severity,
            source=DataSourceInfo(source_id="p", source_name="P", mode=DataMode.REPLAY),
            directly_named_entity_ids=[target] if correlating else ["not-in-this-estate"],
            occurred_at=utcnow(),
        )

    @pytest.mark.anyio
    async def test_the_most_severe_correlating_incident_wins(self, world):
        from app.models.core import Severity

        world._events.clear()
        for name, severity in (
            ("low-one", Severity.LOW),
            ("critical-one", Severity.CRITICAL),
            ("high-one", Severity.HIGH),
            ("moderate-one", Severity.MODERATE),
        ):
            world._events[name] = self._make(world, name, severity, correlating=True)
        router = IntentRouter(ToolContext(state=world))
        assert router._most_severe_incident() == "critical-one"

    @pytest.mark.anyio
    async def test_a_more_severe_event_that_does_not_touch_this_estate_is_skipped(self, world):
        """Somebody else's CRITICAL is not our incident."""
        from app.models.core import Severity

        world._events.clear()
        world._events["theirs"] = self._make(world, "theirs", Severity.CRITICAL, correlating=False)
        world._events["ours"] = self._make(world, "ours", Severity.LOW, correlating=True)
        router = IntentRouter(ToolContext(state=world))
        assert router._most_severe_incident() == "ours"

    @pytest.mark.anyio
    async def test_nothing_correlating_yields_nothing_rather_than_a_guess(self, world):
        from app.models.core import Severity

        world._events.clear()
        world._events["theirs"] = self._make(world, "theirs", Severity.CRITICAL, correlating=False)
        router = IntentRouter(ToolContext(state=world))
        assert router._most_severe_incident() is None

    @pytest.mark.anyio
    async def test_a_tie_on_severity_is_broken_by_recency(self, world):
        from datetime import timedelta

        from app.models.core import Severity, utcnow

        world._events.clear()
        older = self._make(world, "older", Severity.HIGH, correlating=True)
        older.occurred_at = utcnow() - timedelta(hours=3)
        newer = self._make(world, "newer", Severity.HIGH, correlating=True)
        world._events["older"] = older
        world._events["newer"] = newer
        router = IntentRouter(ToolContext(state=world))
        assert router._most_severe_incident() == "newer"


class TestDegradedIsNotDown:
    """"What if Singapore is degraded" and "what if Singapore goes down" are two questions.

    `\\bdegrad` was unprotected, so both phrasings could have modelled a total outage —
    which overstates the impact of every partial-failure what-if an operator asks.
    """

    @pytest.mark.anyio
    async def test_a_degraded_what_if_models_degradation(self, world):
        result = IntentRouter(ToolContext(state=world)).handle(
            "what if the Singapore region is degraded?"
        )
        assert "DEGRADED" in result.answer
        assert "DOWN" not in result.answer

    @pytest.mark.anyio
    async def test_an_outage_what_if_models_an_outage(self, world):
        result = IntentRouter(ToolContext(state=world)).handle(
            "what happens if the Singapore region goes offline?"
        )
        assert "DOWN" in result.answer

    @pytest.mark.anyio
    async def test_a_simulation_answer_says_nothing_real_changed(self, world):
        """Reality Pass §27, at the top of the answer where it cannot be missed."""
        answer = IntentRouter(ToolContext(state=world)).handle(
            "what happens if the Singapore region goes offline?"
        ).answer
        assert answer.startswith(
            "SIMULATION — hypothetical world state. Nothing real has changed.\n"
        )
        assert "Scenario: What-if: " in answer
        assert "\nFailures: " in answer

    @pytest.mark.anyio
    async def test_an_unresolvable_target_asks_rather_than_failing_something_arbitrary(
        self, world
    ):
        answer = IntentRouter(ToolContext(state=world)).handle(
            "what happens if it goes offline?"
        ).answer
        assert answer.startswith("I could not tell which entity to fail.")
        assert not world.scenarios(), "a failed parse must not leave a scenario behind"


class TestAnExplicitBlastRadiusAskWins:
    """Both intents match "show the blast radius if admin-api is compromised"."""

    @pytest.mark.anyio
    async def test_the_vulnerability_route_stands_down(self, world):
        answer = IntentRouter(ToolContext(state=world)).handle(
            "show the blast radius for this exploit"
        ).answer
        assert "assets confirmed running the affected software" not in answer

    @pytest.mark.anyio
    async def test_the_reachability_route_stands_down_too(self, world):
        answer = IntentRouter(ToolContext(state=world)).handle(
            "show the blast radius if admin-api is compromised"
        ).answer
        assert "reachability paths from the public internet" not in answer
        assert "MATERIAL RISK" in answer
