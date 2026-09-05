"""Runtime configuration.

Every secret is read here, server-side, from the environment. Nothing in this module is
ever serialized to an API response — :func:`public_config` exists precisely so the
frontend can learn what is *enabled* without learning any credential.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(str, Enum):
    """How WorldGraph sources its world events.

    ``LIVE``   — poll USGS and CISA KEV, with replay fixtures alongside as context.
    ``DEMO``   — replay fixtures only; deterministic and offline-safe. The default,
                 because a presentation must not depend on someone else's uptime.
    ``OFFLINE``— replay fixtures only and no outbound network calls at all.
    """

    LIVE = "LIVE"
    DEMO = "DEMO"
    OFFLINE = "OFFLINE"


class Settings(BaseSettings):
    """Server settings, populated from the environment or a local ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="WORLDGRAPH_", extra="ignore"
    )

    run_mode: RunMode = RunMode.DEMO

    #: CORS origins allowed to call the API. The Vite dev server by default.
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    #: SQLite path. A relative path resolves against the backend working directory.
    database_path: str = "worldgraph.db"

    #: Requests per minute per client IP against the AI endpoints. AI calls cost money and
    #: are the only endpoints where an unbounded caller is expensive rather than just rude.
    ai_rate_limit_per_minute: int = 20
    #: Requests per minute per client IP against analysis endpoints.
    analysis_rate_limit_per_minute: int = 120

    #: Anthropic API key. Absent by default — the analyst degrades to its deterministic
    #: router, which is what makes the demo runnable with no accounts at all.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    #: Ceiling on a single analyst completion.
    ai_max_output_tokens: int = 1400
    ai_timeout_seconds: float = 30.0

    #: Optional Cesium Ion token for higher-quality terrain and imagery. Without it the
    #: globe renders from bundled Natural Earth II imagery, which needs no account.
    cesium_ion_token: str | None = None

    #: Optional Google Maps key for Photorealistic 3D Tiles. Strictly opt-in: it is a paid
    #: product, and requiring it would make the demo unreproducible.
    google_maps_api_key: str | None = None

    #: Azure subscription ids (or names) to offer as importable workspaces. Empty by
    #: default: WorldGraph must run fully with no cloud account at all.
    #:
    #: No credential appears here. Authentication goes through ``DefaultAzureCredential``,
    #: which reads ``az login``, managed identity or the standard environment variables —
    #: WorldGraph never holds, stores or forwards an Azure secret, and nothing in this
    #: section is exposed by :meth:`public_config`.
    azure_subscriptions: list[str] = Field(default_factory=list)

    #: Path to a sanitized Azure inventory snapshot to replay from disk. Lets the Azure
    #: path be developed, tested and demonstrated with no subscription — and the workspace
    #: it produces is labelled REPLAY, never LIVE, because a recording is not a live view.
    azure_snapshot_path: str | None = None

    @property
    def azure_configured(self) -> bool:
        """Whether any Azure workspace is declared. Never says whether it *works*."""
        return bool(self.azure_subscriptions or self.azure_snapshot_path)

    @property
    def ai_enabled(self) -> bool:
        """Whether a model-backed analyst is available at all."""
        return bool(self.anthropic_api_key)

    @property
    def network_enabled(self) -> bool:
        """Whether outbound feed calls are permitted in this mode."""
        return self.run_mode is RunMode.LIVE

    def public_config(self) -> dict[str, object]:
        """The subset of configuration the browser may know.

        Booleans only where a secret is involved. The frontend needs to know *whether* the
        AI analyst is available so it can label its own state honestly; it must never
        learn the key. The two tile tokens are the sole exception and are returned as
        values because Cesium requires them client-side — which is exactly why they are
        optional, are the only client-visible credentials, and are documented in
        SECURITY.md as belonging to referrer-restricted, quota-limited accounts.
        """
        return {
            "run_mode": self.run_mode.value,
            "ai_enabled": self.ai_enabled,
            "ai_model": self.anthropic_model if self.ai_enabled else None,
            "cesium_ion_token": self.cesium_ion_token,
            "google_maps_api_key": self.google_maps_api_key,
            "network_enabled": self.network_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings. Call ``get_settings.cache_clear()`` in tests that vary the env."""
    return Settings()
