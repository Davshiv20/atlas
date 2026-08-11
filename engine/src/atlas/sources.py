"""Declared data sources.

A source names a database without containing its credentials. The connection
string lives in an environment variable; the registry records which one.

That indirection is the whole point. The console can create, list, and test a
source without a secret ever crossing the browser or being written to disk by
the engine — which matters most while there is no authentication, since an
endpoint that accepts a connection string is a server-side request forgery
primitive and stored credentials are readable by anyone who reaches the box.

Whoever runs the engine puts the URL in `.env`; direnv loads it. Adding a
source is therefore two steps, and the second is deliberately in a terminal.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from atlas.settings import get_settings

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


class SourceRegistry(BaseModel):
    sources: list[Source] = Field(default_factory=list)

    def get(self, source_id: str) -> Source:
        found = next((s for s in self.sources if s.id == source_id), None)
        if found is None:
            raise SourceNotFound(source_id)
        return found

    def add(self, source: Source) -> Source:
        if any(s.id == source.id for s in self.sources):
            raise DuplicateSource(f"a source named {source.id!r} already exists")
        self.sources.append(source)
        return source

    def remove(self, source_id: str) -> None:
        self.get(source_id)
        self.sources = [s for s in self.sources if s.id != source_id]

    def write(self, path: Path | None = None) -> None:
        target = path or registry_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        )

    @classmethod
    def read(cls, path: Path | None = None) -> SourceRegistry:
        target = path or registry_path()
        if not target.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(target.read_text()) or {})


@contextmanager
def source_registry_lock() -> Iterator[None]:
    """Serialize source lifecycle with workspace binding across API/CLI processes."""
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def registry_path() -> Path:
    return get_settings().atlas_sources_file
