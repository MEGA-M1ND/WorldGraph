"""Workspaces — one estate each, and never each other's.

A workspace is a self-contained world: its own entities, edges, events, simulations and
analyses. AtlasPay is one; an imported Azure subscription is another. They must not mix,
because a blast radius that silently crossed from a demo fixture into a real subscription
would be worse than no answer at all.

Isolation here is structural rather than filtered: each workspace owns a separate
``WorldState`` with its own graph and its own repository namespace. There is no query that
can accidentally span two, because there is no shared collection to query.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .core import DataMode, utcnow


class WorkspaceKind(str, Enum):
    """What kind of estate a workspace holds.

    The distinction drives real behaviour, not a label: a ``REAL`` workspace suppresses
    the synthetic-data language in analyses and plans, and marks itself read-only in the
    UI so nobody mistakes a recommendation for an action taken against production.
    """

    #: Synthetic demonstration data authored in this repository.
    DEMO = "DEMO"
    #: Inventory imported from a real environment. Read-only, always.
    REAL = "REAL"


class InventorySourceKind(str, Enum):
    """Where a workspace's entities came from."""

    FIXTURE = "FIXTURE"
    AZURE = "AZURE"
    SNAPSHOT = "SNAPSHOT"


class WorkspaceStatus(str, Enum):
    """Whether a workspace can currently be loaded."""

    #: Loaded and queryable.
    READY = "READY"
    #: Configured but not yet imported.
    NOT_LOADED = "NOT_LOADED"
    #: Configured but its credentials or connectivity are unavailable. The workspace is
    #: still listed — hiding it would make a configuration problem look like an empty
    #: estate.
    UNAVAILABLE = "UNAVAILABLE"
    #: Import in flight.
    LOADING = "LOADING"


class Workspace(BaseModel):
    """A named, isolated estate."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    kind: WorkspaceKind
    source: InventorySourceKind
    status: WorkspaceStatus = WorkspaceStatus.NOT_LOADED

    #: Provenance mode for records this workspace produces. A demo fixture is SYNTHETIC;
    #: a live Azure import is LIVE; a saved import replayed from disk is REPLAY, because
    #: it is a recording and must never claim to be current.
    mode: DataMode = DataMode.SYNTHETIC

    #: Organisation name shown in the UI. Derived from the source, not hardcoded — the
    #: Reality Pass found "AtlasPay" wired into the dashboard regardless of what was
    #: loaded (docs/REALITY_PASS_AUDIT.md, B13).
    organization: str = Field(default="", max_length=120)

    #: One sentence the UI shows about what this estate is and is not.
    description: str = Field(default="", max_length=512)

    #: Present when the workspace could not be loaded. Specific, never "unavailable".
    message: str | None = Field(default=None, max_length=512)

    #: True for every workspace in V1. Present as a field so the UI can state it per
    #: workspace rather than relying on a global footnote.
    read_only: bool = True

    entity_count: int = 0
    edge_count: int = 0
    last_refreshed_at: str | None = None

    #: Bumped every time this workspace's world is rebuilt. Anything cached against a
    #: world — an analyst and its tools, above all — is keyed by it, so a re-import cannot
    #: leave a component answering questions from an estate that no longer exists.
    revision: int = 0

    @property
    def is_real(self) -> bool:
        return self.kind is WorkspaceKind.REAL

    def data_disclaimer(self) -> str:
        """What the operator must understand about this workspace's data."""
        if self.kind is WorkspaceKind.DEMO:
            return (
                f"{self.organization or self.name} is synthetic demonstration data. "
                "World events are labelled LIVE, REPLAY or SYNTHETIC individually."
            )
        if self.mode is DataMode.REPLAY:
            return (
                "This workspace is a saved inventory snapshot, not a live view. "
                "WorldGraph reads inventory only and has changed nothing."
            )
        return (
            "This workspace is imported read-only inventory. WorldGraph has changed "
            "nothing and cannot: it holds no write permission and executes no action."
        )


#: The demo workspace. Always present, always the default — WorldGraph must open to
#: something meaningful with no configuration at all.
ATLASPAY_WORKSPACE_ID = "atlaspay-demo"


def atlaspay_workspace() -> Workspace:
    return Workspace(
        id=ATLASPAY_WORKSPACE_ID,
        name="AtlasPay Demo",
        kind=WorkspaceKind.DEMO,
        source=InventorySourceKind.FIXTURE,
        status=WorkspaceStatus.READY,
        mode=DataMode.SYNTHETIC,
        organization="AtlasPay",
        description=(
            "A fictional multinational payments company with a fully declared business "
            "graph — traffic, revenue, customers, criticality and redundancy."
        ),
    )


class GraphCoverage(BaseModel):
    """What WorldGraph actually knows about a workspace, dimension by dimension.

    Deliberately **not** collapsed into one confidence percentage. A single number would
    average "we have complete hosting topology" together with "we know nothing about
    business services" and produce something meaningless — and, worse, something that
    looks precise. Each dimension is reported separately with the reason behind it, so a
    gap is actionable rather than mysterious.
    """

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    #: HIGH / PARTIAL / LOW / NONE. Never a percentage: the point is the shape of what is
    #: missing, not a score to optimise.
    level: str
    #: The count behind the level, so the operator can check the arithmetic.
    detail: str = Field(default="", max_length=280)
    #: What would raise it. Empty when the dimension is already complete.
    remedy: str = Field(default="", max_length=280)


class ImportSummary(BaseModel):
    """What one inventory import discovered — including what it could not use.

    Reporting unsupported resources is not an apology, it is the product telling the
    operator the boundary of its own knowledge. An import that silently dropped 44 of 187
    resources would leave them believing the graph is complete.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    source: InventorySourceKind
    subscription_label: str = Field(default="", max_length=200)

    resources_discovered: int = 0
    resources_supported: int = 0
    resources_unsupported: int = 0
    #: Azure resource type → count, for the types WorldGraph does not model.
    unsupported_types: dict[str, int] = Field(default_factory=dict)

    entities_created: int = 0
    #: Edges Azure itself proved, e.g. an explicit resource reference.
    explicit_edges: int = 0
    #: Edges an operator declared through WorldGraph tags.
    declared_edges: int = 0
    regions: int = 0

    #: Tag values that were rejected, with why. Never silently ignored.
    rejected_tags: list[str] = Field(default_factory=list)

    coverage: list[GraphCoverage] = Field(default_factory=list)
    mode: DataMode = DataMode.LIVE
    refreshed_at: str = Field(default_factory=lambda: utcnow().isoformat())
    duration_ms: float = 0.0

    @property
    def support_rate(self) -> float:
        if self.resources_discovered <= 0:
            return 0.0
        return self.resources_supported / self.resources_discovered
