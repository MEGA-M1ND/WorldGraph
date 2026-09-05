"""Persistence.

V1 stores entities, edges, events, analyses, scenarios, plans and timeline entries in
SQLite behind an abstract repository. The interface is what matters: swapping in Postgres
later should be a new implementation of :class:`Repository`, not a rewrite of the engines.

Records are stored as validated JSON in a ``payload`` column with the query-relevant
fields promoted to real columns. That is a deliberate V1 trade: the domain model is
Pydantic and evolving, and a fully normalized schema would cost migrations for every field
without buying anything at this scale. The promoted columns cover every query the API
actually makes.

**Neo4j is not used**, per the non-goals: the graph is small enough to hold in memory,
and NetworkX plus relational storage answers every question V1 asks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, TypeVar

from pydantic import BaseModel

from ..models.analysis import (
    BlastRadiusResult,
    ResponsePlan,
    SimulationScenario,
    TimelineEntry,
)
from ..models.core import DependencyEdge, WorldEntity, WorldEvent

T = TypeVar("T", bound=BaseModel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL,
    criticality  TEXT NOT NULL,
    health       TEXT NOT NULL,
    lat          REAL,
    lon          REAL,
    mode         TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

CREATE TABLE IF NOT EXISTS edges (
    id        TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    mode        TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);

CREATE TABLE IF NOT EXISTS analyses (
    id          TEXT PRIMARY KEY,
    origin_kind TEXT NOT NULL,
    severity    TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    event_id    TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analyses_computed ON analyses(computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_event ON analyses(event_id);

CREATE TABLE IF NOT EXISTS scenarios (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id           TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    analysis_id  TEXT,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline (
    id       TEXT PRIMARY KEY,
    at       TEXT NOT NULL,
    stage    TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_id TEXT,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_at ON timeline(at DESC);
"""


class Repository(ABC):
    """Storage interface. Postgres would implement this same surface."""

    @abstractmethod
    def save_entities(self, entities: Iterable[WorldEntity]) -> None: ...

    @abstractmethod
    def load_entities(self) -> list[WorldEntity]: ...

    @abstractmethod
    def save_edges(self, edges: Iterable[DependencyEdge]) -> None: ...

    @abstractmethod
    def load_edges(self) -> list[DependencyEdge]: ...

    @abstractmethod
    def save_events(self, events: Iterable[WorldEvent]) -> None: ...

    @abstractmethod
    def load_events(self, *, limit: int = 200, since: datetime | None = None) -> list[WorldEvent]: ...

    @abstractmethod
    def save_analysis(self, result: BlastRadiusResult, *, event_id: str | None = None) -> None: ...

    @abstractmethod
    def load_analysis(self, analysis_id: str) -> BlastRadiusResult | None: ...

    @abstractmethod
    def recent_analyses(self, *, limit: int = 20) -> list[BlastRadiusResult]: ...

    @abstractmethod
    def save_scenario(self, scenario: SimulationScenario) -> None: ...

    @abstractmethod
    def load_scenario(self, scenario_id: str) -> SimulationScenario | None: ...

    @abstractmethod
    def list_scenarios(self) -> list[SimulationScenario]: ...

    @abstractmethod
    def delete_scenario(self, scenario_id: str) -> None: ...

    @abstractmethod
    def save_plan(self, plan: ResponsePlan, *, analysis_id: str | None = None) -> None: ...

    @abstractmethod
    def load_plan(self, plan_id: str) -> ResponsePlan | None: ...

    @abstractmethod
    def append_timeline(self, entries: Iterable[TimelineEntry]) -> None: ...

    @abstractmethod
    def load_timeline(self, *, limit: int = 100, event_id: str | None = None) -> list[TimelineEntry]: ...


