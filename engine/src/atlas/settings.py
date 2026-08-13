"""Configuration, validated at the boundary.

Every knob that was previously a module constant or a bare `os.environ` read
lives here, so a misconfiguration fails at startup with a named field rather
than halfway through a schema with a confusing traceback.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    # --- model -------------------------------------------------------------
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    atlas_model: str = "qwen/qwen3.7-plus"
    atlas_effort: str = "medium"
    atlas_max_turns: int = Field(default=40, ge=1, le=200)

    # How many tables are read at once. Bounded on purpose at both ends: the
    # model gateway rate-limits, and every worker issues checks against a
    # database Atlas does not own. Six is a deliberate compromise, not a
    # measured optimum — raise it once you have watched a real source cope.
    atlas_max_workers: int = Field(default=6, ge=1, le=32)

    # --- sample values -----------------------------------------------------
    # "strict" withholds anything sensitive-looking, opaque, free-text, or
    # high-cardinality. "full" emits every value the profiler collected.
    #
    # Default strict, because the unsafe setting must be chosen rather than
    # inherited: a deployment pointed at a customer database ships whatever
    # this says, and .env is per-machine while the default is per-product.
    atlas_sample_policy: Literal["strict", "full"] = "strict"

    # --- query guards ------------------------------------------------------
    atlas_max_rows: int = Field(default=50, ge=1, le=1000)
    atlas_statement_timeout_ms: int = Field(default=15_000, ge=100, le=600_000)

    # --- Atlas's own record -------------------------------------------------
    # Where the semantic record lives. Unset means the file-backed store under
    # `atlas_output_dir`; set means Atlas-owned PostgreSQL, which is the store
    # that can offer real transactions and hold more than one reviewer.
    #
    # Deliberately not defaulted to a database. An install that has been
    # writing files does not silently start reading an empty schema instead —
    # moving is `atlas migrate-store`, and it is a decision someone makes.
    atlas_database_url: str | None = None

    # --- output ------------------------------------------------------------
    atlas_output_dir: Path = Path("out")

    # Declared sources. Contains env-var *names*, never secrets, so it is safe
    # to commit and safe to serve.
    atlas_sources_file: Path = Path("sources.yaml")

    # Connection strings written from the UI. Plaintext, 0600, gitignored —
    # see secrets.py for why that is not sufficient outside localhost.
    atlas_secrets_file: Path = Path(".secrets.env")

    # The built console, served by this process in a deployment so there is one
    # origin and no CORS. Absent in local development, where Vite serves the
    # console on its own port and proxies /api here.
    atlas_static_dir: Path = Path("static")

    @field_validator("atlas_effort")
    @classmethod
    def known_effort(cls, value: str) -> str:
        allowed = {"low", "medium", "high"}
        if value not in allowed:
            raise ValueError(f"atlas_effort must be one of {sorted(allowed)}")
        return value

    def require_api_key(self) -> str:
        if self.openrouter_api_key is None:
            raise RuntimeError("OPENROUTER_API_KEY is not set (put it in .env or the environment)")
        return self.openrouter_api_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
