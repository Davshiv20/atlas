"""Adapter selection by dialect.

The one place that maps a URL to an implementation. Everything else takes a
`DatabaseAdapter` and never learns which engine is behind it.
"""

from __future__ import annotations

from sqlalchemy import create_engine

from atlas.adapters.base import DatabaseAdapter, UnsupportedDatabase
from atlas.adapters.postgres import PostgresAdapter


def create_adapter(url: str, concurrency: int = 1) -> DatabaseAdapter:
    """Build the adapter for whatever this URL points at.

    `concurrency` sizes the connection pool to the number of workers that will
    share this adapter. Left at SQLAlchemy's default of five, a six-worker run
    silently spills into overflow connections against a database we do not own.
    """
    engine = create_engine(url, pool_size=max(concurrency, 1), max_overflow=2)
    dialect = engine.dialect.name

    if dialect == "postgresql":
        return PostgresAdapter(engine)

    # Snowflake is the next adapter and is not written yet. The port exists and
    # `DatabaseCapabilities` already carries what it needs — unenforced
    # constraints, no read-only transaction — but advertising it here made a
    # source created from the console fail with an ImportError instead of a
    # sentence.
    engine.dispose()
    raise UnsupportedDatabase(
        f"no adapter for dialect {dialect!r}. Only postgresql is implemented."
    )
