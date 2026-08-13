"""One set of expectations for declared sources, run against every store.

The smallest of the three conformance suites, because a source is the smallest
thing Atlas keeps: an id, an adapter, a namespace, and the *name* of the
variable its URL is read from.

That last clause is the one worth guarding. Moving declarations into a database
must not quietly turn a credential-free table into a credential-bearing one, so
there is a test asserting no store ever sees a URL.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from atlas.sources.models import DuplicateSource, Source, SourceNotFound
from atlas.sources.postgres_store import PostgresSourceRepository
from atlas.sources.yaml_store import YamlSourceRepository

DATABASE_URL = os.environ.get("ATLAS_TEST_DATABASE_URL", "")


def _yaml(tmp_path) -> YamlSourceRepository:
    return YamlSourceRepository(tmp_path / "sources.yaml")


def _postgres(_tmp_path) -> PostgresSourceRepository:
    engine_root = Path(__file__).resolve().parent.parent
    config = Config(engine_root / "alembic.ini")
    config.set_main_option("script_location", str(engine_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    repository = PostgresSourceRepository(DATABASE_URL)
    with repository._engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    command.upgrade(config, "head")
    return repository


STORES = [
    pytest.param(_yaml, id="yaml"),
    pytest.param(
        _postgres,
        id="postgres",
        marks=[
            pytest.mark.postgres,
            pytest.mark.skipif(
                not DATABASE_URL, reason="ATLAS_TEST_DATABASE_URL is not set"
            ),
        ],
    ),
]


@pytest.fixture(params=STORES)
def store(request, tmp_path):
    built = request.param(tmp_path)
    yield built
    if isinstance(built, PostgresSourceRepository):
        built.dispose()


def source(source_id: str = "elara", **overrides) -> Source:
    return Source(
        id=source_id,
        adapter=overrides.pop("adapter", "postgresql"),
        url_env=overrides.pop("url_env", "ELARA_DATABASE_URL"),
        **overrides,
    )


def test_an_empty_store_declares_nothing(store) -> None:
    assert store.list() == []
    with pytest.raises(SourceNotFound):
        store.get("elara")


def test_a_declared_source_survives_a_round_trip(store) -> None:
    store.add(source(namespace="reporting", label="Elara reporting"))

    loaded = store.get("elara")
    assert (loaded.adapter, loaded.url_env) == ("postgresql", "ELARA_DATABASE_URL")
    assert (loaded.namespace, loaded.label) == ("reporting", "Elara reporting")


def test_the_store_never_holds_a_connection_string(store, tmp_path, monkeypatch) -> None:
    """The whole design of `Source`. A reader who dumps this store learns which
    databases exist and nothing that would let them connect."""
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql://user:hunter2@db.internal/app")
    store.add(source())

    assert "hunter2" not in str(store.list())
    # And the source still knows how to find it at the moment of use.
    assert store.get("elara").resolve_url().endswith("db.internal/app")


def test_declaring_a_name_twice_is_refused(store) -> None:
    store.add(source())

    with pytest.raises(DuplicateSource):
        store.add(source(adapter="snowflake", url_env="OTHER_URL"))

    # The original stands. Overwriting would repoint every workspace bound to it.
    assert store.get("elara").adapter == "postgresql"


def test_sources_are_listed_in_a_stable_order(store) -> None:
    for name in ("warehouse", "elara", "billing"):
        store.add(source(name, url_env=f"{name.upper()}_URL"))

    assert [s.id for s in store.list()] == [s.id for s in store.list()]
    assert {s.id for s in store.list()} == {"warehouse", "elara", "billing"}


def test_removing_takes_only_that_source(store) -> None:
    store.add(source("elara"))
    store.add(source("warehouse", url_env="WAREHOUSE_URL"))

    store.remove("elara")

    assert [s.id for s in store.list()] == ["warehouse"]


def test_removing_something_undeclared_is_refused(store) -> None:
    with pytest.raises(SourceNotFound):
        store.remove("never-declared")


def test_the_lock_is_reentrant_within_a_thread(store) -> None:
    """The API holds it across a check that spans two stores and then writes
    through a method that wants it too."""
    with store.lock():  # noqa: SIM117 - the nesting is the subject
        with store.lock():
            store.add(source())

    assert store.get("elara").id == "elara"


def test_concurrent_declarations_of_one_name_leave_one_winner(store) -> None:
    start = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def declare() -> None:
        start.wait(timeout=5)
        try:
            store.add(source())
        except DuplicateSource:
            with guard:
                outcomes.append("refused")
        else:
            with guard:
                outcomes.append("accepted")

    threads = [threading.Thread(target=declare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["accepted", "refused"]
    assert len(store.list()) == 1
