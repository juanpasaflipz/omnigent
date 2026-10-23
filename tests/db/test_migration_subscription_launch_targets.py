"""Tests for the subscriptions v2 migration (launch targets, ``e2f3a4b5c6d7``)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_V1 = "e1f2a3b4c5d6"
_V2 = "e2f3a4b5c6d7"
_LAUNCH_COLUMNS = {
    "agent_id",
    "prompt_template",
    "execution_target",
    "host_id",
    "workspace",
    "max_cost_usd",
    "model_override",
    "account_generation",
}


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


def _columns(engine: sa.Engine) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("subscriptions")}


def _insert(engine: sa.Engine, *, conversation: bool, agent: bool) -> None:
    columns = [
        "workspace_id",
        "id",
        "source",
        "kind",
        "match",
        "state",
        "fire_count",
        "created_at",
    ]
    values: dict[str, object] = {
        "workspace_id": 0,
        "id": uuid.uuid4().bytes,
        "source": "github",
        "kind": "push",
        "match": "{}",
        "state": "active",
        "fire_count": 0,
        "created_at": 1,
    }
    if conversation:
        columns.append("conversation_id")
        values["conversation_id"] = uuid.uuid4().bytes
    if agent:
        columns.append("agent_id")
        values["agent_id"] = uuid.uuid4().bytes
    sql = (
        f"INSERT INTO subscriptions ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)})"
    )
    with engine.begin() as connection:
        connection.execute(sa.text(sql), values)


def _count(engine: sa.Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text("SELECT COUNT(*) FROM subscriptions")).scalar_one()


def test_upgrade_adds_launch_columns_keeps_v1_rows_and_downgrade_reverts(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'subscriptions-v2.db'}"
    engine = sa.create_engine(uri)

    _run(uri, engine, "upgrade", _V1)
    _insert(engine, conversation=True, agent=False)

    _run(uri, engine, "upgrade", _V2)
    columns = _columns(engine)
    assert set(columns) >= _LAUNCH_COLUMNS
    assert all(columns[name]["nullable"] for name in _LAUNCH_COLUMNS)
    assert columns["conversation_id"]["nullable"] is True
    assert _count(engine) == 1

    # A launch row is accepted; a row with no target or both targets is not.
    _insert(engine, conversation=False, agent=True)
    for conversation, agent in ((False, False), (True, True)):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, conversation=conversation, agent=agent)
    assert _count(engine) == 2

    _run(uri, engine, "downgrade", _V1)
    columns = _columns(engine)
    assert not _LAUNCH_COLUMNS & set(columns)
    assert columns["conversation_id"]["nullable"] is False
    # The launch row had no conversation to keep; the v1 row survives.
    assert _count(engine) == 1

    engine.dispose()
    clear_engine_cache()
