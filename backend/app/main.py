"""WorldGraph API application.

Composition root: builds the world state, the analyst, the rate limiters and the router,
and wires the lifespan so adapters start and stop cleanly.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import Settings, get_settings
from .models.workspace import ATLASPAY_WORKSPACE_ID
from .observability.logging import RequestLoggingMiddleware, configure_logging
from .security.ratelimit import RateLimiter
from .services.workspaces import WorkspaceRegistry, default_repository_for
from .storage.repository import Repository

logger = logging.getLogger("worldgraph")

API_PREFIX = "/api"


def create_app(
    settings: Settings | None = None, repository: Repository | None = None
) -> FastAPI:
    """Build the application.

    Both dependencies are injectable so tests can run against an in-memory repository and
    a fixed configuration without touching the process environment.
    """
    settings = settings or get_settings()
    configure_logging()

    def repository_factory(workspace_id: str) -> Repository:
        """One store per workspace.

        An injected repository belongs to the default workspace only. Handing the same
        connection to every workspace would put a demo fixture and a real subscription in
        one file, which is the contamination the registry exists to prevent.
        """
        if repository is not None and workspace_id == ATLASPAY_WORKSPACE_ID:
            return repository
        return default_repository_for(settings, workspace_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        workspaces = WorkspaceRegistry(settings, repository_factory)
        await workspaces.startup()
        app.state.workspaces = workspaces
        # The default world, kept on app.state for the many callers that never switch.
        app.state.world = workspaces.state(workspaces.default_id)
        # One analyst per workspace, built on demand: an analyst is bound to a world, and
        # answering a question about one estate with another's tools would be the same
        # contamination bug in a different layer.
        app.state.analysts = {}
        app.state.limiters = {
            "ai": RateLimiter(limit_per_minute=settings.ai_rate_limit_per_minute),
            "analysis": RateLimiter(limit_per_minute=settings.analysis_rate_limit_per_minute),
        }
        logger.info(
            "worldgraph_ready",
            extra={
                "mode": settings.run_mode.value,
                "entities": len(app.state.world.graph),
                "workspaces": len(workspaces.list()),
                "ai_enabled": settings.ai_enabled,
            },
        )
        try:
            yield
        finally:
            await workspaces.shutdown()

    app = FastAPI(
        title="WorldGraph",
        version="1.0.0",
        description=(
            "AI-native cyber-physical resilience twin. Correlates world events against an "
            "organisation's dependency graph to compute blast radius, business impact and "
            "response options. Every workspace states its own provenance: the built-in "
            "AtlasPay estate is synthetic demonstration data, and an imported cloud estate "
            "is read-only inventory WorldGraph has changed nothing in."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, error: Exception) -> JSONResponse:
        """Catch-all that logs the detail and returns a specific, safe message.

        "Something went wrong" is banned by the product requirements. The client is told
        which operation failed and that the rest of the product still works; the stack
        trace goes to the log, not the browser.
        """
        logger.exception("unhandled_error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    f"WorldGraph could not complete {request.method} {request.url.path}. "
                    "The failure has been logged. Infrastructure analysis and simulation "
                    "remain available."
                ),
                "operation": request.url.path,
            },
        )

    app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
