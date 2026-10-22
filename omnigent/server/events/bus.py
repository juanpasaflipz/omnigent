"""Event bus: match canonical events against subscriptions and wake sessions.

A :class:`CanonicalEvent` is what every ingest route produces after
normalization (``source`` + ``kind`` + flat ``fields``). :meth:`EventBus.publish`
loads the active subscriptions for the event's ``(source, kind)``, applies each
subscription's ``match`` as a subset test over ``fields``, and hands a wake
notice to the injected delivery callable for every match that is outside its
cooldown window. Delivery runs inside the server process, so the callable is
expected to append a hidden meta user message to the conversation the same way
a timer firing does.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from omnigent.entities import Subscription
from omnigent.stores.subscription_store import SubscriptionStore

_logger = logging.getLogger(__name__)

# Upper bound on the JSON payload echoed in a wake notice so a large webhook
# body cannot flood the session's context.
WAKE_NOTICE_FIELDS_MAX_CHARS = 4_000

# Default per-subscription quiet period: a flapping CI job re-firing every few
# seconds must not post a wake notice per flap.
DEFAULT_COOLDOWN_SECONDS = 10

#: Delivers one wake notice into the subscription's conversation. Takes the
#: subscription (which carries the conversation, owner and workspace) and the
#: notice text. Errors are logged by the bus and never propagate.
WakeDelivery = Callable[[Subscription, str], Awaitable[None]]


@dataclass(frozen=True)
class CanonicalEvent:
    """
    One normalized external event.

    :param source: Where the event came from, e.g. ``"github"`` or ``"webhook"``.
    :param kind: Normalized kind, e.g. ``"check_run.completed"`` or ``"push"``.
    :param fields: Flat scalar fields subscriptions match against, e.g.
        ``{"repo": "acme/api", "conclusion": "failure", "pull_request": 412}``.
    :param received_at: Unix epoch seconds the server accepted the event.
    """

    source: str
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    received_at: int = field(default_factory=lambda: int(time.time()))


@dataclass(frozen=True)
class PublishResult:
    """
    Outcome of one :meth:`EventBus.publish` call.

    :param matched: Subscriptions whose ``match`` filter the event satisfied.
    :param delivered: Matches whose wake notice was handed to delivery.
    :param suppressed: Matches skipped because they fired within the cooldown.
    """

    matched: int = 0
    delivered: int = 0
    suppressed: int = 0


def _values_equal(expected: Any, actual: Any) -> bool:
    """
    Compare a filter value with an event field value.

    Ints and strs compare as strings so a filter written as ``412`` still
    matches a webhook that serializes the number as ``"412"`` (and vice
    versa). Everything else must be equal as-is.

    :param expected: The subscription's filter value.
    :param actual: The event's field value.
    :returns: Whether the two count as equal for matching.
    """
    if expected == actual:
        return True
    scalar_types = (int, str)
    if (
        isinstance(expected, scalar_types)
        and isinstance(actual, scalar_types)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
    ):
        return str(expected) == str(actual)
    return False


def event_matches(match: dict[str, Any], fields: dict[str, Any]) -> bool:
    """
    Subset test: every filter key must be present in ``fields`` with an equal value.

    :param match: The subscription's filter; empty matches every event.
    :param fields: The event's fields.
    :returns: ``True`` when the event satisfies the filter.
    """
    for key, expected in match.items():
        if key not in fields:
            return False
        if not _values_equal(expected, fields[key]):
            return False
    return True


def format_wake_notice(event: CanonicalEvent, subscription: Subscription) -> str:
    """
    Build the hidden meta message posted into a subscribed session.

    Mirrors the ``[System: timer X fired]`` shape so the model treats it as a
    runtime status message. The fields payload is compact JSON truncated to
    :data:`WAKE_NOTICE_FIELDS_MAX_CHARS`.

    :param event: The event that matched.
    :param subscription: The subscription it matched.
    :returns: The notice text.
    """
    header = (
        f"[System: event {event.source}/{event.kind} matched subscription "
        f"{subscription.id} ({subscription.note!r})]"
    )
    payload = json.dumps(event.fields, separators=(",", ":"), sort_keys=True, default=str)
    if len(payload) > WAKE_NOTICE_FIELDS_MAX_CHARS:
        payload = payload[:WAKE_NOTICE_FIELDS_MAX_CHARS] + "…[truncated]"
    return f"{header}\n{payload}"


class EventBus:
    """
    Routes canonical events to subscribed sessions.

    :param store: Subscription persistence; ``list_active`` is the hot path and
        ``record_fire`` stamps each delivery.
    :param deliver: Posts a wake notice into a subscription's conversation.
    :param cooldown_seconds: Minimum seconds between two deliveries to the same
        subscription; matches inside the window are counted as suppressed.
    :param clock: Epoch-seconds source, injectable for tests.
    """

    def __init__(
        self,
        store: SubscriptionStore,
        deliver: WakeDelivery,
        *,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._deliver = deliver
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock

    async def publish(self, event: CanonicalEvent) -> PublishResult:
        """
        Deliver ``event`` to every active subscription it matches.

        Delivery failures are logged per subscription and never raised, so one
        unreachable session cannot block the others or fail the ingest request.

        :param event: The normalized event to route.
        :returns: Match / delivery / suppression counts.
        """
        subscriptions = self._store.list_active(event.source, event.kind)
        matched = delivered = suppressed = 0
        for subscription in subscriptions:
            if not event_matches(subscription.match, event.fields):
                continue
            matched += 1
            now = int(self._clock())
            if (
                subscription.last_fired_at is not None
                and now - subscription.last_fired_at < self._cooldown_seconds
            ):
                suppressed += 1
                continue
            # Stamp before delivering so a concurrent duplicate event sees the
            # cooldown even if delivery is slow.
            self._store.record_fire(subscription.id, now)
            try:
                await self._deliver(subscription, format_wake_notice(event, subscription))
            except Exception:
                _logger.exception(
                    "Wake delivery failed for subscription %s (conversation %s)",
                    subscription.id,
                    subscription.conversation_id,
                    extra={"session_id": subscription.conversation_id},
                )
                continue
            delivered += 1
        return PublishResult(matched=matched, delivered=delivered, suppressed=suppressed)
