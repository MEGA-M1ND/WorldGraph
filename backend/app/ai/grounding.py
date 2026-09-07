"""Checking that the figures in a model's answer came from somewhere.

The tool layer is an allowlist, and it works: the model cannot execute, cannot reach the
filesystem, cannot invent an entity. But the allowlist governs what the model may *do*,
and nothing governed what it may *say*. Its final prose was returned verbatim, so a
response claiming "123,456 compromised hosts" with **zero tool calls** was passed through
to the operator as an answer.

``SECURITY.md`` claimed the model "cannot compute and cannot act". The second half was
true and enforced. The first was not.

The rule here is deliberately narrow, because a narrow rule can be correct:

    A number in the answer that appears neither in the operator's question nor in any
    tool result is a number the model made up.

That is decidable without guessing at intent. It does not attempt to police wording,
tone, or reasoning — only figures, which are the claims that get acted on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Numbers as they appear in prose: 41,500 · 0.9045 · 90.45% · $381,563.02 · 12
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

#: Figures small enough to be ordinary English rather than a claim about an estate —
#: "one of three options", "the top 5". Below this a match is not worth reporting, and
#: above it a bare number in a security answer is a quantity somebody may act on.
_TRIVIAL_MAX = 9.0

#: Tolerance when matching an answer's figure against a grounded one, as a fraction of the
#: grounded value. A tool returning 0.90451 and prose saying "90.45%" is the same fact
#: stated at a sane precision, not a fabrication.
_RELATIVE_TOLERANCE = 0.005


@dataclass(slots=True)
class Grounding:
    """Whether an answer's figures trace back to something WorldGraph actually produced."""

    #: Figures with no source in the question or the tool results.
    ungrounded: list[str] = field(default_factory=list)
    #: How many tool calls the answer was built from.
    tool_calls: int = 0

    @property
    def is_grounded(self) -> bool:
        return not self.ungrounded

    @property
    def had_no_data(self) -> bool:
        """The model answered without consulting WorldGraph at all."""
        return self.tool_calls == 0

    def describe(self) -> str:
        figures = ", ".join(self.ungrounded[:5])
        if self.had_no_data:
            return (
                f"The model stated {figures} without calling any WorldGraph tool, so the "
                "figures have no source in this workspace."
            )
        return (
            f"The model stated {figures}, which does not appear in any tool result it "
            "requested."
        )


def extract_numbers(text: str) -> list[float]:
    """Every number in a string, as floats. Malformed matches are skipped, not guessed."""
    found: list[float] = []
    for raw in _NUMBER.findall(text or ""):
        try:
            found.append(float(raw.replace(",", "")))
        except ValueError:  # pragma: no cover — the pattern makes this near-unreachable
            continue
    return found


def numbers_in(payload: Any) -> set[float]:
    """Every number anywhere in a tool result, including inside its strings.

    Serialising and re-scanning rather than walking typed fields: a tool may return a
    figure as a float, as a string, or embedded in a sentence it composed, and all three
    are equally legitimate sources for the model to quote back.
    """
    try:
        blob = json.dumps(payload, default=str)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        blob = str(payload)
    return set(extract_numbers(blob))


def _matches(value: float, grounded: set[float]) -> bool:
    """Whether ``value`` is one of the grounded figures, allowing sane restatement."""
    for source in grounded:
        if value == source:
            return True
        # A fraction restated as a percentage, or the reverse. Both are the same fact.
        for candidate in (source, source * 100.0, source / 100.0):
            if candidate == 0:
                if value == 0:
                    return True
                continue
            if abs(value - candidate) <= abs(candidate) * _RELATIVE_TOLERANCE:
                return True
            # Rounding to a shorter form: 0.90451 quoted as 0.9, 41500 as 41.5 (thousand).
            if round(candidate, 2) == round(value, 2):
                return True
    return False


def check(answer: str, question: str, tool_results: list[Any]) -> Grounding:
    """Find figures in ``answer`` that came from neither the question nor the tools.

    The question counts as a source because an operator quoting a CVE id or a threshold
    back at the model, and the model repeating it, invents nothing.
    """
    grounded: set[float] = set(extract_numbers(question))
    for result in tool_results:
        grounded |= numbers_in(result)

    ungrounded: list[str] = []
    for raw in _NUMBER.findall(answer or ""):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover
            continue
        if abs(value) <= _TRIVIAL_MAX:
            continue
        if _matches(value, grounded):
            continue
        ungrounded.append(raw)

    return Grounding(ungrounded=ungrounded, tool_calls=len(tool_results))
