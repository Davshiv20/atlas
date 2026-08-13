"""Declared sources in Atlas's own PostgreSQL.

Still no credentials. A row holds the *name* of the environment variable a
connection string is read from, so this table stays as safe to read as the file
it replaces — a reader who dumps it learns which databases exist and nothing
that would let them connect.

`lock` is a session advisory lock rather than a file one, which is what makes
the check-then-write across this store and the metadata store hold when there
is more than one engine process.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text

from atlas.sources.base import SourceRepository
from atlas.sources.models import DuplicateSource, Source, SourceNotFound

#: One key for the whole registry, derived the same way the metadata store
#: derives its per-workspace keys and deliberately distinct from any of them.
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"atlas.sources").digest()[:8], "big", signed=True
)

_COLUMNS = "id, adapter, url_env, namespace, label"


class PostgresSourceRepository(SourceRepository):
    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        self._engine = engine or create_engine(url, pool_pre_ping=True, future=True)
        self._state = threading.local()

    def __repr__(self) -> str:
        return f"PostgresSourceRepository({self._engine.url.render_as_string()!r})"

    def dispose(self) -> None:
        self._engine.dispose()

    def list(self) -> list[Source]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT {_COLUMNS} FROM sources ORDER BY id")
            ).mappings().all()
        return [Source.model_validate(dict(row)) for row in rows]

    def get(self, source_id: str) -> Source:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT {_COLUMNS} FROM sources WHERE id = :id"), {"id": source_id}
            ).mappings().first()
        if row is None:
            raise SourceNotFound(source_id)
        return Source.model_validate(dict(row))

    def add(self, source: Source) -> Source:
        with self._engine.begin() as connection:
            # Conditional insert rather than a check and then a write: two
            # consoles declaring the same name at once both found it free.
            inserted = connection.execute(
                text(
                    f"INSERT INTO sources ({_COLUMNS}) "
                    "VALUES (:id, :adapter, :url_env, :namespace, :label) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                source.model_dump(mode="json"),
            ).rowcount
        if not inserted:
            raise DuplicateSource(f"a source named {source.id!r} already exists")
        return source

    def remove(self, source_id: str) -> None:
        with self._engine.begin() as connection:
            removed = connection.execute(
                text("DELETE FROM sources WHERE id = :id"), {"id": source_id}
            ).rowcount
        if not removed:
            raise SourceNotFound(source_id)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Session-scoped, so it spans the transactions taken inside it.

        A transaction-scoped lock would release at the first commit — which
        happens partway through the operations this exists to make atomic, such
        as checking that no workspace references a source and then deleting it.

        Re-entrant within a thread. An advisory lock is held by the session, so
        a second `connect()` from a thread that already holds it is a different
        session and would wait for the first one forever.
        """
        held = getattr(self._state, "depth", 0)
        if held:
            self._state.depth = held + 1
            try:
                yield
            finally:
                self._state.depth -= 1
            return

        with self._engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
            self._state.depth = 1
            try:
                yield
            finally:
                self._state.depth = 0
                connection.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY}
                )
                connection.commit()
