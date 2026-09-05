"""The WorldDataAdapter contract.

Every external signal enters WorldGraph through an adapter with the same lifecycle and the
same honest status reporting. The shape is modelled on God's Eye View's ``DataLayerManager``
(MIT) — the insight worth keeping is that a feed has exactly *one* state at a time, that
guidance states are not faults, and that a feed which is serving cached data must say so
rather than looking healthy.

An adapter is responsible for:

* talking to exactly one upstream, with a timeout and bounded retries;
* normalizing what comes back into :class:`WorldEvent` / :class:`WorldEntity`;
* sanitizing every string it did not author;
* reporting its own state truthfully, including when it is serving stale data.

An adapter is **not** responsible for correlation, scoring or storage. It hands over
normalized records and stops.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import httpx

from ..models.core import (
    DataMode,
    FeedState,
    FeedStatus,
    WorldEntity,
    WorldEvent,
    utcnow,
)

logger = logging.getLogger("worldgraph.adapters")

#: Default per-request timeout. Explicit because a hung feed must not hang the API.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: Retries on a failed fetch, with exponential backoff plus jitter.
DEFAULT_MAX_RETRIES = 3

#: Cap on a response body. A feed that returns more than this is malfunctioning or
#: hostile; either way we do not want it in memory.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AdapterError(RuntimeError):
    """An adapter could not produce data. Carries a message safe to show a user."""


class WorldDataAdapter(ABC):
    """Base class for every data source."""

    #: Stable machine id, used in URLs and status payloads.
    id: str = "adapter"
    #: Human name shown in the UI's feed list.
    name: str = "Adapter"
    #: Where this data comes from, for the provenance panel.
    source_url: str | None = None
    #: What kind of data this adapter produces.
    mode: DataMode = DataMode.LIVE
    #: How often the scheduler should call :meth:`refresh`, in seconds.
    refresh_interval_seconds: float = 300.0
    #: Age at which held data is reported STALE rather than LIVE.
    stale_after_seconds: float = 900.0

    def __init__(self) -> None:
        self._state: FeedState = FeedState.LOADING
        self._message: str | None = None
        self._last_success: datetime | None = None
        self._last_attempt: datetime | None = None
        self._events: list[WorldEvent] = []
        self._entities: list[WorldEntity] = []
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------------------

    async def initialize(self) -> None:
        """One-time setup. Override to load bundled fixtures or validate configuration."""
        self._state = FeedState.LOADING

    async def start(self) -> None:
        """Begin periodic refresh.

        Idempotent: starting a running adapter is a no-op rather than a second poll loop.
        """
        if self._running:
            return
        self._running = True
        self._started = True
        await self.refresh()
        if self.refresh_interval_seconds > 0:
            self._task = asyncio.create_task(self._poll_loop(), name=f"feed:{self.id}")

    async def stop(self) -> None:
        """Stop refreshing and release the poll task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown path
                pass
            self._task = None

    async def refresh(self) -> None:
        """Fetch once and update held records and state.

        Failure never clears previously fetched data: an adapter that loses its upstream
        keeps serving what it has and reports STALE or DEGRADED. Dropping the data would
        make the globe blink empty every time a feed hiccups.
        """
        self._last_attempt = utcnow()
        try:
            events, entities = await self.fetch()
        except Exception as error:  # noqa: BLE001 — adapters must not crash the app
            message = self._describe_error(error)
            logger.warning("feed_refresh_failed adapter=%s reason=%s", self.id, message)
            if self._events or self._entities:
                self._state = FeedState.DEGRADED
            else:
                self._state = FeedState.UNAVAILABLE
            self._message = message
            return

        self._events = events
        self._entities = entities
        self._last_success = utcnow()
        self._message = None
        self._state = FeedState.SIMULATED if self.mode is not DataMode.LIVE else FeedState.LIVE

    async def _poll_loop(self) -> None:
        """Periodic refresh until stopped."""
        try:
            while self._running:
                await asyncio.sleep(self.refresh_interval_seconds)
                if not self._running:
                    return
                await self.refresh()
        except asyncio.CancelledError:
            raise

    # -- data ---------------------------------------------------------------------------

    @abstractmethod
    async def fetch(self) -> tuple[list[WorldEvent], list[WorldEntity]]:
        """Retrieve and normalize upstream data. Raise :class:`AdapterError` on failure."""

    def get_events(self) -> list[WorldEvent]:
        """Events currently held by this adapter."""
        return list(self._events)

    def get_entities(self) -> list[WorldEntity]:
        """Entities currently held by this adapter."""
        return list(self._entities)

    # -- status -------------------------------------------------------------------------

    def get_status(self) -> FeedStatus:
        """The adapter's one honest state.

        The stale check happens here rather than in ``refresh`` so that time passing is
        enough to move a feed from LIVE to STALE — a feed does not become stale only when
        somebody asks it to refresh.
        """
        state = self._state
        if state is FeedState.LIVE and self._last_success is not None:
            age = (utcnow() - self._last_success).total_seconds()
            if age > self.stale_after_seconds:
                state = FeedState.STALE
        return FeedStatus(
            adapter_id=self.id,
            adapter_name=self.name,
            state=state,
            mode=self.mode,
            record_count=len(self._events) + len(self._entities),
            last_success_at=self._last_success,
            last_attempt_at=self._last_attempt,
            message=self._message,
            source_url=self.source_url,
        )

    def mark_fallback(self, message: str) -> None:
        """Declare that this adapter is serving fallback rather than upstream data."""
        self._state = FeedState.FALLBACK
        self._message = message

    # -- helpers ------------------------------------------------------------------------

    async def http_get_json(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        headers: dict[str, str] | None = None,
    ) -> object:
        """GET JSON with a timeout, bounded retries and exponential backoff + jitter.

        Jitter matters: without it, several adapters that failed together retry together,
        and a struggling upstream gets a synchronized thundering herd.
        """
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    response = await client.get(
                        url, headers={"Accept": "application/json", **(headers or {})}
                    )
                    response.raise_for_status()
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise AdapterError(
                            f"{self.name} returned an oversized response "
                            f"({len(response.content) / 1_048_576:.1f} MB)"
                        )
                    return response.json()
            except Exception as error:  # noqa: BLE001 — retried below
                last_error = error
                if attempt == max_retries - 1:
                    break
                backoff = (2.0**attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(backoff)
        raise AdapterError(self._describe_error(last_error))

    def _describe_error(self, error: Exception | None) -> str:
        """A message safe to show a user and safe to log.

        Deliberately narrow: upstream bodies can contain anything, and a request URL can
        contain a key. Only the exception *type* and a fixed explanation escape.
        """
        if error is None:
            return f"{self.name} request failed"
        if isinstance(error, AdapterError):
            return str(error)
        if isinstance(error, httpx.TimeoutException):
            return f"{self.name} timed out after {DEFAULT_TIMEOUT_SECONDS:.0f}s"
        if isinstance(error, httpx.HTTPStatusError):
            return f"{self.name} returned HTTP {error.response.status_code}"
        if isinstance(error, httpx.HTTPError):
            return f"{self.name} is unreachable"
        if isinstance(error, ValueError):
            return f"{self.name} returned a malformed response"
        return f"{self.name} request failed ({type(error).__name__})"

    def freshness_label(self, *, now: datetime | None = None) -> str:
        """Human freshness for the provenance panel, e.g. ``4 minutes``."""
        if self._last_success is None:
            return "never"
        delta: timedelta = (now or utcnow()) - self._last_success
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds} seconds"
        if seconds < 3600:
            return f"{seconds // 60} minutes"
        if seconds < 86400:
            return f"{seconds // 3600} hours"
        return f"{seconds // 86400} days"
