"""Job status, in the same database as the record.

Not part of the record. What a run produced is written into the workspace as
each table completes; this is only what the run is *doing*, so losing it costs
the progress indicator and nothing else.

It is here anyway because an install pointed at PostgreSQL should keep nothing
on local disk, and because the exclusivity check wants a real arbiter: two
extracts submitted at the same instant both saw an idle workspace, and a
process-wide lock stops helping the moment there are two processes.

Deliberately not foreign-keyed to `workspaces`. A job outlives the workspace it
ran against — "this is why that workspace is gone" is exactly the history worth
keeping for a moment — and the registry deletes them explicitly instead.

Revision ID: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        # Documents, because the console renders them whole and the pydantic
        # model stays the single definition of their shape.
        sa.Column("progress", postgresql.JSONB),
        sa.Column("result", postgresql.JSONB),
        sa.Column("error", sa.Text),
        # What the workspace looked like when the run was submitted. A run that
        # takes minutes can finish into a workspace that has been refreshed or
        # rebound underneath it, and these are how that is noticed.
        sa.Column("snapshot_generation", sa.Integer),
        sa.Column("source_id", sa.Text),
        sa.Column("workspace_incarnation", sa.Text),
    )
    # The console's poll: this workspace's jobs, newest first.
    op.create_index("ix_jobs_workspace", "jobs", ["workspace", "created_at"])
    # The exclusivity check, run on every submission.
    op.create_index("ix_jobs_live", "jobs", ["workspace", "status"])
    # Retention, which orders by when a job finished.
    op.create_index("ix_jobs_finished", "jobs", ["finished_at"])


def downgrade() -> None:
    op.drop_table("jobs")
