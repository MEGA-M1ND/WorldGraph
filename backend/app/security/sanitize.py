"""Sanitization for untrusted external text.

Every string that arrives from a feed — an earthquake place name, a CVE description, a
supplier note, a status-page message — is untrusted. It is rendered in the UI and it is
placed near a language model, so it gets two independent defences:

1. **Structural** — length caps, control-character stripping, whitespace normalization.
   The UI never renders raw HTML from a feed, but a control character or a 40 KB blob can
   still wreck a layout or a log line.
2. **Instructional** — external text is fenced as DATA before it reaches a model and
   obvious prompt-injection scaffolding is neutralized. The system prompt states the
   hierarchy; this function makes the common attack shapes visibly inert so a reviewer can
   see the defence rather than trusting the prompt alone.

Neither defence is a substitute for the real control, which is architectural: the model
cannot execute anything except allowlisted, schema-validated tools. See ``SECURITY.md``.
"""

from __future__ import annotations

import re
import unicodedata

#: Default ceiling for a normalized feed string.
MAX_TEXT_LENGTH = 2000

#: Control characters other than tab/newline have no business in feed text.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Zero-width and bidirectional-override characters. These are how a payload hides one
#: string inside another that renders differently — worth removing on sight.
_INVISIBLE = re.compile(r"[​-‏  ‪-‮⁦-⁩﻿]")

_WHITESPACE = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\n{3,}")

#: Phrases that only appear in text trying to address a model rather than describe an
#: event. Matched case-insensitively and defanged, never silently dropped: an operator
#: investigating a suspicious feed needs to see that something tried this.
_INJECTION_PATTERNS = (
    r"ignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)",
    r"disregard (?:all |any )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)",
    r"you are (?:now|actually) (?:a|an|the)\b",
    r"</?(?:system|assistant|user|instructions?|tool_?call)>",
    r"\bsystem\s*(?:prompt|message|instruction)\b",
    r"\bnew instructions?\b",
    r"\boverride (?:your |the )?(?:instructions?|rules?|guardrails?)\b",
    r"\bdeveloper mode\b",
    r"\bprint (?:your |the )?(?:system prompt|instructions?)\b",
)
_INJECTION = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

#: What a neutralized phrase is replaced with. Visible on purpose.
_REDACTION = "[redacted-instruction-like-text]"


def sanitize_text(
    value: object,
    *,
    max_length: int = MAX_TEXT_LENGTH,
    strip_injection: bool = True,
) -> str:
    """Normalize one untrusted string.

    Args:
        value: anything a feed handed us; non-strings are coerced.
        max_length: hard ceiling. Truncation appends an ellipsis so it is visible.
        strip_injection: replace instruction-like phrases with a visible marker.

    Returns:
        A safe, bounded, single-spaced string.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)

    # NFKC folds the lookalike/compatibility forms an attacker would use to slip a phrase
    # past the pattern list below.
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    text = _NEWLINES.sub("\n\n", text)
    text = text.strip()

    if strip_injection:
        text = _INJECTION.sub(_REDACTION, text)

    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def contains_injection_attempt(value: str) -> bool:
    """Whether a string contains instruction-like scaffolding.

    Used for logging and for the UI's "this feed item was sanitized" marker, so a
    suspicious upstream is surfaced rather than silently cleaned.
    """
    if not value:
        return False
    normalized = unicodedata.normalize("NFKC", value)
    return bool(_INJECTION.search(normalized))


def sanitize_identifier(value: object, *, max_length: int = 128) -> str:
    """Reduce an untrusted string to a safe slug for use as an id.

    Rejects rather than truncates on grammar violation is not possible here — feed ids are
    arbitrary — so unsafe characters are replaced with ``-`` and the result is bounded.
    """
    text = sanitize_text(value, max_length=max_length * 2, strip_injection=False)
    cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", text).strip("-")
    return cleaned[:max_length] or "unknown"


def as_untrusted_block(label: str, text: str) -> str:
    """Fence external text for a model prompt.

    The fence is explicit and the label names the source, so the model's system prompt can
    refer to "content inside UNTRUSTED blocks" as a category rather than hoping it infers
    the boundary.
    """
    safe = sanitize_text(text)
    return f"<untrusted source=\"{sanitize_identifier(label)}\">\n{safe}\n</untrusted>"
