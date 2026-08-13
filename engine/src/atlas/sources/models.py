"""What a declared source is, independent of where the declaration is kept.

A source names a database without containing its credentials. The connection
string lives in an environment variable; a source records which one.

That indirection is the whole point. The console can create, list, and test a
source without a secret ever crossing the browser or being written to disk by
the engine — which matters most while there is no authentication, since an
endpoint that accepts a connection string is a server-side request forgery
primitive and stored credentials are readable by anyone who reaches the box.

Nothing here reads or writes. A model that knows how to persist itself has
already picked a store.
"""

from __future__ import annotations

import os
import re

from pydantic import BaseModel, field_validator

SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_VAR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# What `create_adapter` can actually build. Keep this list aligned with the
# registry so the console cannot save a source that the engine cannot open.
SUPPORTED_ADAPTERS = ("postgresql", "snowflake")


class SourceNotFound(KeyError):
    pass


class DuplicateSource(ValueError):
    pass


class Source(BaseModel):
    id: str
    adapter: str
    # The *name* of the variable, never its value. Nothing in this file is a
    # secret, so it is safe to commit and safe to serve over an unauthenticated
    # API.
    url_env: str
    namespace: str = "public"
    label: str | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not SOURCE_ID.match(value):
            raise ValueError("id must be lowercase alphanumeric with - or _, max 63 chars")
        return value

    @field_validator("url_env")
    @classmethod
    def valid_env_name(cls, value: str) -> str:
        if not ENV_VAR.match(value):
            raise ValueError("url_env must look like an environment variable name, e.g. ELARA_DATABASE_URL")
        return value

    @field_validator("adapter")
    @classmethod
    def known_adapter(cls, value: str) -> str:
        if value not in SUPPORTED_ADAPTERS:
            raise ValueError(f"adapter must be one of {list(SUPPORTED_ADAPTERS)}")
        return value

    @property
    def configured(self) -> bool:
        """Whether the environment actually holds a URL for this source."""
        return bool(os.environ.get(self.url_env))

    def resolve_url(self) -> str:
        url = os.environ.get(self.url_env)
        if not url:
            raise RuntimeError(
                f"No connection string for {self.id!r}. Open it on the Connections "
                f"screen and paste one, or set {self.url_env} in engine/.env."
            )
        return url
