"""The workspace registry.

Owns one :class:`WorldState` per workspace and hands out the right one. Isolation is
structural: each workspace has its own graph, its own event store, its own simulation
scenarios and its own repository namespace. Nothing filters across workspaces because
nothing is shared to filter.

That matters more than it sounds. A blast radius that leaked from a demo fixture into a
real Azure subscription — or the reverse — would be the single most damaging bug this
product could ship, and a shared collection with a ``workspace_id`` column is exactly how
that bug gets written.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..fixtures.atlaspay import build_atlaspay
from ..models.workspace import (
    ATLASPAY_WORKSPACE_ID,
    ImportSummary,
    Workspace,
    WorkspaceKind,
    WorkspaceStatus,
    atlaspay_workspace,
)
from ..storage.repository import Repository, SqliteRepository
from .world_state import WorldState

logger = logging.getLogger("worldgraph.workspaces")


def default_repository_for(settings: Settings, workspace_id: str) -> Repository:
    """One SQLite store per workspace, namespaced by id so two estates never share a file.

    Filesystem separation rather than a ``workspace_id`` column: a query cannot reach
    another workspace's rows if those rows are in a different database.
    """
    path = settings.database_path
    if path == ":memory:":
        return SqliteRepository(":memory:")
    suffix = "" if workspace_id == ATLASPAY_WORKSPACE_ID else f".{workspace_id}"
    if path.endswith(".db"):
        return SqliteRepository(f"{path[:-3]}{suffix}.db")
    return SqliteRepository(f"{path}{suffix}")


class WorkspaceError(LookupError):
    """A workspace was requested that does not exist or cannot be loaded."""


class WorkspaceRegistry:
    """Holds every workspace and its isolated world."""

    def __init__(self, settings: Settings, repository_factory=None) -> None:
        self.settings = settings
        # A factory rather than a shared connection: each workspace gets its own store, so
        # a query cannot reach another workspace's rows even by mistake.
        self._repository_factory = repository_factory or self._default_repository
        self._workspaces: dict[str, Workspace] = {}
        self._states: dict[str, WorldState] = {}
        self._summaries: dict[str, ImportSummary] = {}
        self._default_id = ATLASPAY_WORKSPACE_ID

    def _default_repository(self, workspace_id: str) -> Repository:
        return default_repository_for(self.settings, workspace_id)

    # -- lifecycle ---------------------------------------------------------------------

    async def startup(self) -> None:
        """Register and load the workspaces this deployment is configured for.

        The demo always loads. Azure is registered only when configured, and a failure to
        reach it leaves the workspace listed as UNAVAILABLE with a reason rather than
        hidden — a configuration problem must not look like an empty estate.
        """
        demo = atlaspay_workspace()
        entities, edges = build_atlaspay()
        state = WorldState(
            self.settings,
            self._repository_factory(demo.id),
            workspace=demo,
            estate_loader=lambda: (entities, edges),
        )
        await state.startup()
        self._register(demo, state)

        for workspace in self._configured_import_workspaces():
            self._workspaces[workspace.id] = workspace

    def _configured_import_workspaces(self) -> list[Workspace]:
        """Workspaces this deployment declares but has not loaded yet.

        Import adapters register themselves here. Kept as a hook rather than a hardcoded
        list so adding a source is a new module, not an edit to the registry.
        """
        from ..adapters.azure_inventory import configured_azure_workspaces

        return configured_azure_workspaces(self.settings)

    async def shutdown(self) -> None:
        for state in self._states.values():
            await state.shutdown()
        self._states.clear()

    def _register(self, workspace: Workspace, state: WorldState) -> None:
        workspace.entity_count = len(state.graph)
        workspace.edge_count = len(state.graph.edges)
        workspace.status = WorkspaceStatus.READY
        self._workspaces[workspace.id] = workspace
        self._states[workspace.id] = state

    # -- access ------------------------------------------------------------------------

    @property
    def default_id(self) -> str:
        return self._default_id

    def list(self) -> list[Workspace]:
        """Every workspace, demo first, then by name.

        The demo leads because it is the only one guaranteed to work, and an operator
        opening WorldGraph for the first time should land somewhere that does.
        """
        return sorted(
            self._workspaces.values(),
            key=lambda w: (w.kind is not WorkspaceKind.DEMO, w.name),
        )

    def get(self, workspace_id: str | None) -> Workspace:
        workspace = self._workspaces.get(workspace_id or self._default_id)
        if workspace is None:
            raise WorkspaceError(f"No workspace '{workspace_id}' is configured.")
        return workspace

    def state(self, workspace_id: str | None) -> WorldState:
        """The world for a workspace.

        Raises rather than falling back to the default. A silent fallback would answer a
        question about an Azure subscription with data from a demo fixture, which is the
        contamination this class exists to prevent.
        """
        workspace = self.get(workspace_id)
        state = self._states.get(workspace.id)
        if state is None:
            raise WorkspaceError(
                f"Workspace '{workspace.name}' is not loaded"
                + (f": {workspace.message}" if workspace.message else ".")
            )
        return state

    def summary(self, workspace_id: str | None) -> ImportSummary | None:
        return self._summaries.get(self.get(workspace_id).id)

    def is_loaded(self, workspace_id: str) -> bool:
        return workspace_id in self._states

    # -- imports -----------------------------------------------------------------------

    async def load_import(
        self, workspace_id: str, *, force: bool = False
    ) -> ImportSummary:
        """Import (or re-import) an inventory workspace.

        Read-only end to end: the adapter queries, WorldGraph normalizes, nothing is
        written back to the source.
        """
        workspace = self.get(workspace_id)
        if workspace.source.value == "FIXTURE":
            raise WorkspaceError(
                f"'{workspace.name}' is a built-in demo workspace and cannot be imported."
            )
        if self.is_loaded(workspace.id) and not force:
            existing = self._summaries.get(workspace.id)
            if existing is not None:
                return existing

        from ..adapters.azure_inventory import import_azure_workspace

        workspace.status = WorkspaceStatus.LOADING
        try:
            entities, edges, summary = await import_azure_workspace(
                self.settings, workspace
            )
        except Exception as error:  # surfaced to the operator, never crashes the app
            workspace.status = WorkspaceStatus.UNAVAILABLE
            workspace.message = _safe_reason(error)
            logger.warning(
                "workspace_import_failed workspace=%s reason=%s",
                workspace.id,
                workspace.message,
            )
            raise WorkspaceError(
                f"Could not import '{workspace.name}': {workspace.message}"
            ) from error

        state = WorldState(
            self.settings,
            self._repository_factory(workspace.id),
            workspace=workspace,
            estate_loader=lambda: (entities, edges),
        )
        await state.startup()
        # Replace wholesale rather than merging. A re-import must not leave orphans from a
        # previous run pretending to still exist.
        old = self._states.pop(workspace.id, None)
        if old is not None:
            await old.shutdown()
        self._register(workspace, state)
        workspace.last_refreshed_at = summary.refreshed_at
        workspace.message = None
        self._summaries[workspace.id] = summary
        logger.info(
            "workspace_imported workspace=%s entities=%d edges=%d unsupported=%d",
            workspace.id,
            len(entities),
            len(edges),
            summary.resources_unsupported,
        )
        return summary


def _safe_reason(error: Exception) -> str:
    """A user-safe failure reason.

    Only the message an adapter deliberately authored, or the exception type. A cloud SDK
    error can carry a request URL, a tenant id or a token fragment, and none of those
    belong in an API response or a log line.
    """
    from ..adapters.base import AdapterError

    if isinstance(error, AdapterError):
        return str(error)
    return f"{type(error).__name__} while importing inventory"
