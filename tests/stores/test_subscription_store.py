"""Tests for :class:`SqlAlchemySubscriptionStore`.

Exercises ``create``, ``get``, ``list_for_user``, ``list_active``, ``update``,
``update_state``, ``delete`` and ``record_fire`` against a real SQLite
database, including the cross-workspace read the event bus relies on.
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.subscription_store.sqlalchemy_store import SqlAlchemySubscriptionStore


def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySubscriptionStore:
    """
    A fresh :class:`SqlAlchemySubscriptionStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use store instance.
    """
    return SqlAlchemySubscriptionStore(db_uri)


def _create(
    store: SqlAlchemySubscriptionStore,
    seed: str,
    *,
    user_id: str | None = "alice@example.com",
    source: str = "github",
    kind: str = "check_run.completed",
    match: dict[str, object] | None = None,
) -> str:
    return store.create(
        _uid(seed),
        user_id=user_id,
        conversation_id=_uid(f"conv_{seed}"),
        source=source,
        kind=kind,
        match=match if match is not None else {"repo": "acme/api", "pull_request": 412},
        note="CI on my PR",
    ).id


# ── create / get ─────────────────────────────────────────────────────────────


def test_create_round_trips_every_field(store: SqlAlchemySubscriptionStore) -> None:
    subscription_id = _create(store, "sub_1")
    subscription = store.get(subscription_id)

    assert subscription is not None
    assert subscription.id == _uid("sub_1")
    assert subscription.workspace_id == 0
    assert subscription.user_id == "alice@example.com"
    assert subscription.conversation_id == _uid("conv_sub_1")
    assert subscription.source == "github"
    assert subscription.kind == "check_run.completed"
    assert subscription.match == {"repo": "acme/api", "pull_request": 412}
    assert subscription.note == "CI on my PR"
    assert subscription.state == "active"
    assert subscription.created_at > 0
    assert subscription.updated_at is None
    assert subscription.last_fired_at is None
    assert subscription.fire_count == 0


def test_get_unknown_returns_none(store: SqlAlchemySubscriptionStore) -> None:
    assert store.get(_uid("missing")) is None


# ── list_for_user / list_active ──────────────────────────────────────────────


def test_list_for_user_filters_owner_and_hides_deleted(
    store: SqlAlchemySubscriptionStore,
) -> None:
    first = _create(store, "a1")
    second = _create(store, "a2")
    _create(store, "b1", user_id="bob@example.com")
    local = _create(store, "l1", user_id=None)
    store.delete(second)

    assert [row.id for row in store.list_for_user("alice@example.com")] == [first]
    # Single-user rows have a NULL owner and need an IS NULL match.
    assert [row.id for row in store.list_for_user(None)] == [local]


def test_list_active_scopes_by_source_kind_and_state(
    store: SqlAlchemySubscriptionStore,
) -> None:
    active = _create(store, "active")
    paused = _create(store, "paused")
    _create(store, "other_kind", kind="push")
    _create(store, "other_source", source="webhook")
    store.update_state(paused, "paused")

    assert [row.id for row in store.list_active("github", "check_run.completed")] == [active]
    assert store.list_active("github", "nothing") == []


def test_list_active_spans_workspaces_but_get_does_not(
    store: SqlAlchemySubscriptionStore,
) -> None:
    default_ws = _create(store, "ws0")
    with workspace_scope(7):
        tenant_ws = _create(store, "ws7")

    active_ids = [row.id for row in store.list_active("github", "check_run.completed")]
    assert active_ids == [default_ws, tenant_ws]
    assert store.get(tenant_ws) is None
    with workspace_scope(7):
        row = store.get(tenant_ws)
        assert row is not None
        assert row.workspace_id == 7


# ── update / update_state / delete ───────────────────────────────────────────


def test_update_changes_state_note_and_match(store: SqlAlchemySubscriptionStore) -> None:
    subscription_id = _create(store, "u1")

    updated = store.update(subscription_id, state="paused", note=None, match={"repo": "acme/web"})

    assert updated is not None
    assert updated.state == "paused"
    assert updated.note is None
    assert updated.match == {"repo": "acme/web"}
    assert updated.updated_at is not None
    # Omitted note (sentinel) leaves the cleared value alone.
    assert store.update(subscription_id, state="active").note is None


def test_update_rejects_unknown_state_and_deleted_rows(
    store: SqlAlchemySubscriptionStore,
) -> None:
    subscription_id = _create(store, "u2")
    with pytest.raises(ValueError, match="unknown subscription state"):
        store.update(subscription_id, state="bogus")
    store.delete(subscription_id)
    assert store.update(subscription_id, state="paused") is None
    assert store.update(_uid("missing"), state="paused") is None


def test_delete_is_soft_and_idempotent(store: SqlAlchemySubscriptionStore) -> None:
    subscription_id = _create(store, "d1")

    assert store.delete(subscription_id) is True
    assert store.delete(subscription_id) is False
    assert store.delete(_uid("missing")) is False
    row = store.get(subscription_id)
    assert row is not None
    assert row.state == "deleted"


# ── record_fire ──────────────────────────────────────────────────────────────


def test_record_fire_stamps_and_counts_across_workspaces(
    store: SqlAlchemySubscriptionStore,
) -> None:
    with workspace_scope(3):
        subscription_id = _create(store, "f1")

    # The bus calls this outside any workspace scope.
    first = store.record_fire(subscription_id, 1_700_000_000)
    second = store.record_fire(subscription_id, 1_700_000_010)

    assert first is not None and second is not None
    assert (first.last_fired_at, first.fire_count) == (1_700_000_000, 1)
    assert (second.last_fired_at, second.fire_count) == (1_700_000_010, 2)
    assert store.record_fire(_uid("missing"), 1) is None


def test_malformed_match_json_decodes_to_empty_filter(
    store: SqlAlchemySubscriptionStore, db_uri: str
) -> None:
    import sqlalchemy as sa

    from omnigent.db.db_models import SqlSubscription
    from omnigent.db.utils import get_or_create_engine

    subscription_id = _create(store, "m1")
    engine = get_or_create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(
            sa.update(SqlSubscription)
            .where(SqlSubscription.id == subscription_id)
            .values(match="{not json")
        )

    row = store.get(subscription_id)
    assert row is not None
    assert row.match == {}
