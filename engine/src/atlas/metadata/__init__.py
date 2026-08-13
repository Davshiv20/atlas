"""Atlas's own persistence: the port, its implementations, and the registry."""

from atlas.metadata.base import (
    MetadataRepository,
    MetadataRepositoryError,
    NoSnapshot,
    UnknownWorkspace,
    WorkspaceBusy,
    WorkspaceExists,
)
from atlas.metadata.registry import get_repository

__all__ = [
    "MetadataRepository",
    "MetadataRepositoryError",
    "NoSnapshot",
    "UnknownWorkspace",
    "WorkspaceBusy",
    "WorkspaceExists",
    "get_repository",
]
