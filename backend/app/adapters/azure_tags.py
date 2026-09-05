"""The ``worldgraph.*`` Azure tag convention.

Azure inventory is rich in topology and nearly silent on business meaning. It knows a
cluster is in Southeast Asia; it has no idea whether the business would notice losing it.
Tags are the one place an operator can tell WorldGraph what the cloud cannot.

Four rules, and they are the whole design:

1. **Optional.** An estate with no tags imports fine and reports its gaps honestly.
2. **Explicit.** Only the ``worldgraph.`` namespace is read. WorldGraph never infers
   business meaning from ``env=prod``, a naming convention, or a resource group.
3. **Validated.** A malformed value is *rejected and reported*, never guessed at. Reading
   ``criticality: very important`` as CRITICAL would be exactly the fabrication this phase
   exists to remove.
4. **Read-only.** WorldGraph never writes a tag. It has no permission to and no reason to.

Every value that survives validation carries provenance recording that a human declared
it, which is a stronger claim than anything WorldGraph derives on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.core import Criticality
from ..security.sanitize import sanitize_text

#: The namespace. Anything outside it is ordinary cloud metadata and is never interpreted.
TAG_PREFIX = "worldgraph."

TAG_SERVICE = "worldgraph.service"
TAG_CRITICALITY = "worldgraph.criticality"
TAG_CUSTOMER_FACING = "worldgraph.customer_facing"
TAG_DEPENDS_ON = "worldgraph.depends_on"
TAG_OWNER = "worldgraph.owner"
TAG_REVENUE_PER_HOUR = "worldgraph.revenue_per_hour"

KNOWN_TAGS: frozenset[str] = frozenset(
    {
        TAG_SERVICE,
        TAG_CRITICALITY,
        TAG_CUSTOMER_FACING,
        TAG_DEPENDS_ON,
        TAG_OWNER,
        TAG_REVENUE_PER_HOUR,
    }
)

#: Accepted spellings for a boolean tag. Anything else is rejected rather than guessed:
#: "maybe", "1?" and "prod" are not booleans and must not silently become one.
_TRUE = frozenset({"true", "yes", "1", "y"})
_FALSE = frozenset({"false", "no", "0", "n"})

#: Ceiling on a declared revenue figure. Above this the value is far more likely a typo or
#: a misplaced currency than a real hourly figure, and a wrong number here propagates
#: straight into "revenue at risk".
_MAX_REVENUE_PER_HOUR = 1_000_000_000.0

#: Ceiling on declared dependencies per resource. A tag listing hundreds of ids is a
#: templating accident, not a dependency declaration.
_MAX_DEPENDS_ON = 24

#: Length ceiling per tag value. ``depends_on`` gets a much larger one because an Azure
#: resource id is around 150 characters and a legitimate list of a dozen runs past 2 KB.
#: Truncating that list would silently discard declared dependencies — WorldGraph would
#: read three of a declared twelve and report the graph as complete, which is precisely the
#: silent guessing this module exists to prevent. A value over the ceiling is rejected
#: whole, never trimmed.
_MAX_VALUE_LENGTH = 512
_MAX_DEPENDS_ON_LENGTH = 8192

#: An Azure resource id names a provider and a type, not just a subscription. Requiring the
#: full shape means a fragment like ``/subscriptions/x`` — which could never resolve to a
#: resource — is reported to the operator instead of being carried around as a dependency
#: on nothing.
_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/[^/]+/[^/]+/[^/]+",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TagRejection:
    """One tag value WorldGraph refused, and why.

    Surfaced in the import summary. A silently ignored tag is worse than a rejected one:
    the operator believes they declared something and WorldGraph believes they did not.
    """

    resource: str
    tag: str
    value: str
    reason: str

    def describe(self) -> str:
        return f"{self.resource}: {self.tag}={self.value!r} — {self.reason}"


@dataclass(slots=True)
class ParsedTags:
    """What a resource's ``worldgraph.*`` tags declare."""

    service: str | None = None
    criticality: Criticality | None = None
    customer_facing: bool | None = None
    #: Azure resource ids this resource declares a dependency on. Resolved to WorldGraph
    #: entity ids later, once the whole inventory is known — a tag may name a resource
    #: that has not been read yet, or one outside the subscription entirely.
    depends_on: list[str] = field(default_factory=list)
    owner: str | None = None
    revenue_per_hour: float | None = None
    rejections: list[TagRejection] = field(default_factory=list)

    @property
    def declares_anything(self) -> bool:
        return any(
            value is not None
            for value in (
                self.service,
                self.criticality,
                self.customer_facing,
                self.owner,
                self.revenue_per_hour,
            )
        ) or bool(self.depends_on)


