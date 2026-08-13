"""Job status in Atlas's own PostgreSQL.

Shares the database the semantic record lives in, and the engine that connects
to it, so an install pointed at PostgreSQL keeps nothing on local disk.

The reason to be here rather than in SQLite is the exclusivity check. Two
extracts submitted at the same instant both queried an idle workspace and both
proceeded; SQLite closes that with a process-wide lock, which stops holding the
moment there are two processes. Here it is one statement — an insert conditional
on no active job existing — and the database is the thing arbitrating.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, text

from atlas.jobs.base import JobStore
from atlas.jobs.models import (
    EXCLUSIVE_KINDS,
    ActiveWorkspaceJob,
    Job,
    JobProgress,
    JobStatus,
)

_LIVE = (JobStatus.PENDING.value, JobStatus.RUNNING.value)

_COLUMNS = (
    "id, kind, workspace, status, created_at, started_at, finished_at, "
    "progress, result, error, snapshot_generation, source_id, workspace_incarnation"
)


class PostgresJobStore(JobStore):
    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        self._engine = engine or create_engine(url, pool_pre_ping=True, future=True)

    def __repr__(self) -> str:
        return f"PostgresJobStore({self._engine.url.render_as_string()!r})"

    def dispose(self) -> None:
        self._engine.dispose()

    def initialize(self) -> None:
        """Verify, never create.

        The table belongs to a migration. A store that creates its own on
        connect gives the schema two definitions, and the one that runs first
        wins silently.
        """
        with self._engine.connect() as connection:
            present = connection.execute(text("SELECT to_regclass('public.jobs')")).scalar()
        if present is None:
            raise RuntimeError(
                "the jobs table is missing; run `make migrate` "
                "(alembic upgrade head) against ATLAS_DATABASE_URL"
            )

    # ---- writing ---------------------------------------------------------

    def insert(self, job: Job, *, exclusive: bool) -> None:
        payload = _payload(job)
        with self._engine.begin() as connection:
            if exclusive:
                # One statement: the row goes in only if no live mutation job
                # holds this workspace, so the check cannot be overtaken
                # between asking and inserting.
                inserted = connection.execute(
                    text(
                        f"INSERT INTO jobs ({_COLUMNS}) "
                        "SELECT :id, :kind, :workspace, :status, :created_at, :started_at, "
                        "       :finished_at, CAST(:progress AS jsonb), "
                        "       CAST(:result AS jsonb), :error, :snapshot_generation, "
                        "       :source_id, :workspace_incarnation "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM jobs WHERE workspace = :workspace "
                        "  AND kind = ANY(:kinds) AND status = ANY(:live)"
                        ")"
                    ),
                    {**payload, "kinds": list(EXCLUSIVE_KINDS), "live": list(_LIVE)},
                ).rowcount
                if not inserted:
                    holder = connection.execute(
                        text(
                            "SELECT id FROM jobs WHERE workspace = :workspace "
                            "AND kind = ANY(:kinds) AND status = ANY(:live) "
                            "ORDER BY created_at LIMIT 1"
                        ),
                        {
                            "workspace": job.workspace,
                            "kinds": list(EXCLUSIVE_KINDS),
                            "live": list(_LIVE),
                        },
                    ).scalar()
                    raise ActiveWorkspaceJob(holder or "unknown")
                return
            connection.execute(
                text(
                    f"INSERT INTO jobs ({_COLUMNS}) VALUES "
                    "(:id, :kind, :workspace, :status, :created_at, :started_at, "
                    " :finished_at, CAST(:progress AS jsonb), CAST(:result AS jsonb), "
                    " :error, :snapshot_generation, :source_id, :workspace_incarnation)"
                ),
                payload,
            )

    def record_progress(self, job_id: str, progress: JobProgress) -> None:
        self._update(
            job_id,
            "progress = CAST(:progress AS jsonb)",
            {"progress": json.dumps(progress.model_dump(mode="json"), default=str)},
        )

    def record_started(self, job_id: str, at: datetime) -> None:
        self._update(
            job_id,
            "status = :status, started_at = :at",
            {"status": JobStatus.RUNNING.value, "at": at},
        )

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
            "status = :status, finished_at = :at, "
            "result = CAST(:result AS jsonb), error = :error",
            {
                "status": status.value,
                "at": at,
                "result": None if result is None else json.dumps(result, default=str),
                "error": error,
            },
        )

    def delete_workspace(self, workspace: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM jobs WHERE workspace = :w"), {"w": workspace}
            )

    def reconcile(self, message: str) -> int:
        with self._engine.begin() as connection:
            return connection.execute(
                text(
                    "UPDATE jobs SET status = :interrupted, finished_at = :at, "
                    "  error = :message WHERE status = ANY(:live)"
                ),
                {
                    "interrupted": JobStatus.INTERRUPTED.value,
                    "at": datetime.now(UTC),
                    "message": message,
                    "live": list(_LIVE),
                },
            ).rowcount

    def evict(self, keep: int) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM jobs WHERE id IN ("
                    "  SELECT id FROM jobs WHERE finished_at IS NOT NULL "
                    "  ORDER BY finished_at DESC OFFSET :keep"
                    ")"
                ),
                {"keep": keep},
            )

    def _update(self, job_id: str, assignments: str, params: dict[str, Any]) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(f"UPDATE jobs SET {assignments} WHERE id = :id"),
                {**params, "id": job_id},
            )

    # ---- reading ---------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT {_COLUMNS} FROM jobs WHERE id = :id"), {"id": job_id}
            ).mappings().first()
        return Job.model_validate(dict(row)) if row else None

    def list(self, workspace: str | None = None) -> list[Job]:
        query = f"SELECT {_COLUMNS} FROM jobs"
        params: dict[str, Any] = {}
        if workspace:
            query += " WHERE workspace = :w"
            params["w"] = workspace
        query += " ORDER BY created_at DESC"
        with self._engine.connect() as connection:
            rows = connection.execute(text(query), params).mappings().all()
        return [Job.model_validate(dict(row)) for row in rows]

    def active(self, workspace: str) -> Job | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT {_COLUMNS} FROM jobs WHERE workspace = :w "
                    "AND kind = ANY(:kinds) AND status = ANY(:live) "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"w": workspace, "kinds": list(EXCLUSIVE_KINDS), "live": list(_LIVE)},
            ).mappings().first()
        return Job.model_validate(dict(row)) if row else None


def _payload(job: Job) -> dict[str, Any]:
    data = job.model_dump(mode="json")
    for column in ("progress", "result"):
        data[column] = (
            None if data[column] is None else json.dumps(data[column], default=str)
        )
    return data
