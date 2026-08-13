"""Which source store this process uses.

The same switch as the record and the job log: `ATLAS_DATABASE_URL`. Sources
are not split from the workspaces that bind to them, because declaring a
workspace and deleting a source are one decision seen from two sides, and one
lock has to cover both.
"""

from __future__ import annotations

import threading

from atlas.settings import get_settings
from atlas.sources.base import SourceRepository
from atlas.sources.postgres_store import PostgresSourceRepository
from atlas.sources.yaml_store import YamlSourceRepository

_repositories: dict[str, PostgresSourceRepository] = {}
_lock = threading.Lock()


def get_source_repository() -> SourceRepository:
    url = get_settings().atlas_database_url
    if url is None:
        return YamlSourceRepository()
    with _lock:
        repository = _repositories.get(url)
        if repository is None:
            repository = PostgresSourceRepository(url)
            _repositories[url] = repository
        return repository


def reset_source_repositories() -> None:
    """Close pooled engines and forget them. For tests, and for anything that
    reconfigures the database URL inside a live process."""
    with _lock:
        for repository in _repositories.values():
            repository.dispose()
        _repositories.clear()
