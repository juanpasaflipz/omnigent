"""Add the subscriptions table.

Revision ID: e1f2a3b4c5d6
Revises: c8e4f2a6b901
Create Date: 2026-10-22 00:00:00.000000

Adds the ``subscriptions`` table: one row per session interest in external
events of a ``(source, kind)`` that satisfy a JSON field filter. The event bus
reads active rows by ``(state, source, kind)`` on every published event, so
that lookup gets its own index.

Brand-new table created at the current schema state: tenant-partition
``workspace_id`` leads the primary key and there are no foreign-key constraints
(schema Rule R032) — ``conversation_id`` is enforced by the application.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``subscriptions`` table and its indexes."""
    op.create_table(
        "subscriptions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PK stored as 16 raw bytes (Uuid16 → BINARY(16) on MySQL, BLOB/BYTEA
        # elsewhere).
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        # Relates to conversations.id (no DB FK, Rule R032).
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(128), nullable=False),
        # JSON object text; matching happens in the bus, never in SQL.
        sa.Column("match", sa.Text(), nullable=False),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_fired_at", sa.BigInteger(), nullable=True),
        sa.Column("fire_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'deleted')", name="ck_subscriptions_state"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_subscriptions_active_lookup",
        "subscriptions",
        ["state", "source", "kind"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_user_scope",
        "subscriptions",
        ["workspace_id", "user_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``subscriptions`` table."""
    op.drop_index("ix_subscriptions_user_scope", table_name="subscriptions")
    op.drop_index("ix_subscriptions_active_lookup", table_name="subscriptions")
    op.drop_table("subscriptions")
