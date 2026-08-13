"""The job-status port.

Atlas's third store, and the smallest. It holds what a run is doing, not what a
run produced — results go into the workspace as each table completes, so losing
this loses the progress indicator and nothing else.

That is worth persisting anyway, because losing it is worse than it sounds: a
restart made a live run invisible, and the console showed an idle workspace
while the engine was ten minutes into analysing one.

What is deliberately not here
-----------------------------

**Running the work.** Threads, the workspace guard, retention policy, and what
counts as failure all live in `atlas.jobs.registry`. A store that also started
threads could not be swapped for one that did not.

**Surviving a restart.** No implementation of this keeps a run alive; the worker
is a thread in the API process either way. What the port guarantees is an honest
record afterwards — `reconcile` settles what the dead process left behind.
Actually surviving would need a separate worker reading the same table, which
this shape is ready for and no adapter does yet.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from atlas.jobs.models import Job, JobProgress, JobStatus


class JobStore(ABC):
    """Where job status is kept."""

    @abstractmethod
    def initialize(self) -> None:
        """Make the store ready to use. Called once per process.

        A store whose schema is managed elsewhere — by a migration tool —
        should verify rather than create: silently creating a table that
        migrations are supposed to own is how two definitions of it appear.
        """

    # ---- writing ---------------------------------------------------------

    @abstractmethod
    def insert(self, job: Job, *, exclusive: bool) -> None:
        """Record a submitted job.

        `exclusive` means refuse if this workspace already has a mutation job
        pending or running, raising `ActiveWorkspaceJob` with the id of the one
        that holds it. The check and the insert must be atomic: two extracts
        submitted at once both saw an idle workspace and both proceeded, which
        is the race this argument exists to close.
        """

    @abstractmethod
    def record_progress(self, job_id: str, progress: JobProgress) -> None: ...

    @abstractmethod
    def record_started(self, job_id: str, at: datetime) -> None: ...

    @abstractmethod
    def record_finished(
        self,
        job_id: str,
        status: JobStatus,
        at: datetime,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> None: ...

    @abstractmethod
    def delete_workspace(self, workspace: str) -> None:
        """Forget a deleted workspace's jobs. Its history describes something
        that no longer exists."""

    @abstractmethod
    def reconcile(self, message: str) -> int:
        """Settle jobs orphaned by a process that is gone, returning how many.

        Their worker threads died with it, so nothing will move them off
        RUNNING. Left alone they are worse than a lost job: the console polls
        one forever and reports a run that is not happening.
        """

    @abstractmethod
    def evict(self, keep: int) -> None:
        """Drop the oldest finished jobs past `keep`.

        Finished ones only. Evicting a running job orphans the thread still
        writing to it.
        """

    # ---- reading ---------------------------------------------------------

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def list(self, workspace: str | None = None) -> list[Job]:
        """Every job, newest first, optionally for one workspace."""

    @abstractmethod
    def active(self, workspace: str) -> Job | None:
        """The mutation job holding this workspace, if any."""
