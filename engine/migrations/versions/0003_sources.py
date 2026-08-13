"""Declared sources.

The last thing that forced a local file on an install that had moved everything
else. Deliberately still credential-free: a row holds the *name* of the
environment variable a connection string is read from, never the string, so
this table is as safe to dump as the YAML file it replaces.

No foreign key from `workspaces.source_id`. The binding is checked in
`atlas.catalog` under one lock, and a constraint here would turn "this source
still has workspaces" — which the API answers with a list of them — into an
integrity error with nothing useful in it.

Revision ID: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("adapter", sa.Text, nullable=False),
        # The variable's name. Never its value.
        sa.Column("url_env", sa.Text, nullable=False),
        sa.Column("namespace", sa.Text, nullable=False, server_default="public"),
        sa.Column("label", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("sources")
