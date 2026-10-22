"""Subscription store — persists sessions' interest in external events.

A subscription ties one conversation to events of one ``(source, kind)`` that
match a field filter. This store owns the ``subscriptions`` table; the event
bus (``omnigent.server.events.bus``) is its hot-path reader via
:meth:`SubscriptionStore.list_active`.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Subscription

# Sentinel meaning "caller did not supply this argument; leave the column unchanged."
# Distinct from None, which means "set the column to NULL."
_UNSET: Any = object()


class SubscriptionStore(ABC):
    """
    Abstract base for subscription persistence.

    Manages subscription CRUD plus the two writes the event bus performs:
    :meth:`record_fire` after a delivery and :meth:`update_state` for
    pause/resume.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the subscription store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        subscription_id: str,
        *,
        user_id: str | None,
        conversation_id: str,
        source: str,
        kind: str,
        match: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> Subscription:
        """
        Insert a new active subscription.

        :param subscription_id: Pre-generated unique id (bare 32-char hex UUID).
        :param user_id: Owner the wake is delivered as; ``None`` in single-user mode.
        :param conversation_id: Session the wake notice is posted into.
        :param source: Event source, e.g. ``"github"`` or ``"webhook"``.
        :param kind: Normalized event kind, e.g. ``"check_run.completed"``.
        :param match: Field filter the event must satisfy; ``None`` means any event.
        :param note: Optional label echoed in the wake notice.
        :returns: The newly created :class:`Subscription`.
        """
        ...

    @abstractmethod
    def get(self, subscription_id: str) -> Subscription | None:
        """
        Return a subscription by id, or ``None`` if not found.

        :param subscription_id: Opaque subscription identifier.
        :returns: The :class:`Subscription` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_for_user(self, user_id: str | None) -> builtins.list[Subscription]:
        """
        List a user's non-deleted subscriptions ordered by ``created_at ASC, id ASC``.

        :param user_id: Owner to filter by; ``None`` lists single-user rows.
        :returns: List of :class:`Subscription` instances.
        """
        ...

    @abstractmethod
    def list_active(self, source: str, kind: str) -> builtins.list[Subscription]:
        """
        List active subscriptions for one ``(source, kind)`` across every workspace.

        The event bus runs outside any request workspace scope, so this read
        deliberately ignores ``current_workspace_id()``; each returned row
        carries its own ``workspace_id`` for the delivery path.

        :param source: Event source to match.
        :param kind: Event kind to match.
        :returns: Active subscriptions ordered by ``workspace_id, created_at, id``.
        """
        ...

    @abstractmethod
    def update(
        self,
        subscription_id: str,
        *,
        state: str | None = None,
        note: str | None = _UNSET,
        match: dict[str, Any] | None = None,
    ) -> Subscription | None:
        """
        Update mutable fields of a subscription.

        ``None`` leaves ``state`` and ``match`` unchanged. For ``note`` the
        sentinel default means "leave unchanged" and an explicit ``None``
        clears it. Returns ``None`` if the row does not exist or is deleted.

        :param subscription_id: Opaque subscription identifier.
        :param state: New lifecycle state (``active``/``paused``).
        :param note: New label, or ``None`` to clear it.
        :param match: Replacement field filter.
        :returns: The updated :class:`Subscription`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def update_state(self, subscription_id: str, state: str) -> Subscription | None:
        """
        Set the lifecycle state of a subscription.

        :param subscription_id: Opaque subscription identifier.
        :param state: One of ``active``/``paused``/``deleted``.
        :returns: The updated :class:`Subscription`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete(self, subscription_id: str) -> bool:
        """
        Soft-delete a subscription. Idempotent.

        :param subscription_id: Opaque subscription identifier.
        :returns: ``True`` if newly deleted; ``False`` if missing or already deleted.
        """
        ...

    @abstractmethod
    def record_fire(self, subscription_id: str, fired_at: int) -> Subscription | None:
        """
        Stamp a delivery: set ``last_fired_at`` and increment ``fire_count``.

        Matches on id alone (not the request workspace) because the event bus
        calls it for rows from any workspace.

        :param subscription_id: Opaque subscription identifier.
        :param fired_at: Unix epoch seconds of the delivery.
        :returns: The updated :class:`Subscription`, or ``None`` if not found.
        """
        ...