class SqliteRepository(Repository):
    """SQLite-backed repository.

    One connection guarded by a lock. FastAPI runs sync handlers in a threadpool, and
    SQLite connections are not safe to share across threads without care; a single guarded
    connection is simpler and entirely fast enough for a graph this size.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps a reader from blocking the writer; a no-op for :memory:.
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._conn.cursor()
            try:
                yield cursor
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cursor.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- entities ----------------------------------------------------------------------

    def save_entities(self, entities: Iterable[WorldEntity]) -> None:
        rows = [
            (
                e.id,
                e.type.value,
                e.name,
                e.criticality.value,
                e.health.value,
                e.location.lat if e.location else None,
                e.location.lon if e.location else None,
                e.source.mode.value,
                e.updated_at.isoformat(),
                e.model_dump_json(),
            )
            for e in entities
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO entities "
                "(id, type, name, criticality, health, lat, lon, mode, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def load_entities(self) -> list[WorldEntity]:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM entities ORDER BY id")
            return [WorldEntity.model_validate_json(row["payload"]) for row in cursor.fetchall()]

    # -- edges -------------------------------------------------------------------------

    def save_edges(self, edges: Iterable[DependencyEdge]) -> None:
        rows = [
            (e.id, e.source_entity_id, e.target_entity_id, e.type.value, e.model_dump_json())
            for e in edges
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO edges (id, source_id, target_id, type, payload) "
                "VALUES (?,?,?,?,?)",
                rows,
            )

    def load_edges(self) -> list[DependencyEdge]:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM edges ORDER BY id")
            return [
                DependencyEdge.model_validate_json(row["payload"]) for row in cursor.fetchall()
            ]

    # -- events ------------------------------------------------------------------------

    def save_events(self, events: Iterable[WorldEvent]) -> None:
        rows = [
            (
                e.id,
                e.category.value,
                e.severity.value,
                e.occurred_at.isoformat(),
                e.source.source_id,
                e.source.mode.value,
                e.location.lat if e.location else None,
                e.location.lon if e.location else None,
                e.model_dump_json(),
            )
            for e in events
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO events "
                "(id, category, severity, occurred_at, source_id, mode, lat, lon, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def load_events(
        self, *, limit: int = 200, since: datetime | None = None
    ) -> list[WorldEvent]:
        query = "SELECT payload FROM events"
        params: list[object] = []
        if since is not None:
            query += " WHERE occurred_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(int(limit))
        with self._cursor() as cursor:
            cursor.execute(query, params)
            return [WorldEvent.model_validate_json(row["payload"]) for row in cursor.fetchall()]

    # -- analyses ----------------------------------------------------------------------

    def save_analysis(self, result: BlastRadiusResult, *, event_id: str | None = None) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO analyses "
                "(id, origin_kind, severity, computed_at, event_id, payload) VALUES (?,?,?,?,?,?)",
                (
                    result.id,
                    result.origin_kind,
                    result.severity.value,
                    result.computed_at.isoformat(),
                    event_id,
                    result.model_dump_json(),
                ),
            )

    def load_analysis(self, analysis_id: str) -> BlastRadiusResult | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM analyses WHERE id = ?", (analysis_id,))
            row = cursor.fetchone()
        return BlastRadiusResult.model_validate_json(row["payload"]) if row else None

    def recent_analyses(self, *, limit: int = 20) -> list[BlastRadiusResult]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM analyses ORDER BY computed_at DESC LIMIT ?", (int(limit),)
            )
            return [
                BlastRadiusResult.model_validate_json(row["payload"]) for row in cursor.fetchall()
            ]

    # -- scenarios ---------------------------------------------------------------------

    def save_scenario(self, scenario: SimulationScenario) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO scenarios (id, name, updated_at, payload) VALUES (?,?,?,?)",
                (
                    scenario.id,
                    scenario.name,
                    scenario.updated_at.isoformat(),
                    scenario.model_dump_json(),
                ),
            )

    def load_scenario(self, scenario_id: str) -> SimulationScenario | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM scenarios WHERE id = ?", (scenario_id,))
            row = cursor.fetchone()
        return SimulationScenario.model_validate_json(row["payload"]) if row else None

    def list_scenarios(self) -> list[SimulationScenario]:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM scenarios ORDER BY updated_at DESC")
            return [
                SimulationScenario.model_validate_json(row["payload"])
                for row in cursor.fetchall()
            ]

    def delete_scenario(self, scenario_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))

    # -- plans -------------------------------------------------------------------------

    def save_plan(self, plan: ResponsePlan, *, analysis_id: str | None = None) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO plans (id, generated_at, analysis_id, payload) "
                "VALUES (?,?,?,?)",
                (plan.id, plan.generated_at.isoformat(), analysis_id, plan.model_dump_json()),
            )

    def load_plan(self, plan_id: str) -> ResponsePlan | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM plans WHERE id = ?", (plan_id,))
            row = cursor.fetchone()
        return ResponsePlan.model_validate_json(row["payload"]) if row else None

    # -- timeline ----------------------------------------------------------------------

    def append_timeline(self, entries: Iterable[TimelineEntry]) -> None:
        rows = [
            (
                entry.id,
                entry.at.isoformat(),
                entry.stage,
                entry.severity.value,
                entry.event_id,
                entry.model_dump_json(),
            )
            for entry in entries
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO timeline (id, at, stage, severity, event_id, payload) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )

    def load_timeline(
        self, *, limit: int = 100, event_id: str | None = None
    ) -> list[TimelineEntry]:
        query = "SELECT payload FROM timeline"
        params: list[object] = []
        if event_id is not None:
            query += " WHERE event_id = ?"
            params.append(event_id)
        query += " ORDER BY at DESC LIMIT ?"
        params.append(int(limit))
        with self._cursor() as cursor:
            cursor.execute(query, params)
            return [
                TimelineEntry.model_validate_json(row["payload"]) for row in cursor.fetchall()
            ]


def dumps(model: BaseModel) -> str:
    """Stable JSON for a model. Kept here so storage owns its own serialization."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
