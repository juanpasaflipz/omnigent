"""Deliver event wake notices through the server's own session-event path.

A wake notice is posted the same way a timer firing or a scheduled-task prompt
is: as a hidden (``is_meta``) user message that the normal
``POST /v1/sessions/{id}/events`` logic persists and forwards to the bound
runner, starting a turn if the session is idle. Instead of HTTP-ing the server
from inside the server, the events router hands its ``_post_event_impl``
closure out through ``register_startup_dispatch``; :func:`build_wake_delivery`
calls it directly as the subscription's owner.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.datastructures import Headers

from omnigent.db.db_models import workspace_scope
from omnigent.entities import Subscription
from omnigent.server.events.bus import WakeDelivery
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import StartupDispatch, StartupInputContext
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)


class EventWakeContext(StartupInputContext):
    """
    Sender identity for a wake notice posted by the event bus.

    Reuses the startup-input context the session-event logic already accepts
    for server-originated input (identity without a live browser request),
    but never claims a pending-input row: a wake notice is not a queued
    startup prompt, so :meth:`claim` always lets dispatch proceed.
    """

    async def claim(self, store: ConversationStore, session_id: str) -> bool:
        """
        Let the dispatch proceed without touching pending-input rows.

        :param store: Unused; present to match the base signature.
        :param session_id: Unused; present to match the base signature.
        :returns: Always ``True``.
        """
        del store, session_id
        self.submitted = True
        return True


def wake_event(text: str) -> SessionEventInput:
    """
    Build the hidden meta user message that carries a wake notice.

    :param text: The notice text from ``format_wake_notice``.
    :returns: The event to submit to the session.
    """
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "is_meta": True,
            "content": [{"type": "input_text", "text": text}],
        },
    )


def build_wake_delivery(app: Any) -> WakeDelivery:
    """
    Build the bus's delivery callable bound to a FastAPI app.

    The session-event dispatcher is looked up on ``app.state`` at call time
    (``session_event_dispatch``), because the sessions router registers it
    after the bus is constructed.

    :param app: The FastAPI application whose sessions router handles events.
    :returns: An ``async deliver(subscription, text)`` for :class:`EventBus`.
    """

    async def deliver(subscription: Subscription, text: str) -> None:
        dispatch: StartupDispatch | None = getattr(app.state, "session_event_dispatch", None)
        if dispatch is None:
            raise RuntimeError("session event dispatch is not registered on the app")
        context = EventWakeContext(
            app=app,
            headers=Headers({}),
            user_id=subscription.user_id,
            input_id=f"evt_{uuid.uuid4().hex}",
        )
        # The bus runs outside any request, so scope store reads to the
        # subscription's own tenant for the duration of the dispatch.
        with workspace_scope(subscription.workspace_id):
            await dispatch(context, subscription.conversation_id, wake_event(text))

    return deliver
