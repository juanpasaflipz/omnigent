"""SQLAlchemy-backed subscription store."""

from __future__ import annotations

import builtins
import json
import logging
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlSubscription,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Subscription, SubscriptionState
from omnigent.stores.subscription_store import SubscriptionStore

_logger = logging.getLogger(__name__)

# Sentinel meaning "caller did not supply this argument; leave the column unchanged."
# Distinct from None, which means "set the column to NULL."
_UNSET: Any = object()

_VALID_STATES = frozenset(state.value for state in SubscriptionState)


def _decode_match(raw: str | None) -> dict[str, Any]:
    """
    Decode the JSON ``match`` column into a dict, tolerating corrupt rows.

    :param raw: The stored JSON text, or ``None``.
    :returns: The decoded filter; an empty dict for empty or malformed text.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        _logger.warning("Ignoring malformed subscription match JSON: %r", raw[:200])
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlSubscription) -> Subscription:
    """
    Convert a :class:`SqlSubscription` ORM row to a :class:`Subscription`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Subscription` dataclass instance.
    """
    return Subscription(
        id=row.id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        source=row.source,
        kind=row.kind,
        created_at=row.created_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        match=_decode_match(row.match),
        note=row.note,
        state=row.state,
        updated_at=row.updated_at,
        last_fired_at=row.last_fired_at,
        fire_count=row.fire_count or 0,
        agent_id=row.agent_id,
        prompt_template=row.prompt_template,
        execution_target=row.execution_target,
        host_id=row.host_id,
        workspace=row.workspace,
        max_cost_usd=row.max_cost_usd,
        model_override=row.model_override,
        account_generation=row.account_generation,
    )


def _require_state(state: str) -> str:
    """
    Validate a lifecycle state name.

    :param state: Candidate state string.
    :returns: The same string when valid.
    :raises ValueError: If ``state`` is not a :class:`SubscriptionState`.
    """
    if state not in _VALID_STATES:
        raise ValueError(f"unknown subscription state {state!r}")
    return state


class SqlAlchemySubscriptionStore(SubscriptionStore):
    """SQLAlchemy implementation of :class:`SubscriptionStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy subscription store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.subscription_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.subscription_store",
            immediate=True,
        )

    def create(
        self,
        subscription_id: str,
        *,
        user_id: str | None,
        conversation_id: str | None = None,
        source: str,
        kind: str,
        match: dict[str, Any] | None = None,
        note: str | None = None,
        agent_id: str | None = None,
        prompt_template: str | None = None,
        execution_target: str | None = None,
        host_id: str | None = None,
        workspace: str | None = None,
        max_cost_usd: float | None = None,
        model_override: str | None = None,
    ) -> Subscription:
        """Insert a new active subscription targeting a conversation or an agent."""
        if (conversation_id is None) == (agent_id is None):
            raise ValueError("exactly one of conversation_id and agent_id must be set")
        launch = agent_id is not None
        created_at = now_epoch()

        def write(session: Session) -> Subscription:
            generation = require_active_account(session, user_id)
            row = SqlSubscription(
                id=subscription_id,
                user_id=user_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                prompt_template=prompt_template if launch else None,
                execution_target=(execution_target or "connected_host") if launch else None,
                host_id=host_id if launch else None,
                workspace=workspace if launch else None,
                max_cost_usd=max_cost_usd if launch else None,
                model_override=model_override if launch else None,
                account_generation=generation,
                source=source,
                kind=kind,
                match=json.dumps(match or {}, sort_keys=True),
                note=note,
                state=SubscriptionState.ACTIVE.value,
                last_fired_at=None,
                fire_count=0,
                created_at=created_at,
                updated_at=None,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_subscription", write)

    def get(self, subscription_id: str) -> Subscription | None:
        """Return a subscription by id, or ``None`` if not found."""
        with self._session("select_subscription_by_id") as session:
            row = session.get(SqlSubscription, (current_workspace_id(), subscription_id))
            if row is None:
                return None
            return _to_entity(row)

    def list_for_user(self, user_id: str | None) -> builtins.list[Subscription]:
        """List a user's non-deleted subscriptions ordered by ``created_at ASC, id ASC``."""
        with self._session("list_subscriptions_for_user") as session:
            stmt = (
                select(SqlSubscription)
                .where(SqlSubscription.workspace_id == current_workspace_id())
                .where(SqlSubscription.state != SubscriptionState.DELETED.value)
                .order_by(asc(SqlSubscription.created_at), asc(SqlSubscription.id))
            )
            # NULL never equals NULL, so single-user rows need an IS NULL test.
            if user_id is None:
                stmt = stmt.where(SqlSubscription.user_id.is_(None))
            else:
                stmt = stmt.where(SqlSubscription.user_id == user_id)
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(row) for row in rows]

    def list_active(self, source: str, kind: str) -> builtins.list[Subscription]:
        """List active subscriptions for one ``(source, kind)`` across every workspace."""
        with self._session("list_active_subscriptions_for_event") as session:
            stmt = (
                select(SqlSubscription)
                .where(SqlSubscription.state == SubscriptionState.ACTIVE.value)
                .where(SqlSubscription.source == source)
                .where(SqlSubscription.kind == kind)
                .order_by(
                    asc(SqlSubscription.workspace_id),
                    asc(SqlSubscription.created_at),
                    asc(SqlSubscription.id),
                )
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(row) for row in rows]

    def update(
        self,
        subscription_id: str,
        *,
        state: str | None = None,
        note: str | None = _UNSET,
        match: dict[str, Any] | None = None,
    ) -> Subscription | None:
        """Update mutable fields; ``None`` leaves state/match unchanged."""
        if state is not None:
            _require_state(state)
        updated_at = now_epoch()

        def write(session: Session) -> Subscription | None:
            row = session.get(
                SqlSubscription, (current_workspace_id(), subscription_id), with_for_update=True
            )
            if row is None or row.state == SubscriptionState.DELETED.value:
                return None
            changed = False
            if state is not None and row.state != state:
                row.state = state
                changed = True
            if note is not _UNSET and row.note != note:
                row.note = note
                changed = True
            if match is not None:
                encoded = json.dumps(match, sort_keys=True)
                if row.match != encoded:
                    row.match = encoded
                    changed = True
            if changed:
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_subscription", write)

    def update_state(self, subscription_id: str, state: str) -> Subscription | None:
        """Set the lifecycle state, including ``deleted``."""
        _require_state(state)
        updated_at = now_epoch()

        def write(session: Session) -> Subscription | None:
            row = session.get(
                SqlSubscription, (current_workspace_id(), subscription_id), with_for_update=True
            )
            if row is None:
                return None
            if row.state != state:
                row.state = state
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_subscription_state", write)

    def delete(self, subscription_id: str) -> bool:
        """Soft-delete a subscription. Idempotent."""

        def write(session: Session) -> bool:
            row = session.get(
                SqlSubscription, (current_workspace_id(), subscription_id), with_for_update=True
            )
            if row is None or row.state == SubscriptionState.DELETED.value:
                return False
            row.state = SubscriptionState.DELETED.value
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session_immediate, "delete_subscription", write)

    def record_fire(self, subscription_id: str, fired_at: int) -> Subscription | None:
        """Stamp a delivery on the row regardless of the request workspace."""

        def write(session: Session) -> Subscription | None:
            stmt = (
                select(SqlSubscription)
                .where(SqlSubscription.id == subscription_id)
                .with_for_update()
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                return None
            row.last_fired_at = fired_at
            row.fire_count = (row.fire_count or 0) + 1
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "record_subscription_fire", write)
