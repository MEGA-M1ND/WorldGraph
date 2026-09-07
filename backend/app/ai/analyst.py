"""The AI analyst.

Two backends, one contract:

* :class:`DeterministicAnalyst` — the intent router. Always available, no key, no network.
* :class:`ModelAnalyst` — Anthropic tool-use loop over the *same* tool registry.

The model backend adds language and intent flexibility. It does not add facts: every number
it can say came from a deterministic tool result. When it is unavailable — no key, a
timeout, an API error — the analyst falls back to the router and says so, rather than
returning "something went wrong" and leaving the operator with nothing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from ..services.world_state import WorldState
from .grounding import check as check_grounding
from .prompts import DETERMINISTIC_NOTICE, SYSTEM_PROMPT
from .router import IntentRouter
from .tools import ToolContext, ToolError, run_tool, tool_definitions

logger = logging.getLogger("worldgraph.ai")

#: Maximum tool-use rounds in one conversation turn. A model that has not answered after
#: this many calls is looping, and an unbounded loop is an unbounded bill.
MAX_TOOL_ROUNDS = 8

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(slots=True)
class AnalystAnswer:
    """One analyst response."""

    answer: str
    #: ``deterministic`` or ``model``. Surfaced in the UI so the operator knows which.
    engine: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Camera / highlight directives for the frontend to apply.
    directives: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the model backend was expected but could not be used.
    degraded_reason: str | None = None
    active_scenario_id: str | None = None
    duration_ms: float = 0.0


class DeterministicAnalyst:
    """Rule-based analyst over the deterministic tool layer."""

    engine = "deterministic"

    def __init__(self, state: WorldState) -> None:
        self.state = state

    async def ask(
        self,
        message: str,
        *,
        selected_entity_id: str | None = None,
        selected_event_id: str | None = None,
        active_scenario_id: str | None = None,
    ) -> AnalystAnswer:
        started = time.perf_counter()
        ctx = ToolContext(
            state=self.state,
            selected_entity_id=selected_entity_id,
            selected_event_id=selected_event_id,
            active_scenario_id=active_scenario_id,
        )
        result = IntentRouter(ctx).handle(message)
        self.state.record_ai_timeline(
            f"Analyst (deterministic) answered using {len(result.tool_calls)} tool calls."
        )
        return AnalystAnswer(
            answer=result.answer,
            engine=self.engine,
            tool_calls=result.tool_calls,
            directives=result.directives,
            active_scenario_id=ctx.active_scenario_id,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )


class ModelAnalyst:
    """Anthropic-backed analyst running a bounded tool-use loop."""

    engine = "model"

    def __init__(self, state: WorldState, settings: Settings) -> None:
        self.state = state
        self.settings = settings
        self._fallback = DeterministicAnalyst(state)

    async def ask(
        self,
        message: str,
        *,
        selected_entity_id: str | None = None,
        selected_event_id: str | None = None,
        active_scenario_id: str | None = None,
    ) -> AnalystAnswer:
        started = time.perf_counter()
        ctx = ToolContext(
            state=self.state,
            selected_entity_id=selected_entity_id,
            selected_event_id=selected_event_id,
            active_scenario_id=active_scenario_id,
        )
        try:
            answer, calls, outputs = await self._run_loop(ctx, message)
        except Exception as error:
            reason = _describe_model_error(error)
            logger.warning("analyst_model_failed reason=%s", reason)
            fallback = await self._fallback.ask(
                message,
                selected_entity_id=selected_entity_id,
                selected_event_id=selected_event_id,
                active_scenario_id=active_scenario_id,
            )
            fallback.degraded_reason = reason
            return fallback

        grounding = check_grounding(answer, message, outputs)
        if not grounding.is_grounded and grounding.had_no_data:
            # The model stated figures having consulted nothing. Those figures are invented
            # by construction — there was no source for them to come from — so the answer
            # is not returned. The deterministic router answers instead: it reads the same
            # world through the same tools and cannot fabricate a number.
            logger.warning(
                "analyst_answer_ungrounded tool_calls=0 figures=%s",
                ",".join(grounding.ungrounded[:5]),
            )
            self.state.record_ai_timeline(
                "Model answer withheld: unsourced figures with no tool calls. "
                "Answered deterministically instead."
            )
            fallback = await self._fallback.ask(
                message,
                selected_entity_id=selected_entity_id,
                selected_event_id=selected_event_id,
                active_scenario_id=active_scenario_id,
            )
            fallback.degraded_reason = grounding.describe()
            return fallback

        if not grounding.is_grounded:
            # Figures that no tool result contains, from a model that did call tools. The
            # answer is kept — it may be a restatement this checker cannot recognise — but
            # it is never passed off as verified.
            logger.warning(
                "analyst_answer_partially_ungrounded tool_calls=%d figures=%s",
                len(calls),
                ",".join(grounding.ungrounded[:5]),
            )
            answer = (
                f"{answer}\n\n[UNVERIFIED] {grounding.describe()} "
                "Check it against the panels before acting on it."
            )

        self.state.record_ai_timeline(
            f"Analyst ({self.settings.anthropic_model}) answered using {len(calls)} tool calls."
        )
        return AnalystAnswer(
            answer=answer,
            engine=self.engine,
            tool_calls=calls,
            directives=list(ctx.directives),
            active_scenario_id=ctx.active_scenario_id,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def _run_loop(
        self, ctx: ToolContext, message: str
    ) -> tuple[str, list[dict[str, Any]], list[Any]]:
        """Bounded tool-use loop.

        Returns the answer, the call log, and the raw tool outputs. The third is what makes
        the answer checkable: without the results there is nothing to hold its figures
        against.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": _selected_context_block(ctx) + message}
        ]
        calls: list[dict[str, Any]] = []
        outputs: list[Any] = []

        async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
            for _ in range(MAX_TOOL_ROUNDS):
                payload = {
                    "model": self.settings.anthropic_model,
                    "max_tokens": self.settings.ai_max_output_tokens,
                    "system": SYSTEM_PROMPT,
                    "tools": tool_definitions(),
                    "messages": messages,
                }
                response = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": self.settings.anthropic_api_key or "",
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()

                content = body.get("content") or []
                tool_uses = [block for block in content if block.get("type") == "tool_use"]
                text = "\n".join(
                    block.get("text", "") for block in content if block.get("type") == "text"
                ).strip()

                if not tool_uses:
                    return (
                        text or "I could not produce an answer for that.",
                        calls,
                        outputs,
                    )

                messages.append({"role": "assistant", "content": content})
                results = []
                for use in tool_uses:
                    name = str(use.get("name", ""))
                    args = use.get("input") if isinstance(use.get("input"), dict) else {}
                    try:
                        output = run_tool(ctx, name, args)
                        calls.append({"tool": name, "args": args, "ok": True})
                        outputs.append(output)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": use.get("id"),
                                "content": json.dumps(output, default=str)[:60_000],
                            }
                        )
                    except ToolError as error:
                        calls.append({"tool": name, "args": args, "ok": False, "error": str(error)})
                        # A tool error is returned to the model as a result, not raised.
                        # The model can correct a bad argument; a raised exception just
                        # loses the turn.
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": use.get("id"),
                                "is_error": True,
                                "content": str(error),
                            }
                        )
                messages.append({"role": "user", "content": results})

        return (
            "I reached the tool-call limit for this question without finishing. "
            "Try asking for one thing at a time.",
            calls,
            outputs,
        )


