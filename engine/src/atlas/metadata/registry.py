"""Which metadata store this process uses.

The mirror of `atlas.adapters.registry`, and deliberately the only place that
names an implementation. Everything else asks for a `MetadataRepository` and is
handed one, which is what makes moving Atlas's record from files to PostgreSQL
a change to configuration rather than to callers.

The choice is `ATLAS_DATABASE_URL`. Unset means files. It is not defaulted to a
database because an install that has been writing files must not silently start
reading an empty schema instead — the data does not move on its own, and a
workspace that reads as absent is one nobody can tell from deleted.
"""

from __future__ import annotations

import threading

from atlas.metadata.base import MetadataRepository
from atlas.metadata.postgres_store import PostgresMetadataRepository
from atlas.metadata.yaml_store import YamlMetadataRepository
from atlas.settings import get_settings

#: One repository per database URL per process, because a SQLAlchemy engine
#: owns a connection pool. Building one per request opens a connection per
#: request and reuses none, which is how a read-heavy console exhausts
#: `max_connections` on an otherwise idle database.
#:
#: The YAML store is deliberately not pooled here — it holds a `Path`, not a
#: resource — so a test that repoints `ATLAS_OUTPUT_DIR` needs no invalidation.
_engines: dict[str, PostgresMetadataRepository] = {}
_engines_lock = threading.Lock()


def get_repository() -> MetadataRepository:
    url = get_settings().atlas_database_url
    if url is None:
        return YamlMetadataRepository()
    with _engines_lock:
        repository = _engines.get(url)
        if repository is None:
            repository = PostgresMetadataRepository(url)
            _engines[url] = repository
        return repository


def reset_repositories() -> None:
    """Close pooled engines and forget them.

    For tests, and for anything that reconfigures the database URL inside a
    live process. Disposing rather than dropping the reference matters: an
    abandoned pool holds its server-side connections until it is collected.
    """
    with _engines_lock:
        for repository in _engines.values():
            repository.dispose()
        _engines.clear()