def parse_tags(tags: dict[str, str] | None, *, resource: str) -> ParsedTags:
    """Read the ``worldgraph.*`` namespace from one resource's tags.

    Every string here was written by someone outside WorldGraph, so all of it is
    sanitized: a tag value is data, and an owner field containing "IGNORE PREVIOUS
    INSTRUCTIONS" is a suspicious string, not a request.
    """
    parsed = ParsedTags()
    if not tags:
        return parsed

    for raw_key, raw_value in tags.items():
        key = str(raw_key).strip().lower()
        if not key.startswith(TAG_PREFIX):
            continue

        # `depends_on` is a list and needs room; everything else is a short scalar.
        limit = _MAX_DEPENDS_ON_LENGTH if key == TAG_DEPENDS_ON else _MAX_VALUE_LENGTH
        raw_length = len(str(raw_value))
        if raw_length > limit:
            # Rejected whole rather than trimmed. A truncated list of dependencies would
            # look to WorldGraph exactly like a shorter list the operator meant to write.
            parsed.rejections.append(
                TagRejection(
                    resource,
                    key,
                    f"{raw_length} characters",
                    f"value exceeds the {limit}-character ceiling and was not truncated; "
                    "shorten it so nothing is silently dropped",
                )
            )
            continue

        value = sanitize_text(raw_value, max_length=limit)
        if not value:
            parsed.rejections.append(
                TagRejection(resource, key, str(raw_value), "value is empty")
            )
            continue

        if key not in KNOWN_TAGS:
            # Reported, not ignored. A typo like `worldgraph.criticallity` would otherwise
            # look to the operator exactly like a tag that worked.
            parsed.rejections.append(
                TagRejection(
                    resource,
                    key,
                    value,
                    "not a recognised WorldGraph tag; known tags are "
                    + ", ".join(sorted(KNOWN_TAGS)),
                )
            )
            continue

        if key == TAG_SERVICE:
            parsed.service = value[:120]

        elif key == TAG_CRITICALITY:
            try:
                criticality = Criticality(value.strip().upper())
            except ValueError:
                parsed.rejections.append(
                    TagRejection(
                        resource,
                        key,
                        value,
                        "not a criticality; expected one of "
                        + ", ".join(c.value for c in Criticality if c is not Criticality.UNKNOWN),
                    )
                )
            else:
                if criticality is Criticality.UNKNOWN:
                    # Declaring UNKNOWN is indistinguishable from not declaring, and
                    # accepting it would let a tag masquerade as a judgement.
                    parsed.rejections.append(
                        TagRejection(
                            resource, key, value, "UNKNOWN is not a declarable criticality"
                        )
                    )
                else:
                    parsed.criticality = criticality

        elif key == TAG_CUSTOMER_FACING:
            lowered = value.strip().lower()
            if lowered in _TRUE:
                parsed.customer_facing = True
            elif lowered in _FALSE:
                parsed.customer_facing = False
            else:
                parsed.rejections.append(
                    TagRejection(
                        resource, key, value, "not a boolean; expected true/false"
                    )
                )

        elif key == TAG_DEPENDS_ON:
            # Comma- or semicolon-separated Azure resource ids.
            candidates = [
                part.strip()
                for part in value.replace(";", ",").split(",")
                if part.strip()
            ]
            if len(candidates) > _MAX_DEPENDS_ON:
                parsed.rejections.append(
                    TagRejection(
                        resource,
                        key,
                        f"{len(candidates)} entries",
                        f"more than {_MAX_DEPENDS_ON} declared dependencies; "
                        "this looks like a templating error rather than a declaration",
                    )
                )
                continue
            for candidate in candidates:
                if not _RESOURCE_ID_PATTERN.match(candidate):
                    parsed.rejections.append(
                        TagRejection(
                            resource,
                            key,
                            candidate,
                            "not an Azure resource id (expected /subscriptions/<id>"
                            "/resourceGroups/<group>/providers/<provider>/<type>/<name>)",
                        )
                    )
                    continue
                parsed.depends_on.append(candidate)

        elif key == TAG_OWNER:
            parsed.owner = value[:160]

        elif key == TAG_REVENUE_PER_HOUR:
            cleaned = value.replace(",", "").replace("$", "").strip()
            try:
                revenue = float(cleaned)
            except ValueError:
                parsed.rejections.append(
                    TagRejection(resource, key, value, "not a number")
                )
            else:
                if revenue < 0:
                    parsed.rejections.append(
                        TagRejection(resource, key, value, "revenue cannot be negative")
                    )
                elif revenue > _MAX_REVENUE_PER_HOUR:
                    parsed.rejections.append(
                        TagRejection(
                            resource,
                            key,
                            value,
                            f"exceeds the sanity ceiling of {_MAX_REVENUE_PER_HOUR:,.0f} "
                            "per hour; check the units",
                        )
                    )
                else:
                    parsed.revenue_per_hour = revenue

    return parsed
