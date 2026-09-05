"""Shared test fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import RunMode, Settings
from app.fixtures.atlaspay import build_atlaspay
from app.graph.world_graph import WorldGraph
from app.main import create_app
from app.storage.repository import SqliteRepository


@pytest.fixture
def atlaspay_graph() -> WorldGraph:
    """The AtlasPay estate as a graph. Rebuilt per test so mutation cannot leak."""
    entities, edges = build_atlaspay()
    return WorldGraph(entities, edges)


@pytest.fixture
def settings() -> Settings:
    """Offline, deterministic settings with no credentials."""
    return Settings(
        run_mode=RunMode.DEMO,
        database_path=":memory:",
        anthropic_api_key=None,
        cesium_ion_token=None,
        google_maps_api_key=None,
    )


@pytest.fixture
def client(settings: Settings):
    """A TestClient over an app with an in-memory repository."""
    app = create_app(settings, SqliteRepository(":memory:"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def world(settings: Settings):
    """A started WorldState."""
    from app.services.world_state import WorldState

    state = WorldState(settings, SqliteRepository(":memory:"))
    await state.startup()
    try:
        yield state
    finally:
        await state.shutdown()
