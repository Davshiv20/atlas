"""Background job execution.

Extraction takes seconds and analysis takes minutes per table, so neither can
run inside a request. Every long operation is submitted, returns immediately
with an id, and is polled.

Status is stored in SQLite rather than in memory. The output was never at risk
— results are written to the workspace as they complete — but the *progress
indicator* was, and losing it is worse than it sounds: a restart made a live
run invisible, so the console showed an idle workspace while the engine was ten
minutes into analysing one.

What this does not do is keep the work alive. The worker is still a thread in
this process, so a restart still kills the run; it now leaves an honest record
saying so (`INTERRUPTED`) instead of a job that vanished or, worse, one stuck at
`RUNNING` forever. Surviving a restart needs a separate worker process reading
the same table — a larger change this schema is deliberately ready for.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from atlas.settings import get_settings

logger = logging.getLogger(__name__)

MAX_RETAINED_JOBS = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    progress    TEXT,
    result      TEXT,
    error       TEXT,
    snapshot_generation INTEGER,
    source_id TEXT,
    workspace_incarnation TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_workspace ON jobs (workspace, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status);
"""

# Columns held as JSON text. The Pydantic model stays the single definition of
# their shape — the table knows only that they are documents.
JSON_COLUMNS = ("progress", "result")


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # The process died while this was running. Distinct from FAILED, which
    # means the work itself raised: nothing is known about how far it got, and
    # nothing is wrong with the request that started it.
    INTERRUPTED = "interrupted"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.INTERRUPTED})


class ActiveWorkspaceJob(RuntimeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"workspace already has an active mutation job: {job_id}")
        self.job_id = job_id


