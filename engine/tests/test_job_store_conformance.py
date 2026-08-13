"""One set of expectations for job status, run against every store.

The sibling of `test_metadata_conformance`. Smaller, because a job store holds
less: what a run is doing, never what it produced.

The clause that matters most is exclusivity. Two extracts submitted at the same
instant both saw an idle workspace and both proceeded, and the fix belongs in
whichever store is in use — so it is asserted here rather than trusted to one
implementation.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from atlas.jobs.models import ActiveWorkspaceJob, Job, JobProgress, JobStatus
from atlas.jobs.postgres_store import PostgresJobStore
from atlas.jobs.sqlite_store import SqliteJobStore

DATABASE_URL = os.environ.get("ATLAS_TEST_DATABASE_URL", "")


def _sqlite(tmp_path) -> SqliteJobStore:
    store = SqliteJobStore(tmp_path / "jobs.db")
    store.initialize()
    return store


def _postgres(_tmp_path) -> PostgresJobStore:
    engine_root = Path(__file__).resolve().parent.parent
    config = Config(engine_root / "alembic.ini")
    config.set_main_option("script_location", str(engine_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    store = PostgresJobStore(DATABASE_URL)
    with store._engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    command.upgrade(config, "head")
    store.initialize()
    return store


STORES = [
    pytest.param(_sqlite, id="sqlite"),
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
    if isinstance(built, PostgresJobStore):
        built.dispose()


def job(job_id: str = "job_1", *, kind: str = "analyze", workspace: str = "demo") -> Job:
    return Job(id=job_id, kind=kind, workspace=workspace)


# --- the basics ------------------------------------------------------------


def test_an_unknown_job_is_none_rather_than_an_error(store) -> None:
    assert store.get("job_missing") is None
    assert store.list() == []
    assert store.active("demo") is None


def test_a_submitted_job_survives_a_round_trip(store) -> None:
    submitted = Job(
        id="job_1",
        kind="extract",
        workspace="demo",
        snapshot_generation=3,
        source_id="shop",
        workspace_incarnation="a" * 32,
    )
    store.insert(submitted, exclusive=False)

    loaded = store.get("job_1")
    assert loaded is not None
    assert (loaded.kind, loaded.workspace) == ("extract", "demo")
    assert loaded.status is JobStatus.PENDING
    assert (loaded.snapshot_generation, loaded.source_id) == (3, "shop")
    assert loaded.workspace_incarnation == "a" * 32
    assert loaded.created_at == submitted.created_at


def test_progress_is_readable_by_someone_who_is_not_the_worker(store) -> None:
    """The whole reason status is stored at all: the reviewer polling from the
    console holds none of the worker's objects."""
    store.insert(job(), exclusive=False)

    store.record_progress(
        "job_1",
        JobProgress(
            message="Reading users",
            tables=["users", "orders"],
            completed=["orders"],
            current=["users"],
        ),
    )

    progress = store.get("job_1").progress
    assert progress.message == "Reading users"
    assert progress.current == ["users"]
    assert progress.completed == ["orders"]


def test_a_finished_job_carries_its_result(store) -> None:
    store.insert(job(), exclusive=False)
    store.record_started("job_1", datetime.now(UTC))

    store.record_finished(
        "job_1", JobStatus.SUCCEEDED, datetime.now(UTC), result={"claims": 4}
    )

    finished = store.get("job_1")
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result == {"claims": 4}
    assert finished.started_at is not None
    assert finished.finished_at is not None
    assert finished.error is None


def test_a_failed_job_carries_its_message(store) -> None:
    store.insert(job(), exclusive=False)
    store.record_finished(
        "job_1", JobStatus.FAILED, datetime.now(UTC), error="RuntimeError: nope"
    )

    failed = store.get("job_1")
    assert failed.status is JobStatus.FAILED
    assert failed.error == "RuntimeError: nope"
    assert failed.result is None