def _selected_context_block(ctx: ToolContext) -> str:
    """Tell the model what the operator has open — ids only, not the world.

    This is the "do not dump application state into the prompt" rule in practice: the model
    gets pointers and calls a tool if it needs the contents.
    """
    parts = []
    if ctx.selected_entity_id:
        parts.append(f"selected_entity_id={ctx.selected_entity_id}")
    if ctx.selected_event_id:
        parts.append(f"selected_event_id={ctx.selected_event_id}")
    if ctx.active_scenario_id:
        parts.append(f"active_scenario_id={ctx.active_scenario_id}")
    if not parts:
        return ""
    return f"[operator context: {', '.join(parts)}]\n\n"


def _describe_model_error(error: Exception) -> str:
    """A user-safe reason the model backend was unavailable."""
    if isinstance(error, httpx.TimeoutException):
        return "The language model timed out."
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code == 401:
            return "The configured model credential was rejected."
        if code == 429:
            return "The language model is rate-limited."
        return f"The language model returned HTTP {code}."
    if isinstance(error, httpx.HTTPError):
        return "The language model is unreachable."
    return f"The language model call failed ({type(error).__name__})."


def build_analyst(state: WorldState, settings: Settings):
    """Pick the analyst backend for this deployment."""
    if settings.ai_enabled:
        return ModelAnalyst(state, settings)
    return DeterministicAnalyst(state)


def analyst_status(settings: Settings) -> dict[str, Any]:
    """What the UI shows in the analyst header."""
    if settings.ai_enabled:
        return {
            "engine": "model",
            "model": settings.anthropic_model,
            "notice": None,
            "available": True,
        }
    return {
        "engine": "deterministic",
        "model": None,
        "notice": DETERMINISTIC_NOTICE,
        "available": True,
    }