class JobProgress(BaseModel):
    """What a running job is doing, in a shape the console can render.

    A sentence alone was not enough: the analysis spends minutes per table, so
    the reviewer needs to know which table is under way and how much of the run
    is left, not just that something is happening.
    """

    message: str
    tables: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    # Plural: tables are read concurrently, so several are in flight at once.
    current: list[str] = Field(default_factory=list)

    @field_validator("current", mode="before")
    @classmethod
    def _accept_a_single_table(cls, value: object) -> object:
        """Read rows written before tables were read concurrently.

        Job status is persisted now, so a model change is a migration. Rows
        from an older process hold `current` as a table name or null, and
        rejecting them took down the whole list endpoint — which is the one the
        console uses to find a run it did not start, so a live run became
        invisible while `/jobs/{id}` kept working and looking healthy.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class Job(BaseModel):
    id: str
    kind: str
    workspace: str
    snapshot_generation: int | None = None
    source_id: str | None = None
    workspace_incarnation: str | None = None
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: JobProgress | None = None
    result: dict | None = None
    error: str | None = None


class ProgressReporter:
    """How running work says what it is doing.

    Work used to assign `job.progress` and that was enough, because the reader
    held the same object in memory. Once status lives in a database it is not,
    and an attribute assignment that silently writes to disk would be worse
    than the bug it fixes. Reporting is a call.
    """

    def __init__(self, registry: JobRegistry, job_id: str) -> None:
        self.registry = registry
        self.job_id = job_id

    def __call__(self, progress: JobProgress) -> None:
        self.registry.record_progress(self.job_id, progress)


class JobRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_settings().atlas_output_dir / "jobs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialises this process's writers. SQLite would handle them via
        # busy_timeout, but a lock turns "eventually got the write lock" into
        # "waited", which is cheaper and easier to reason about.
        self._write_lock = threading.Lock()
        self._workspace_locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            _migrate(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection per operation.

        Jobs are written once per table analysed — minutes apart — and read
        every couple of seconds. At that volume a fresh connection costs
        nothing and avoids sharing one across the worker and request threads.
        """
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            # WAL so a poll never blocks on the worker writing progress.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                yield connection
        finally:
            connection.close()

    # --- lifecycle ---------------------------------------------------------

    @contextmanager
    def workspace_guard(self, workspace: str) -> Iterator[None]:
        """Serialize file mutations for one workspace inside this instance."""
        with self._workspace_locks_guard:
            lock = self._workspace_locks.setdefault(workspace, threading.Lock())
        with lock:
            yield

    def reconcile(self) -> int:
        """Settle jobs orphaned by a previous process.

        Their worker threads died with that process, so nothing will ever move
        them off RUNNING. Left alone they are worse than a lost job: the console
        polls one forever and reports a run that is not happening.
        """
        now = datetime.now(UTC).isoformat()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ? "
                "WHERE status IN (?, ?)",
                (
                    JobStatus.INTERRUPTED.value,
                    now,
                    "The engine restarted while this run was in progress.",
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
            orphaned = cursor.rowcount
        if orphaned:
            logger.warning("marked %d orphaned job(s) as interrupted", orphaned)
        return orphaned

    def submit(
        self,
        kind: str,
        workspace: str,
        work: Callable[[ProgressReporter], dict],
        *,
        snapshot_generation: int | None = None,
        source_id: str | None = None,
        workspace_incarnation: str | None = None,
        exclusive: bool = False,
    ) -> Job:
        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            kind=kind,
            workspace=workspace,
            snapshot_generation=snapshot_generation,
            source_id=source_id,
            workspace_incarnation=workspace_incarnation,
        )
        self._submit(job, work, exclusive=exclusive)
        return job

    def active_workspace_job(self, workspace: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE workspace = ? AND kind IN ('extract', 'analyze') "
                "AND status IN (?, ?) ORDER BY created_at LIMIT 1",
                (workspace, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
        return _to_job(row) if row else None

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _to_job(row) if row else None

    def list(self, workspace: str | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        parameters: tuple[str, ...] = ()
        if workspace:
            query += " WHERE workspace = ?"
            parameters = (workspace,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_to_job(row) for row in rows]

    def delete_workspace(self, workspace: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE workspace = ?", (workspace,))

    # --- writes ------------------------------------------------------------

    def record_progress(self, job_id: str, progress: JobProgress) -> None:
        self._update(job_id, progress=_dump(progress.model_dump(mode="json")))

    def _run(
        self, job_id: str, workspace: str, work: Callable[[ProgressReporter], dict]
    ) -> None:
        self._update(
            job_id,
            status=JobStatus.RUNNING.value,
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            with self.workspace_guard(workspace):
                result = work(ProgressReporter(self, job_id))
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            # Message for the client, traceback for the log — a stack trace in
            # an API response leaks connection strings and file paths.
            logger.debug("%s", traceback.format_exc())
            self._update(
                job_id,
                status=JobStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC).isoformat(),
            )
        else:
            self._update(
                job_id,
                status=JobStatus.SUCCEEDED.value,
                result=_dump(result),
                finished_at=datetime.now(UTC).isoformat(),
            )

    def _submit(
        self,
        job: Job,
        work: Callable[[ProgressReporter], dict],
        *,
        exclusive: bool,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            if exclusive:
                active = connection.execute(
                    "SELECT id FROM jobs WHERE workspace = ? AND kind IN ('extract', 'analyze') "
                    "AND status IN (?, ?) ORDER BY created_at LIMIT 1",
                    (job.workspace, JobStatus.PENDING.value, JobStatus.RUNNING.value),
                ).fetchone()
                if active:
                    raise ActiveWorkspaceJob(active["id"])
            self._insert_locked(connection, job)
        self._evict()

        thread = threading.Thread(
            target=self._run, args=(job.id, job.workspace, work), daemon=True
        )
        thread.start()

    def _insert(self, job: Job) -> None:
        with self._write_lock, self._connect() as connection:
            self._insert_locked(connection, job)

    def _insert_locked(self, connection: sqlite3.Connection, job: Job) -> None:
        payload = job.model_dump(mode="json")
        for column in JSON_COLUMNS:
            payload[column] = _dump(payload[column])
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{name}" for name in payload)
        connection.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", payload)

    def _update(self, job_id: str, **fields: str | None) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = :id", {**fields, "id": job_id}
            )

    def _evict(self) -> None:
        """Drop the oldest finished jobs past the retention limit.

        Only finished ones: evicting a running job would orphan the thread
        still writing to it.
        """
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT id FROM jobs WHERE finished_at IS NOT NULL"
                "  ORDER BY finished_at DESC LIMIT -1 OFFSET ?"
                ")",
                (MAX_RETAINED_JOBS,),
            )


def _migrate(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "snapshot_generation" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN snapshot_generation INTEGER")
    if "source_id" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN source_id TEXT")
    if "workspace_incarnation" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN workspace_incarnation TEXT")


def _dump(value: object | None) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _to_job(row: sqlite3.Row) -> Job:
    payload = dict(row)
    for column in JSON_COLUMNS:
        payload[column] = json.loads(payload[column]) if payload[column] else None
    return Job.model_validate(payload)


@lru_cache(maxsize=1)
def get_registry() -> JobRegistry:
    """The process-wide registry, opened on first use.

    A function rather than a module-level instance: importing this module must
    not create files. The settings that say where they go are not necessarily
    loaded at import time, and a test that merely imports the API would write a
    database into the developer's working directory.
    """
    return JobRegistry()
