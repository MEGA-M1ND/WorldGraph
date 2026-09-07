"""A re-import must replace the estate, and nothing may keep serving the old one.

Every test here uses a **disk-backed** database. That is the whole point: the existing
isolation suite passes `:memory:` repositories, which are fresh per construction, so a
re-import appeared to work while the persisted-estate path was never exercised. The bug
lived precisely in the gap between the test fixture and the deployment.

Two defects, one cause. The store outranked the loader, so a forced re-import kept stale
inventory; and the per-workspace analyst was cached by workspace id, which survives a
re-import even though the world it wraps does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import RunMode, Settings
from app.services.workspaces import WorkspaceRegistry
from app.storage.repository import SqliteRepository

SUBSCRIPTION = "/subscriptions/00000000-0000-0000-0000-000000000001"


def write_snapshot(path: Path, names: list[str]) -> None:
    """A snapshot naming exactly these web apps — the source of truth for an import."""
    path.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "id": f"{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.Web/sites/{name}",
                        "name": name,
                        "type": "microsoft.web/sites",
                        "location": "westeurope",
                        "resourceGroup": "rg",
                        "subscriptionId": "00000000-0000-0000-0000-000000000001",
                        "tags": {},
                        "properties": {"state": "Running"},
                    }
                    for name in names
                ]
            }
        )
    )


@pytest.fixture
def disk_registry(tmp_path: Path):
    """A registry backed by a real file, not `:memory:`."""
    snapshot = tmp_path / "snapshot.json"
    write_snapshot(snapshot, ["app-original"])
    settings = Settings(
        run_mode=RunMode.OFFLINE,
        database_path=str(tmp_path / "worldgraph.db"),
        azure_snapshot_path=str(snapshot),
    )
    return settings, snapshot


def names_in(state) -> set[str]:
    return {e.name for e in state.graph.entities}


# ======================================================================================
# The estate follows the source
# ======================================================================================


class TestReimportReplaces:
    @pytest.mark.anyio
    async def test_a_renamed_resource_propagates(self, disk_registry):
        """The reproduction: the source said RENAMED and the graph said ORIGINAL."""
        settings, snapshot = disk_registry
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        try:
            await registry.load_import("azure-snapshot")
            assert "app-original" in names_in(registry.state("azure-snapshot"))

            write_snapshot(snapshot, ["app-renamed"])
            await registry.load_import("azure-snapshot", force=True)

            names = names_in(registry.state("azure-snapshot"))
            assert "app-renamed" in names
            assert "app-original" not in names
        finally:
            await registry.shutdown()

    @pytest.mark.anyio
    async def test_a_deleted_resource_disappears(self, disk_registry):
        """INSERT OR REPLACE can add and update but never remove."""
        settings, snapshot = disk_registry
        write_snapshot(snapshot, ["keep-me", "delete-me"])
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        try:
            await registry.load_import("azure-snapshot")
            assert {"keep-me", "delete-me"} <= names_in(registry.state("azure-snapshot"))

            write_snapshot(snapshot, ["keep-me"])
            await registry.load_import("azure-snapshot", force=True)

            names = names_in(registry.state("azure-snapshot"))
            assert "keep-me" in names
            assert "delete-me" not in names, "an estate that can only grow mirrors nothing"
        finally:
            await registry.shutdown()

    @pytest.mark.anyio
    async def test_an_added_resource_appears(self, disk_registry):
        settings, snapshot = disk_registry
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        try:
            await registry.load_import("azure-snapshot")
            write_snapshot(snapshot, ["app-original", "app-new"])
            await registry.load_import("azure-snapshot", force=True)
            assert {"app-original", "app-new"} <= names_in(registry.state("azure-snapshot"))
        finally:
            await registry.shutdown()

    @pytest.mark.anyio
    async def test_the_estate_can_shrink(self, disk_registry):
        settings, snapshot = disk_registry
        write_snapshot(snapshot, [f"app-{i}" for i in range(6)])
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        try:
            await registry.load_import("azure-snapshot")
            before = len(registry.state("azure-snapshot").graph)

            write_snapshot(snapshot, ["app-0"])
            await registry.load_import("azure-snapshot", force=True)

            assert len(registry.state("azure-snapshot").graph) < before
        finally:
            await registry.shutdown()

    @pytest.mark.anyio
    async def test_a_fresh_process_sees_the_latest_import(self, disk_registry):
        """The persisted estate is a cache of the last read, and must read as one."""
        settings, snapshot = disk_registry
        first = WorkspaceRegistry(settings)
        await first.startup()
        await first.load_import("azure-snapshot")
        await first.shutdown()

        write_snapshot(snapshot, ["app-second-generation"])
        second = WorkspaceRegistry(settings)
        await second.startup()
        try:
            await second.load_import("azure-snapshot", force=True)
            assert "app-second-generation" in names_in(second.state("azure-snapshot"))
        finally:
            await second.shutdown()


class TestRepositoryReplacement:
    def test_replace_estate_removes_what_is_absent(self, tmp_path: Path):
        from app.fixtures.atlaspay import build_atlaspay

        repo = SqliteRepository(str(tmp_path / "r.db"))
        entities, edges = build_atlaspay()
        repo.save_entities(entities)
        repo.save_edges(edges)
        assert len(repo.load_entities()) == len(entities)

        repo.replace_estate(entities[:3], [])
        assert len(repo.load_entities()) == 3
        assert repo.load_edges() == []
        repo.close()

    def test_replace_estate_accepts_an_empty_estate(self, tmp_path: Path):
        from app.fixtures.atlaspay import build_atlaspay

        repo = SqliteRepository(str(tmp_path / "r.db"))
        entities, edges = build_atlaspay()
        repo.replace_estate(entities, edges)
        repo.replace_estate([], [])
        assert repo.load_entities() == []
        assert repo.load_edges() == []
        repo.close()


# ======================================================================================
# Nothing keeps serving the old world
# ======================================================================================


class TestNothingServesAStaleWorld:
    @pytest.mark.anyio
    async def test_every_rebuild_bumps_the_revision(self, disk_registry):
        settings, _snapshot = disk_registry
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        try:
            await registry.load_import("azure-snapshot")
            first = registry.get("azure-snapshot").revision
            await registry.load_import("azure-snapshot", force=True)
            assert registry.get("azure-snapshot").revision > first
        finally:
            await registry.shutdown()

    def test_the_analyst_is_rebound_when_the_world_is_replaced(self, tmp_path: Path):
        """An analyst is bound to a world; the id survives a re-import and the world does not."""
        from fastapi.testclient import TestClient

        from app.main import create_app

        snapshot = tmp_path / "snapshot.json"
        write_snapshot(snapshot, ["app-original"])
        settings = Settings(
            run_mode=RunMode.OFFLINE,
            database_path=str(tmp_path / "wg.db"),
            azure_snapshot_path=str(snapshot),
        )
        app = create_app(settings)
        with TestClient(app) as client:
            params = {"workspace": "azure-snapshot"}
            client.post("/api/workspaces/azure-snapshot/import")
            # /api/ai/ask is the route that actually depends on get_analyst. Asserting
            # against /api/ai/status would pass whether or not the cache was invalidated,
            # because that route never builds one.
            client.post("/api/ai/ask", params=params, json={"message": "what changed?"})
            first = dict(app.state.analysts)
            assert first, "the analyst cache must be populated for this test to mean anything"

            write_snapshot(snapshot, ["app-renamed"])
            client.post(
                "/api/workspaces/azure-snapshot/import", params={"force": "true"}
            )
            client.post("/api/ai/ask", params=params, json={"message": "what changed?"})

            # The API answers from the new estate...
            world = client.get("/api/world", params=params).json()
            assert any(e["name"] == "app-renamed" for e in world["entities"])
            assert not any(e["name"] == "app-original" for e in world["entities"])

            # ...and no analyst cached against the previous world survived.
            stale = set(first) & set(app.state.analysts)
            assert stale == set(), f"stale analyst kept for {stale}"

    def test_the_analyst_cache_does_not_grow_per_import(self, tmp_path: Path):
        from fastapi.testclient import TestClient

        from app.main import create_app

        snapshot = tmp_path / "snapshot.json"
        write_snapshot(snapshot, ["app"])
        settings = Settings(
            run_mode=RunMode.OFFLINE,
            database_path=str(tmp_path / "wg.db"),
            azure_snapshot_path=str(snapshot),
        )
        app = create_app(settings)
        with TestClient(app) as client:
            params = {"workspace": "azure-snapshot"}
            for _ in range(4):
                client.post(
                    "/api/workspaces/azure-snapshot/import", params={"force": "true"}
                )
                client.post(
                    "/api/ai/ask", params=params, json={"message": "what changed?"}
                )
            azure_keys = [k for k in app.state.analysts if k.startswith("azure-snapshot:")]
            assert len(azure_keys) == 1, azure_keys


class TestTheDemoIsUnaffected:
    @pytest.mark.anyio
    async def test_the_demo_still_prefers_its_persisted_estate(self, tmp_path: Path):
        """Only imports are authoritative; the demo's store stays a cheap restart cache."""
        settings = Settings(
            run_mode=RunMode.OFFLINE, database_path=str(tmp_path / "wg.db")
        )
        registry = WorkspaceRegistry(settings)
        await registry.startup()
        first = len(registry.state("atlaspay-demo").graph)
        await registry.shutdown()

        again = WorkspaceRegistry(settings)
        await again.startup()
        try:
            assert len(again.state("atlaspay-demo").graph) == first
        finally:
            await again.shutdown()