def test_jobs_are_listed_newest_first_and_filtered_by_workspace(store) -> None:
    older = Job(
        id="job_old",
        kind="analyze",
        workspace="demo",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = Job(
        id="job_new",
        kind="analyze",
        workspace="demo",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    elsewhere = Job(
        id="job_other",
        kind="analyze",
        workspace="other",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    for entry in (older, newer, elsewhere):
        store.insert(entry, exclusive=False)

    assert [j.id for j in store.list("demo")] == ["job_new", "job_old"]
    assert {j.id for j in store.list()} == {"job_new", "job_old", "job_other"}


# --- exclusivity -----------------------------------------------------------


def test_a_second_mutation_of_one_workspace_is_refused(store) -> None:
    store.insert(job("job_first"), exclusive=True)

    with pytest.raises(ActiveWorkspaceJob) as refused:
        store.insert(job("job_second"), exclusive=True)

    assert refused.value.job_id == "job_first"
    assert store.get("job_second") is None, "the refused job must not be recorded"


def test_the_workspace_frees_up_once_the_job_finishes(store) -> None:
    store.insert(job("job_first"), exclusive=True)
    store.record_finished("job_first", JobStatus.SUCCEEDED, datetime.now(UTC))

    store.insert(job("job_second"), exclusive=True)

    assert store.get("job_second") is not None


def test_two_workspaces_do_not_block_each_other(store) -> None:
    store.insert(job("job_a", workspace="demo"), exclusive=True)
    store.insert(job("job_b", workspace="other"), exclusive=True)

    assert store.active("demo").id == "job_a"
    assert store.active("other").id == "job_b"


def test_a_reading_job_does_not_hold_the_workspace(store) -> None:
    """Only extract and analyze mutate. Two reads racing costs nothing, and
    treating them as exclusive would block a refresh behind a compile."""
    store.insert(job("job_read", kind="compile"), exclusive=True)

    store.insert(job("job_write", kind="analyze"), exclusive=True)

    assert store.active("demo").id == "job_write"


def test_concurrent_submissions_leave_exactly_one_winner(store) -> None:
    """The race the atomic check exists for: both threads ask an idle
    workspace and both are told to go ahead."""
    start = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def submit(job_id: str) -> None:
        start.wait(timeout=5)
        try:
            store.insert(job(job_id), exclusive=True)
        except ActiveWorkspaceJob:
            with guard:
                outcomes.append("refused")
        else:
            with guard:
                outcomes.append("accepted")

    threads = [threading.Thread(target=submit, args=(f"job_{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["accepted", "refused"]
    assert len(store.list("demo")) == 1


# --- housekeeping ----------------------------------------------------------


def test_reconcile_settles_what_a_dead_process_left_running(store) -> None:
    store.insert(job("job_stuck"), exclusive=False)
    store.record_started("job_stuck", datetime.now(UTC))
    store.insert(job("job_done", workspace="other"), exclusive=False)
    store.record_finished("job_done", JobStatus.SUCCEEDED, datetime.now(UTC))

    assert store.reconcile("the engine restarted") == 1

    settled = store.get("job_stuck")
    assert settled.status is JobStatus.INTERRUPTED
    assert settled.error == "the engine restarted"
    assert settled.finished_at is not None
    # A job that had already finished is not rewritten.
    assert store.get("job_done").status is JobStatus.SUCCEEDED


def test_deleting_a_workspace_forgets_its_jobs_only(store) -> None:
    store.insert(job("job_a", workspace="demo"), exclusive=False)
    store.insert(job("job_b", workspace="other"), exclusive=False)

    store.delete_workspace("demo")

    assert store.get("job_a") is None
    assert store.get("job_b") is not None


def test_eviction_drops_the_oldest_finished_jobs_and_spares_the_living(store) -> None:
    for index in range(4):
        entry = Job(id=f"job_{index}", kind="analyze", workspace="demo")
        store.insert(entry, exclusive=False)
        store.record_finished(
            f"job_{index}",
            JobStatus.SUCCEEDED,
            datetime(2026, 1, index + 1, tzinfo=UTC),
        )
    store.insert(job("job_running"), exclusive=False)
    store.record_started("job_running", datetime.now(UTC))

    store.evict(keep=2)

    remaining = {j.id for j in store.list("demo")}
    assert remaining == {"job_3", "job_2", "job_running"}
