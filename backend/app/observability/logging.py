"""Structured logging.

One JSON object per line, so logs are greppable and machine-readable without a collector.
Feed refreshes, adapter errors, graph calculations, AI tool usage, simulation runs and API
latency all land here.

**Secrets never enter a log record.** The formatter drops any field whose name looks like a
credential, and adapters are required to produce user-safe error strings rather than raw
upstream bodies — a proxied error body is exactly where a key ends up.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

#: Field names that must never be serialized, matched case-insensitively as substrings.
_SECRET_FIELDS = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|bearer)",
    re.IGNORECASE,
)

#: Value shapes that look like credentials even under an innocent field name.
_SECRET_VALUES = re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,})")

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "taskName",
}


def _redact(value: Any) -> Any:
    """Scrub anything credential-shaped out of a log value."""
    if isinstance(value, str):
        return _SECRET_VALUES.sub("[redacted]", value)
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if _SECRET_FIELDS.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if _SECRET_FIELDS.search(key):
                payload[key] = "[redacted]"
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = str(value)
            payload[key] = _redact(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates what RequestLoggingMiddleware records, with less
    # structure. One source of truth for request logs.
    logging.getLogger("uvicorn.access").disabled = True


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with its latency.

    Only the path template and status are recorded — never the query string or body, both
    of which can carry operator-supplied text.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        logging.getLogger("worldgraph.http").info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response
