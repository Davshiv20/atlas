"""What a background job is, independent of where its status is kept.

Separate from both the store port and the registry that runs the work, because
all three need these and none of them owns them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

#: How many finished jobs are kept. The history is an operational aid, not a
#: record — what a run produced is in the workspace, not here.
MAX_RETAINED_JOBS = 500


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

#: The kinds that mutate a workspace, and so may not overlap on one. Reading
#: jobs are not in here because two of them racing costs nothing.
EXCLUSIVE_KINDS = ("extract", "analyze")


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
