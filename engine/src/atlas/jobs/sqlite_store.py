"""Job status in a local SQLite file.

The default, and enough for the single-process engine it was written for: one
API process, worker threads inside it, a file beside the workspaces. It creates
its own schema because there is nothing else to — an operator running the file
store never runs a migration.

Its ceiling is the same as the file metadata store's. A second engine process
pointed at the same file would see the jobs but could not run them, because the
worker is a thread here and the workspace guard is a `threading.Lock`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from atlas.jobs.base import JobStore
from atlas.jobs.models import (
    EXCLUSIVE_KINDS,
    ActiveWorkspaceJob,
    Job,
    JobProgress,
    JobStatus,
)
from atlas.settings import get_settings

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

# Columns held as JSON text. The pydantic model stays the single definition of
# their shape — the table knows only that they are documents.
JSON_COLUMNS = ("progress", "result")

_ACTIVE = (
    "SELECT * FROM jobs WHERE workspace = ? "
    f"AND kind IN ({', '.join('?' * len(EXCLUSIVE_KINDS))}) "
    "AND status IN (?, ?) ORDER BY created_at LIMIT 1"
)


def _active_parameters(workspace: str) -> tuple[str, ...]:
    return (
        workspace,
        *EXCLUSIVE_KINDS,
        JobStatus.PENDING.value,
        JobStatus.RUNNING.value,
    )


class SqliteJobStore(JobStore):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_settings().atlas_output_dir / "jobs.db"
        # Serialises this process's writers. SQLite would handle them via
        # busy_timeout, but a lock turns "eventually got the write lock" into
        # "waited", which is cheaper and easier to reason about.
        self._write_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"SqliteJobStore({str(self.path)!r})"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    # ---- writing ---------------------------------------------------------

    def insert(self, job: Job, *, exclusive: bool) -> None:
        with self._write_lock, self._connect() as connection:
            if exclusive:
                active = connection.execute(_ACTIVE, _active_parameters(job.workspace)).fetchone()
                if active:
                    raise ActiveWorkspaceJob(active["id"])
            payload = job.model_dump(mode="json")
            for column in JSON_COLUMNS:
                payload[column] = _dump(payload[column])
            columns = ", ".join(payload)
            placeholders = ", ".join(f":{name}" for name in payload)
            connection.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", payload
            )

    def record_progress(self, job_id: str, progress: JobProgress) -> None:
        self._update(job_id, progress=_dump(progress.model_dump(mode="json")))

    def record_started(self, job_id: str, at: datetime) -> None:
        self._update(job_id, status=JobStatus.RUNNING.value, started_at=at.isoformat())

    def record_finished(
        self,
        job_id: str,
        status: JobStatus,
        at: datetime,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        self._update(
            job_id,
            status=status.value,
            finished_at=at.isoformat(),
            result=_dump(result),
            error=error,
        )

    def delete_workspace(self, workspace: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE workspace = ?", (workspace,))

    def reconcile(self, message: str) -> int:
        now = datetime.now(UTC).isoformat()
        with self._write_lock, self._connect() as connection:
            return connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ? "
                "WHERE status IN (?, ?)",
                (
                    JobStatus.INTERRUPTED.value,
                    now,
                    message,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            ).rowcount

    def evict(self, keep: int) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT id FROM jobs WHERE finished_at IS NOT NULL"
                "  ORDER BY finished_at DESC LIMIT -1 OFFSET ?"
                ")",
                (keep,),
            )

    def _update(self, job_id: str, **fields: str | None) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = :id", {**fields, "id": job_id}
            )

    # ---- reading ---------------------------------------------------------

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

    def active(self, workspace: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(_ACTIVE, _active_parameters(workspace)).fetchone()
        return _to_job(row) if row else None


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
