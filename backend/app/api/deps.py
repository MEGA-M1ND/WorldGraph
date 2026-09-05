"""Shared API dependencies.

The world state is a process singleton created at startup — it owns the graph, the poll
loops and the repository connection, so it must not be rebuilt per request. It is stashed
on ``app.state`` rather than in a module global so tests can build an isolated app.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from ..config import Settings, get_settings
from ..security.ratelimit import RateLimiter
from ..services.world_state import WorldState


def get_state(request: Request) -> WorldState:
    """The process-wide world state."""
    state: WorldState | None = getattr(request.app.state, "world", None)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WorldGraph is still starting up.",
        )
    return state


def get_analyst(request: Request):
    """The configured analyst backend."""
    analyst = getattr(request.app.state, "analyst", None)
    if analyst is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The analyst is still starting up.",
        )
    return analyst


def client_key(request: Request) -> str:
    """Rate-limit key for a request.

    Uses the socket peer, not ``X-Forwarded-For``: a header a caller controls is not an
    identity, and trusting it would let anyone bypass the limit by rotating a string.
    Deployments behind a proxy should configure the ASGI server's trusted-proxy handling
    so ``request.client`` is the real peer.
    """
    return request.client.host if request.client else "unknown"


def rate_limit(kind: str):
    """Dependency factory enforcing one of the configured limits."""

    def dependency(request: Request, settings: Settings = Depends(get_settings)) -> None:
        limiters: dict[str, RateLimiter] | None = getattr(request.app.state, "limiters", None)
        if limiters is None:
            return
        limiter = limiters.get(kind)
        if limiter is None:
            return
        allowed, remaining, retry_after = limiter.check(client_key(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit reached for {kind} requests. "
                    f"Try again in {retry_after:.0f}s."
                ),
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        request.state.rate_limit_remaining = remaining

    return dependency
