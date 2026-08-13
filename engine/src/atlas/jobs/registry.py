"""Running background work, and saying honestly what happened to it.

Extraction takes seconds and analysis takes minutes per table, so neither can
run inside a request. Every long operation is submitted, returns immediately
with an id, and is polled.

Everything about *where* status is kept is a `JobStore`. What is here is the
part that cannot be swapped: starting the worker, serializing mutations of one
workspace, deciding what counts as failure, and settling what a dead process
left behind.

What this does not do is keep the work alive. The worker is a thread in this
process, so a restart still kills the run; it leaves an honest record saying so
(`INTERRUPTED`) instead of a job that vanished or, worse, one stuck at `RUNNING`
forever. Surviving a restart needs a separate worker process reading the same
table — a larger change the store port is deliberately ready for.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from atlas.jobs.base import JobStore
from atlas.jobs.models import (
    MAX_RETAINED_JOBS,
    Job,
    JobProgress,
    JobStatus,
)
from atlas.jobs.postgres_store import PostgresJobStore
from atlas.jobs.sqlite_store import SqliteJobStore
from atlas.settings import get_settings

logger = logging.getLogger(__name__)

INTERRUPTED_BY_RESTART = "The engine restarted while this run was in progress."


class ProgressReporter:
    """How running work says what it is doing.

    Work used to assign `job.progress` and that was enough, because the reader
    held the same object in memory. Once status lives in a store it is not, and
    an attribute assignment that silently writes to a database would be worse
    than the bug it fixes. Reporting is a call.
    """

    def __init__(self, registry: JobRegistry, job_id: str) -> None:
        self.registry = registry
        self.job_id = job_id

    def __call__(self, progress: JobProgress) -> None:
        self.registry.record_progress(self.job_id, progress)


class JobRegistry:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._workspace_locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}
        self.store.initialize()

    def __repr__(self) -> str:
        return f"JobRegistry({self.store!r})"

    # --- lifecycle ---------------------------------------------------------

    @contextmanager
    def workspace_guard(self, workspace: str) -> Iterator[None]:
        """Serialize mutations of one workspace inside this process.

        In-process only, and knowingly so: the store's exclusivity check is
        what holds across processes. This closes the narrower window between
        two threads here that have both already passed that check.
        """
        with self._workspace_locks_guard:
            lock = self._workspace_locks.setdefault(workspace, threading.Lock())
        with lock:
            yield

    def reconcile(self) -> int:
        orphaned = self.store.reconcile(INTERRUPTED_BY_RESTART)
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
        # Recorded before the thread starts. A worker that began first could
        # finish and write its result against a row that does not exist yet.
        self.store.insert(job, exclusive=exclusive)
        self.store.evict(MAX_RETAINED_JOBS)
        threading.Thread(
            target=self._run, args=(job.id, workspace, work), daemon=True
        ).start()
        return job

    # --- reading -----------------------------------------------------------

    def active_workspace_job(self, workspace: str) -> Job | None:
        return self.store.active(workspace)

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def list(self, workspace: str | None = None) -> list[Job]:
        return self.store.list(workspace)

    def delete_workspace(self, workspace: str) -> None:
        self.store.delete_workspace(workspace)

    # --- writing -----------------------------------------------------------

    def record_progress(self, job_id: str, progress: JobProgress) -> None:
        self.store.record_progress(job_id, progress)

    def _run(
        self, job_id: str, workspace: str, work: Callable[[ProgressReporter], dict]
    ) -> None:
        self.store.record_started(job_id, datetime.now(UTC))
        try:
            with self.workspace_guard(workspace):
                result = work(ProgressReporter(self, job_id))
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            # Message for the client, traceback for the log — a stack trace in
            # an API response leaks connection strings and file paths.
            logger.debug("%s", traceback.format_exc())
            self.store.record_finished(
                job_id,
                JobStatus.FAILED,
                datetime.now(UTC),
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            self.store.record_finished(
                job_id, JobStatus.SUCCEEDED, datetime.now(UTC), result=result
            )


def build_store() -> JobStore:
    """Job status follows the record.

    Deliberately the same switch as `atlas.metadata.registry`, not a second
    setting. An install with its record in PostgreSQL and its jobs in a local
    file is one where scaling to a second engine process silently half-works,
    and the half that fails is the one that decides whether two extracts of the
    same workspace may run at once.
    """
    url = get_settings().atlas_database_url
    if url is None:
        return SqliteJobStore()
    return PostgresJobStore(url)


@lru_cache(maxsize=1)
def get_registry() -> JobRegistry:
    """The process-wide registry, opened on first use.

    A function rather than a module-level instance: importing this module must
    not create files or open connections. The settings that say where they go
    are not necessarily loaded at import time, and a test that merely imports
    the API would write a database into the developer's working directory.
    """
    return JobRegistry(build_store())
