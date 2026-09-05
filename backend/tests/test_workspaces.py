"""Workspace isolation, and the performance budget on an imported estate.

The isolation claim is structural, not a filter: each workspace owns a separate
``WorldState`` with its own graph and its own repository. These tests exist to prove that
claim in the only way that matters — by looking for leakage in both directions between a
demo fixture and an imported estate, at every layer that could carry it.

A blast radius that crossed from AtlasPay into a real Azure subscription (or the reverse)
would be the single most damaging bug this product could ship.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.analysis.blast_radius import calculate_blast_radius
from app.config import RunMode, Settings
from app.graph.world_graph import WorldGraph
from app.main import create_app
from app.models.core import DataMode
from app.models.workspace import ATLASPAY_WORKSPACE_ID, WorkspaceKind, WorkspaceStatus
from app.services.workspaces import WorkspaceError, WorkspaceRegistry
from app.storage.repository import SqliteRepository

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "azure_snapshot.json"


@pytest.fixture
def multi_settings() -> Settings:
    """A deployment with both the demo workspace and a snapshot-backed Azure workspace."""
    return Settings(
        run_mode=RunMode.OFFLINE,
        database_path=":memory:",
        anthropic_api_key=None,
        cesium_ion_token=None,
        google_maps_api_key=None,
        azure_snapshot_path=str(SNAPSHOT_PATH),
    )


@pytest.fixture
def multi_client(multi_settings: Settings):
    app = create_app(multi_settings, SqliteRepository(":memory:"))
    with TestClient(app) as client:
        yield client


@pytest.fixture
async def registry(multi_settings: Settings):
    reg = WorkspaceRegistry(multi_settings, lambda _id: SqliteRepository(":memory:"))
    await reg.startup()
    try:
        yield reg
    finally:
        await reg.shutdown()


# ======================================================================================
# Registration
# ======================================================================================


class TestRegistration:
    @pytest.mark.anyio
    async def test_the_demo_always_loads(self, registry):
        state = registry.state(ATLASPAY_WORKSPACE_ID)
        assert len(state.graph) > 0
        # WorldGraph must open to something meaningful with no configuration at all.
        assert registry.default_id == ATLASPAY_WORKSPACE_ID

    @pytest.mark.anyio
    async def test_configured_workspaces_are_listed_before_they_load(self, registry):
        ids = [w.id for w in registry.list()]
        assert ATLASPAY_WORKSPACE_ID in ids
        assert "azure-snapshot" in ids
        assert registry.get("azure-snapshot").status is WorkspaceStatus.NOT_LOADED

    @pytest.mark.anyio
    async def test_the_demo_leads_the_list(self, registry):
        # It is the only workspace guaranteed to work; a first-time operator should land
        # somewhere that does.
        assert registry.list()[0].kind is WorkspaceKind.DEMO

    @pytest.mark.anyio
    async def test_an_unknown_workspace_is_an_error_not_a_default(self, registry):
        with pytest.raises(WorkspaceError):
            registry.get("azure-does-not-exist")
        with pytest.raises(WorkspaceError):
            registry.state("azure-does-not-exist")

    @pytest.mark.anyio
    async def test_an_unloaded_workspace_raises_rather_than_falling_back(self, registry):
        # The critical case. A silent fallback would answer a question about an Azure
        # subscription with data from a demo fixture.
        with pytest.raises(WorkspaceError) as error:
            registry.state("azure-snapshot")
        assert "not loaded" in str(error.value)

    @pytest.mark.anyio
    async def test_a_demo_workspace_cannot_be_imported(self, registry):
        with pytest.raises(WorkspaceError) as error:
            await registry.load_import(ATLASPAY_WORKSPACE_ID)
        assert "built-in demo" in str(error.value)


# ======================================================================================
# Isolation
# ======================================================================================


class TestIsolation:
    @pytest.mark.anyio
    async def test_the_two_estates_share_no_entity(self, registry):
        await registry.load_import("azure-snapshot")
        demo = {e.id for e in registry.state(ATLASPAY_WORKSPACE_ID).graph.entities}
        azure = {e.id for e in registry.state("azure-snapshot").graph.entities}
        assert demo and azure
        assert demo.isdisjoint(azure)

    @pytest.mark.anyio
    async def test_no_atlaspay_entity_appears_in_the_imported_estate(self, registry):
        await registry.load_import("azure-snapshot")
        names = {e.name.lower() for e in registry.state("azure-snapshot").graph.entities}
        for demo_name in ("payments-api", "atlaspay", "ledger", "fraud"):
            assert not any(demo_name in name for name in names)

    @pytest.mark.anyio
    async def test_no_imported_entity_appears_in_the_demo(self, registry):
        await registry.load_import("azure-snapshot")
        demo_graph = registry.state(ATLASPAY_WORKSPACE_ID).graph
        assert not any(e.id.startswith("az.") for e in demo_graph.entities)

    @pytest.mark.anyio
    async def test_the_states_are_distinct_objects_with_distinct_repositories(self, registry):
        await registry.load_import("azure-snapshot")
        demo = registry.state(ATLASPAY_WORKSPACE_ID)
        azure = registry.state("azure-snapshot")
        assert demo is not azure
        assert demo.repository is not azure.repository

    @pytest.mark.anyio
    async def test_blast_radius_cannot_cross_workspaces(self, registry):
        await registry.load_import("azure-snapshot")
        azure_graph = registry.state("azure-snapshot").graph
        origin = next(e for e in azure_graph.entities if e.id == "az.region.westeurope")
        result = calculate_blast_radius(
            azure_graph, origin_ids=[origin.id], mode=DataMode.REPLAY
        )
        reached = {
            impacted.entity_id
            for impacted in result.direct_impact + result.indirect_impact
        }
        assert reached
        assert all(entity_id.startswith("az.") for entity_id in reached)

    @pytest.mark.anyio
    async def test_a_simulation_in_one_workspace_does_not_touch_the_other(self, registry):
        from app.simulation.engine import new_scenario

        await registry.load_import("azure-snapshot")
        azure = registry.state("azure-snapshot")
        demo = registry.state(ATLASPAY_WORKSPACE_ID)
        scenario = azure.put_scenario(new_scenario("isolation probe"))
        # put_scenario persists, so this exercises the repositories too: the demo store
        # must not be able to return a scenario saved against the Azure workspace.
        assert azure.scenario(scenario.id) is not None
        assert demo.scenario(scenario.id) is None
        assert demo.scenarios() == []

    @pytest.mark.anyio
    async def test_reimport_replaces_rather_than_merges(self, registry):
        first = await registry.load_import("azure-snapshot")
        before = len(registry.state("azure-snapshot").graph)
        second = await registry.load_import("azure-snapshot", force=True)
        # A re-import must not leave orphans from a previous run pretending to exist.
        assert len(registry.state("azure-snapshot").graph) == before
        assert first.entities_created == second.entities_created

    @pytest.mark.anyio
    async def test_an_import_failure_leaves_the_workspace_unavailable_with_a_reason(
        self, multi_settings
    ):
        broken = Settings(
            run_mode=RunMode.OFFLINE,
            database_path=":memory:",
            azure_snapshot_path="/nonexistent/snapshot.json",
        )
        reg = WorkspaceRegistry(broken, lambda _id: SqliteRepository(":memory:"))
        await reg.startup()
        try:
            with pytest.raises(WorkspaceError):
                await reg.load_import("azure-snapshot")
            workspace = reg.get("azure-snapshot")
            # Still listed, with a specific reason. A configuration problem must not look
            # like an empty estate.
            assert workspace.status is WorkspaceStatus.UNAVAILABLE
            assert workspace.message
            assert "not found" in workspace.message
        finally:
            await reg.shutdown()

    @pytest.mark.anyio
    async def test_a_failure_reason_carries_no_internal_detail(self, multi_settings):
        broken = Settings(
            run_mode=RunMode.OFFLINE,
            database_path=":memory:",
            azure_snapshot_path="/nonexistent/snapshot.json",
        )
        reg = WorkspaceRegistry(broken, lambda _id: SqliteRepository(":memory:"))
        await reg.startup()
        try:
            with pytest.raises(WorkspaceError) as error:
                await reg.load_import("azure-snapshot")
            assert "Traceback" not in str(error.value)
        finally:
            await reg.shutdown()


# ======================================================================================
# API
# ======================================================================================


class TestWorkspaceApi:
    def test_workspaces_are_listed(self, multi_client):
        payload = multi_client.get("/api/workspaces").json()
        ids = [w["id"] for w in payload["workspaces"]]
        assert payload["default_id"] == ATLASPAY_WORKSPACE_ID
        assert ids[0] == ATLASPAY_WORKSPACE_ID
        assert "azure-snapshot" in ids

    def test_every_workspace_declares_itself_read_only(self, multi_client):
        for workspace in multi_client.get("/api/workspaces").json()["workspaces"]:
            assert workspace["read_only"] is True

    def test_the_default_workspace_answers_when_none_is_named(self, multi_client):
        payload = multi_client.get("/api/dashboard").json()
        assert payload["workspace_id"] == ATLASPAY_WORKSPACE_ID
        assert payload["organization"] == "AtlasPay"

    def test_an_unknown_workspace_is_404_not_a_fallback(self, multi_client):
        response = multi_client.get("/api/world", params={"workspace": "nope"})
        assert response.status_code == 404
        assert "nope" in response.json()["detail"]

    def test_an_unloaded_workspace_is_409_with_a_reason(self, multi_client):
        response = multi_client.get("/api/world", params={"workspace": "azure-snapshot"})
        # Not a silent fallback to the demo estate, and not a 500 either: the request was
        # well-formed and the reason is actionable.
        assert response.status_code == 409
        assert "not loaded" in response.json()["detail"]

    def test_import_then_query_returns_the_imported_estate(self, multi_client):
        summary = multi_client.post("/api/workspaces/azure-snapshot/import").json()
        assert summary["resources_discovered"] == 9
        assert summary["mode"] == "REPLAY"

        world = multi_client.get("/api/world", params={"workspace": "azure-snapshot"}).json()
        ids = {e["id"] for e in world["entities"]}
        assert all(entity_id.startswith("az.") for entity_id in ids)

        demo = multi_client.get("/api/world").json()
        assert not any(e["id"].startswith("az.") for e in demo["entities"])

    def test_the_dashboard_names_the_workspace_it_answered_for(self, multi_client):
        multi_client.post("/api/workspaces/azure-snapshot/import")
        payload = multi_client.get(
            "/api/dashboard", params={"workspace": "azure-snapshot"}
        ).json()
        assert payload["workspace_id"] == "azure-snapshot"
        assert payload["read_only"] is True
        assert "AtlasPay" not in json.dumps(payload)

    def test_current_workspace_carries_its_disclaimer_and_coverage(self, multi_client):
        multi_client.post("/api/workspaces/azure-snapshot/import")
        payload = multi_client.get(
            "/api/workspaces/current", params={"workspace": "azure-snapshot"}
        ).json()
        assert payload["loaded"] is True
        assert "changed nothing" in payload["disclaimer"]
        coverage = payload["import_summary"]["coverage"]
        assert coverage and all(c["level"] in {"HIGH", "PARTIAL", "LOW", "NONE"} for c in coverage)

    def test_importing_the_demo_workspace_is_refused(self, multi_client):
        response = multi_client.post(f"/api/workspaces/{ATLASPAY_WORKSPACE_ID}/import")
        assert response.status_code == 409

    def test_importing_an_unknown_workspace_is_refused(self, multi_client):
        assert multi_client.post("/api/workspaces/nope/import").status_code == 409

    def test_analysis_runs_against_the_named_workspace(self, multi_client):
        multi_client.post("/api/workspaces/azure-snapshot/import")
        response = multi_client.post(
            "/api/analysis/blast-radius",
            params={"workspace": "azure-snapshot"},
            json={"entity_ids": ["az.region.westeurope"]},
        )
        assert response.status_code == 200
        result = response.json()
        impacted = result["direct_impact"] + result["indirect_impact"]
        assert impacted
        assert all(i["entity_id"].startswith("az.") for i in impacted)

    def test_no_azure_configuration_reaches_the_client_config(self, multi_client):
        payload = multi_client.get("/api/config").json()
        assert "azure" not in json.dumps(payload).lower()

    def test_a_workspace_id_cannot_be_used_to_read_the_filesystem(self, multi_client):
        for hostile in ("../../etc/passwd", "..", "%2e%2e%2f"):
            response = multi_client.get("/api/world", params={"workspace": hostile})
            assert response.status_code in {404, 422}


# ======================================================================================
# Performance on an imported estate
# ======================================================================================


def _synthetic_azure_rows(count: int) -> list[dict]:
    """A plausible imported estate of ``count`` resources across four regions."""
    regions = ["westeurope", "northeurope", "eastus", "southeastasia"]
    rows: list[dict] = []
    for index in range(count):
        region = regions[index % len(regions)]
        rows.append(
            {
                "id": (
                    f"/subscriptions/00000000-0000-0000-0000-000000000009"
                    f"/resourceGroups/rg-{index % 40}/providers/Microsoft.Web/sites/app-{index}"
                ),
                "name": f"app-{index}",
                "type": "microsoft.web/sites",
                "location": region,
                "resourceGroup": f"rg-{index % 40}",
                "subscriptionId": "00000000-0000-0000-0000-000000000009",
                "tags": {},
                "properties": {
                    "state": "Running",
                    # Every resource but the first in its region references its neighbour,
                    # which is the realistic shape: a long reference chain, not a star.
                    "serverFarmId": (
                        f"/subscriptions/00000000-0000-0000-0000-000000000009"
                        f"/resourceGroups/rg-{(index - 4) % 40}/providers/Microsoft.Web/"
                        f"sites/app-{index - 4}"
                    )
                    if index >= 4
                    else "",
                },
            }
        )
    return rows


async def _import_synthetic(count: int, tmp_path: Path):
    from app.adapters.azure_inventory import build_snapshot, import_azure_workspace
    from app.models.workspace import InventorySourceKind, Workspace

    snapshot = tmp_path / f"estate-{count}.json"
    snapshot.write_text(json.dumps(build_snapshot(_synthetic_azure_rows(count))))
    settings = Settings(database_path=":memory:", azure_snapshot_path=str(snapshot))
    workspace = Workspace(
        id="azure-perf",
        name="Azure — Perf",
        kind=WorkspaceKind.REAL,
        source=InventorySourceKind.SNAPSHOT,
        mode=DataMode.REPLAY,
        organization="perf",
    )
    return await import_azure_workspace(settings, workspace)


class TestPerformance:
    """Budgets, not benchmarks. They exist to catch an accidental quadratic."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("count", [100, 1000])
    async def test_import_scales(self, count, tmp_path):
        started = time.perf_counter()
        entities, edges, summary = await _import_synthetic(count, tmp_path)
        elapsed = time.perf_counter() - started
        assert summary.resources_supported == count
        assert len(entities) == count + 4  # four regions
        assert edges
        assert elapsed < 15.0, f"{count} resources took {elapsed:.1f}s"

    @pytest.mark.anyio
    async def test_five_thousand_resources_import_within_budget(self, tmp_path):
        started = time.perf_counter()
        _entities, _edges, summary = await _import_synthetic(5000, tmp_path)
        elapsed = time.perf_counter() - started
        assert summary.resources_supported == 5000
        assert elapsed < 60.0, f"5000 resources took {elapsed:.1f}s"

    @pytest.mark.anyio
    async def test_blast_radius_on_a_large_imported_estate_stays_interactive(self, tmp_path):
        entities, edges, _summary = await _import_synthetic(1000, tmp_path)
        graph = WorldGraph(entities, edges)
        started = time.perf_counter()
        result = calculate_blast_radius(
            graph, origin_ids=["az.region.westeurope"], mode=DataMode.REPLAY
        )
        elapsed = time.perf_counter() - started
        assert result.total_impacted > 0
        # The README's interactive budget. Traversal is bounded by depth and node budget,
        # and a breach here means one of those bounds stopped applying.
        assert elapsed < 5.0, f"blast radius took {elapsed:.1f}s"

    @pytest.mark.anyio
    async def test_truncation_is_visible_when_a_bound_is_hit(self, tmp_path):
        entities, edges, _summary = await _import_synthetic(1000, tmp_path)
        graph = WorldGraph(entities, edges)
        result = calculate_blast_radius(
            graph, origin_ids=["az.region.westeurope"], max_depth=2, mode=DataMode.REPLAY
        )
        # If traversal stopped early the caller must be able to say so. A silently partial
        # blast radius reads as a complete one.
        if result.truncated:
            assert result.truncation_reason
