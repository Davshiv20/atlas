"""The semantic record: workspaces, snapshot generations, and claims.

Two storage shapes, chosen by whether the thing is ever edited in place.

A *snapshot* is one immutable capture of a physical schema. Nothing updates a
column of it; a new capture is a new generation. It is stored as one JSONB
document, which keeps a generation exactly reproducible and keeps this schema
from having to mirror every field the extractor learns to collect.

A *claim*, *question*, or *evidence record* is edited one at a time by people
working the same workspace at once. Those get a row each, so a reviewer settling
one claim writes one row and cannot carry a stale copy of anyone else's. The
document stays in JSONB — the model owns its own shape — and the few fields
that get filtered or ordered on are lifted into columns beside it.

Everything semantic is scoped to a generation. Claims are about columns that a
re-extraction may have removed, so they do not follow a workspace across a
refresh, and the composite primary keys are what make that structural rather
than a rule someone has to remember.

Revision ID: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        # Immutable once set. A workspace's claims are about one database, and
        # rebinding would leave every stored claim describing something else.
        sa.Column("source_id", sa.Text, nullable=False),
        # Distinguishes this workspace from a deleted one that had the same
        # name, so a job that started minutes ago cannot publish into its
        # replacement.
        sa.Column("incarnation_id", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # The single active-generation pointer. Zero means created but never
        # extracted.
        sa.Column("snapshot_generation", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("snapshot_generation >= 0", name="ck_workspaces_generation"),
    )
    op.create_index("ix_workspaces_source", "workspaces", ["source_id"])

    op.create_table(
        "snapshots",
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("workspace", "generation"),
        sa.ForeignKeyConstraint(["workspace"], ["workspaces.name"], ondelete="CASCADE"),
        sa.CheckConstraint("generation > 0", name="ck_snapshots_generation"),
    )

    op.create_table(
        "claims",
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        # `subject#aspect` or `subject#aspect#discriminator`, assigned by the
        # model. The store does not mint it: an id a claim is addressed by must
        # be the same in every store.
        sa.Column("claim_id", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("aspect", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("consequence", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.PrimaryKeyConstraint("workspace", "generation", "claim_id"),
        sa.ForeignKeyConstraint(["workspace"], ["workspaces.name"], ondelete="CASCADE"),
    )
    # The review queue: consequential and unsettled first. Ordering in SQL is
    # the point of storing claims as rows rather than a document.
    op.create_index(
        "ix_claims_queue", "claims", ["workspace", "generation", "consequence", "confidence"]
    )
    # Regeneration drops a table's claims, and a table is the first dotted
    # segment of the subject.
    op.create_index("ix_claims_subject", "claims", ["workspace", "generation", "subject"])

    op.create_table(
        "questions",
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("question_id", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        # Named `relation` rather than `table`, which is reserved.
        sa.Column("relation", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.PrimaryKeyConstraint("workspace", "generation", "question_id"),
        sa.ForeignKeyConstraint(["workspace"], ["workspaces.name"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_questions_relation", "questions", ["workspace", "generation", "relation"]
    )

    op.create_table(
        "evidence_records",
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        # Content-addressed by the model: the same observation over the same
        # data is the same id, which is what makes re-adding one a no-op
        # instead of a duplicate.
        sa.Column("record_id", sa.Text, nullable=False),
        sa.Column("subjects", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.PrimaryKeyConstraint("workspace", "generation", "record_id"),
        sa.ForeignKeyConstraint(["workspace"], ["workspaces.name"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_evidence_subjects",
        "evidence_records",
        ["subjects"],
        postgresql_using="gin",
    )

    op.create_table(
        "evidence_links",
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("claim_id", sa.Text, nullable=False),
        sa.Column("record_id", sa.Text, nullable=False),
        sa.Column("relationship", sa.Text, nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        # What makes two links the same link. Not the rationale: that is prose
        # the writer chose, and two callers wording it differently are still
        # saying this record bears on this claim in this way.
        sa.PrimaryKeyConstraint(
            "workspace", "generation", "claim_id", "record_id", "relationship"
        ),
        sa.ForeignKeyConstraint(["workspace"], ["workspaces.name"], ondelete="CASCADE"),
        # A link to a record that is gone cites nothing. The cascade is what
        # stops a dangling one outliving the observation it points at.
        sa.ForeignKeyConstraint(
            ["workspace", "generation", "record_id"],
            ["evidence_records.workspace", "evidence_records.generation",
             "evidence_records.record_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_links_claim", "evidence_links", ["workspace", "generation", "claim_id"]
    )


def downgrade() -> None:
    op.drop_table("evidence_links")
    op.drop_table("evidence_records")
    op.drop_table("questions")
    op.drop_table("claims")
    op.drop_table("snapshots")
    op.drop_index("ix_workspaces_source", table_name="workspaces")
    op.drop_table("workspaces")
