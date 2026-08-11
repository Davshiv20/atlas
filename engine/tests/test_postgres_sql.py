"""The generated SQL, actually executed.

Every other test in this suite fakes at the `CheckObservation` boundary. Those
prove `checks.py` reaches the right verdict from a given set of numbers and say
nothing about whether the adapter produced those numbers correctly — so a whole
class of defect was structurally invisible. Four lived in exactly that gap: a
join that counted one source row once per match, a composite grain that looked
only at its first key column for nulls, and two profiling queries that opened
unguarded connections against a database Atlas does not own.

SQLite cannot stand in here. It rejects `count(DISTINCT (a, b))` outright, so a
harness built on it would go green on the very grain bug it was built to catch.

Requires a real PostgreSQL: `make engine-test-postgres`. Skipped otherwise, so
the default loop stays fast and free of a Docker dependency.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from atlas.adapters.base import GrainCheck, JoinCheck
from atlas.adapters.postgres import PostgresAdapter
from atlas.checks import run_check
from atlas.evidence import Verdict

URL = os.environ.get("ATLAS_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="ATLAS_TEST_DATABASE_URL is not set"),
]

SCHEMA = "atlas_sql_test"

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};

-- `status` is not unique in events, so joining on it fans out: three source
-- rows meet three matching target rows and a LEFT JOIN yields seven.
CREATE TABLE {SCHEMA}.orders (id int PRIMARY KEY, status text);
CREATE TABLE {SCHEMA}.events (id int PRIMARY KEY, status text);
INSERT INTO {SCHEMA}.orders VALUES (1, 'open'), (2, 'open'), (3, 'ghost');
INSERT INTO {SCHEMA}.events VALUES (10, 'open'), (11, 'open'), (12, 'open');

-- A composite key whose second column is nullable.
CREATE TABLE {SCHEMA}.memberships (org_id int, user_id int, role text);
INSERT INTO {SCHEMA}.memberships VALUES (1, 1, 'owner'), (1, NULL, 'ghost');
"""


@pytest.fixture(scope="module")
def adapter():
    engine = create_engine(URL)
    with engine.begin() as connection:
        connection.exec_driver_sql(DDL)
    built = PostgresAdapter(engine)
    yield built
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    built.close()


def test_a_fanned_out_join_counts_each_source_row_once(adapter) -> None:
    """`orders` holds three rows, one of which references nothing.

    Counting `count(*)` over a LEFT JOIN counted a source row once per match,
    reporting seven source rows for a three-row table and dividing the orphan
    count by that inflated denominator — 14% where the truth is 33%. Nothing
    downstream can recover the real figure from those numbers.
    """
    record, _ = run_check(
        adapter,
        JoinCheck(
            source_relation=f"{SCHEMA}.orders",
            source_fields=["status"],
            target_relation=f"{SCHEMA}.events",
            target_fields=["status"],
        ),
        database="test",
    )

    assert record is not None
    assert record.observation["source_rows"] == 3
    assert record.observation["orphan_rows"] == 1
    assert record.observation["orphan_rate"] == pytest.approx(1 / 3, rel=1e-3)
    assert record.verdict is Verdict.FAILED


def test_a_null_in_any_key_column_fails_the_grain(adapter) -> None:
    """One membership row has no `user_id`, so the table has no such grain.

    `count(DISTINCT (a, b))` skips a row with a null component, so total and
    distinct agreed; and the null count looked only at `key_fields[0]`, which
    is populated. Both blind spots pointed the same way and the grain passed.
    """
    record, message = run_check(
        adapter,
        GrainCheck(relation=f"{SCHEMA}.memberships", key_fields=["org_id", "user_id"]),
        database="test",
    )

    assert record is not None
    assert record.observation["null_keys"] == 1
    assert record.verdict is Verdict.FAILED
    assert "null key" in message


def test_every_profiling_query_goes_through_the_guard(adapter, monkeypatch) -> None:
    """The read-only transaction and the statement timeout lived in `_run`.

    That covers typed checks, which are bounded and cheap. The aggregate sweep
    and the top-value query — a count(DISTINCT) and a GROUP BY per column, by
    far the most expensive statements this adapter issues — opened raw
    connections and ran with no timeout and no read-only guarantee.
    """
    guarded: list[str] = []
    original = PostgresAdapter._begin_read_only

    def spy(self, connection) -> None:
        guarded.append("guarded")
        original(self, connection)

    monkeypatch.setattr(PostgresAdapter, "_begin_read_only", spy)

    snapshot = adapter.extract_structure(SCHEMA)
    adapter.profile(snapshot)

    assert guarded, "profiling opened a connection that skipped the guard"


def test_the_guard_actually_holds_the_session_read_only(adapter) -> None:
    """Proof the guard is more than a call: the session refuses to write."""
    with adapter._read_only() as connection:
        assert connection.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() != "0"


def test_top_values_read_whatever_source_the_sweep_read(adapter) -> None:
    """A sampled table drew its aggregates from a TABLESAMPLE and its values
    from the whole table, so a profile stamped `sampled=True` carried
    full-scan values under it. A reader cannot tell those apart, so the label
    has to be true of every field beneath it."""
    whole = adapter._top_values(f"{SCHEMA}.orders", "status", "full")
    restricted = adapter._top_values(
        f"(SELECT * FROM {SCHEMA}.orders WHERE id = 1) AS sampled", "status", "full"
    )

    assert whole is not None and restricted is not None
    assert sum(v.count for v in whole) == 3
    assert sum(v.count for v in restricted) == 1
