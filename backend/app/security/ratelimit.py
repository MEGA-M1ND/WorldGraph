"""Per-client rate limiting.

A fixed-window counter keyed by client IP. Deliberately simple: WorldGraph V1 is a
single-process app with no shared cache, and a token bucket in Redis would be
infrastructure for a limit whose only job is to stop one caller running up an AI bill or
hammering the analysis engines.

Applied to the AI endpoints (they cost money) and the analysis endpoints (they cost CPU).
Read endpoints are unmetered.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class RateLimiter:
    """Fixed-window per-key counter."""

    def __init__(self, *, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit_per_minute))
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int, float]:
        """Record a hit.

        Returns ``(allowed, remaining, retry_after_seconds)``. ``retry_after`` is 0 when
        the request is allowed.
        """
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits[key]
            # Drop expired hits in place — the list is short by construction (bounded by
            # the limit) so this stays cheap and needs no background sweep.
            hits[:] = [stamp for stamp in hits if stamp > cutoff]
            if len(hits) >= self.limit:
                retry_after = max(0.0, hits[0] + self.window - now)
                return False, 0, round(retry_after, 1)
            hits.append(now)
            return True, self.limit - len(hits), 0.0

    def reset(self) -> None:
        """Clear all counters. Used by tests."""
        with self._lock:
            self._hits.clear()
