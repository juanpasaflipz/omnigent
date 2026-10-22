"""Subscription entity — persisted in the ``subscriptions`` table.

A :class:`Subscription` is a saved interest of one session in events of one
``(source, kind)`` that match a field filter. When a matching event reaches the
server's event bus, a wake notice is posted into the subscribed conversation,
exactly like a timer firing. This module holds the plain dataclass the store
converts ORM rows into; the store owns the JSON (de)serialization of the
Text-backed ``match`` column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Event sources the bus accepts. ``github`` rows are fed by the signed
# ``POST /v1/events/github`` ingest; ``webhook`` rows by the bearer-authed
# generic ``POST /v1/events/webhook``.
SUBSCRIPTION_SOURCES: frozenset[str] = frozenset({"github", "webhook"})


class SubscriptionState(StrEnum):
    """Lifecycle state of a :class:`Subscription`."""

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


@dataclass
class Subscription:
    """
    A subscription persisted in the ``subscriptions`` table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param user_id: Owner the wake notice is delivered as, e.g.
        ``"alice@example.com"``. ``None`` in single-user mode.
    :param workspace_id: Tenant partition key that owns this row.
    :param conversation_id: Session the wake notice is posted into.
    :param source: Event source, one of :data:`SUBSCRIPTION_SOURCES`.
    :param kind: Normalized event kind, e.g. ``"check_run.completed"``.
    :param match: Field filter; every key must be present in the event's
        fields with an equal value for the subscription to fire.
    :param note: Optional human-readable label echoed in the wake notice.
    :param state: Lifecycle state — one of :class:`SubscriptionState`.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    :param last_fired_at: Unix epoch seconds of the most recent delivery,
        or ``None`` if it has never fired. Drives the per-subscription
        cooldown.
    :param fire_count: Number of deliveries so far.
    """

    id: str
    user_id: str | None
    conversation_id: str
    source: str
    kind: str
    created_at: int
    workspace_id: int = 0
    match: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    state: str = SubscriptionState.ACTIVE.value
    updated_at: int | None = None
    last_fired_at: int | None = None
    fire_count: int = 0
