"""Let a subscription launch a new session instead of waking an existing one.

Revision ID: e2f3a4b5c6d7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-22 00:00:00.000000

Subscriptions v2: a row now targets either an existing ``conversation_id``
(v1 wake notice) or an ``agent_id`` + ``prompt_template`` that the event bus
launches as a fresh session through the scheduled-task fire path. The launch
columns mirror their ``scheduled_tasks`` counterparts (``execution_target``,
``host_id``, ``workspace``, ``max_cost_usd``, ``model_override``,
``account_generation``), all nullable so v1 rows are untouched.

``conversation_id`` becomes nullable and ``ck_subscriptions_one_target``
enforces that exactly one of ``conversation_id`` / ``agent_id`` is set; every
existing row has a conversation and no agent, so it already satisfies the
check. Batch mode keeps the change SQLite-safe (table recreate).

Downgrade deletes launch rows (they have no conversation to keep) before
restoring ``conversation_id`` to NOT NULL and dropping the new columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ONE_TARGET = "(conversation_id IS NULL) <> (agent_id IS NULL)"
_EXECUTION_TARGET = (
    "execution_target IS NULL OR execution_target IN ('connected_host', 'managed_sandbox')"
)


def upgrade() -> None:
    """Add the launch-target columns and the exactly-one-target check."""
    with op.batch_alter_table("subscriptions") as batch:
        batch.alter_column("conversation_id", existing_type=Uuid16(), nullable=True)
        # Relates to agents.id (no DB FK, Rule R032).
        batch.add_column(sa.Column("agent_id", Uuid16(), nullable=True))
        batch.add_column(sa.Column("prompt_template", sa.Text(), nullable=True))
        batch.add_column(sa.Column("execution_target", sa.String(32), nullable=True))
        # Relates to hosts.host_id (no DB FK, Rule R032).
        batch.add_column(sa.Column("host_id", Uuid16(), nullable=True))
        batch.add_column(sa.Column("workspace", sa.String(2048), nullable=True))
        batch.add_column(sa.Column("max_cost_usd", sa.Float(), nullable=True))
        batch.add_column(sa.Column("model_override", sa.String(128), nullable=True))
        batch.add_column(sa.Column("account_generation", sa.String(32), nullable=True))
        batch.create_check_constraint("ck_subscriptions_one_target", _ONE_TARGET)
        batch.create_check_constraint("ck_subscriptions_execution_target", _EXECUTION_TARGET)


def downgrade() -> None:
    """Drop launch subscriptions and the launch columns; require a conversation again."""
    subscriptions = sa.table("subscriptions", sa.column("conversation_id"))
    op.execute(subscriptions.delete().where(subscriptions.c.conversation_id.is_(None)))
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_constraint("ck_subscriptions_execution_target", type_="check")
        batch.drop_constraint("ck_subscriptions_one_target", type_="check")
        batch.drop_column("account_generation")
        batch.drop_column("model_override")
        batch.drop_column("max_cost_usd")
        batch.drop_column("workspace")
        batch.drop_column("host_id")
        batch.drop_column("execution_target")
        batch.drop_column("prompt_template")
        batch.drop_column("agent_id")
        batch.alter_column("conversation_id", existing_type=Uuid16(), nullable=False)
