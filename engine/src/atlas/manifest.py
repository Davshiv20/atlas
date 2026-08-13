"""What a workspace *is*, independent of where it is kept.

Separate from both the storage port and the domain service because both need
it and neither owns it. A manifest names the workspace, binds it immutably to
one source, distinguishes this incarnation from a deleted one that had the same
name, and points at the active snapshot generation.

Nothing here reads or writes. A model that knows how to persist itself is a
model that has picked a store.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

#: A workspace name is used as an identifier by every store — a directory name
#: under YAML, a key under anything else — so it is validated rather than
#: sanitized. Silently rewriting "../etc" to something safe hides what was
#: actually attempted.
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class InvalidWorkspace(ValueError):
    """The name could never address a workspace, in any store."""


def require_valid_name(name: str) -> str:
    if not SAFE_NAME.match(name):
        raise InvalidWorkspace(
            "workspace names must be lowercase alphanumeric with - or _, max 63 chars"
        )
    return name


class WorkspaceManifest(BaseModel):
    schema_version: int = 1
    id: str
    source_id: str
    incarnation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot_generation: int = Field(default=0, ge=0)

    @field_validator("id", "source_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not SAFE_NAME.match(value):
            raise ValueError("must be lowercase alphanumeric with - or _, max 63 chars")
        return value

    @field_validator("incarnation_id")
    @classmethod
    def valid_incarnation(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("incarnation_id must be a 32-character lowercase hex UUID")
        return value

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported workspace manifest schema_version")
        return value
