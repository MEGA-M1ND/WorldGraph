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

from .ai.analyst import build_analyst
from .api.routes import router
from .config import Settings, get_settings
from .observability.logging import RequestLoggingMiddleware, configure_logging
from .security.ratelimit import RateLimiter
from .services.world_state import WorldState
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        world = WorldState(settings, repository)
        await world.startup()
        app.state.world = world
        app.state.analyst = build_analyst(world, settings)
        app.state.limiters = {
            "ai": RateLimiter(limit_per_minute=settings.ai_rate_limit_per_minute),
            "analysis": RateLimiter(limit_per_minute=settings.analysis_rate_limit_per_minute),
        }
        logger.info(
            "worldgraph_ready",
            extra={
                "mode": settings.run_mode.value,
                "entities": len(world.graph),
                "ai_enabled": settings.ai_enabled,
            },
        )
        try:
            yield
        finally:
            await world.shutdown()

    app = FastAPI(
        title="WorldGraph",
        version="1.0.0",
        description=(
            "AI-native cyber-physical resilience twin. Correlates world events against an "
            "organisation's dependency graph to compute blast radius, business impact and "
            "response options. The enterprise estate in this deployment (AtlasPay) is "
            "synthetic demonstration data."
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
