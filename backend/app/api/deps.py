"""Shared API dependencies.

The world state is a process singleton created at startup — it owns the graph, the poll
loops and the repository connection, so it must not be rebuilt per request. It is stashed
on ``app.state`` rather than in a module global so tests can build an isolated app.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request, status

from ..config import Settings, get_settings
from ..models.workspace import Workspace
from ..security.ratelimit import RateLimiter
from ..services.workspaces import WorkspaceError, WorkspaceRegistry
from ..services.world_state import WorldState


def get_registry(request: Request) -> WorkspaceRegistry:
    """The workspace registry."""
    registry: WorkspaceRegistry | None = getattr(request.app.state, "workspaces", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WorldGraph is still starting up.",
        )
    return registry


def get_workspace(
    request: Request,
    workspace: str | None = Query(
        default=None,
        max_length=64,
        description=(
            "Which estate to answer about. Omitted means the default demo workspace. An "
            "unknown or unloaded id is an error, never a silent fallback."
        ),
    ),
) -> Workspace:
    """The workspace a request is addressed to."""
    registry = get_registry(request)
    try:
        return registry.get(workspace)
    except WorkspaceError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error


def get_state(
    request: Request, selected: Workspace = Depends(get_workspace)
) -> WorldState:
    """The world for the requested workspace.

    A workspace that exists but is not loaded is a 409 with the reason, never a fallback
    to the default. Answering a question about an Azure subscription with data from a demo
    fixture would be the single most damaging bug this product could ship.
    """
    registry = get_registry(request)
    try:
        return registry.state(selected.id)
    except WorkspaceError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error


def get_analyst(request: Request, state: WorldState = Depends(get_state)):
    """The analyst bound to the requested workspace's world.

    Built once per workspace and cached. Each analyst's tools read only its own world, so
    a question asked of one estate cannot be answered from another's graph.
    """
    from ..ai.analyst import build_analyst

    analysts: dict | None = getattr(request.app.state, "analysts", None)
    if analysts is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The analyst is still starting up.",
        )
    key = state.workspace.id
    analyst = analysts.get(key)
    if analyst is None:
        analyst = build_analyst(state, get_settings())
        analysts[key] = analyst
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
