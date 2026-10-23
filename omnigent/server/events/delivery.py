"""Deliver event wake notices through the server's own session-event path.

A wake notice is posted the same way a timer firing or a scheduled-task prompt
is: as a hidden (``is_meta``) user message that the normal
``POST /v1/sessions/{id}/events`` logic persists and forwards to the bound
runner, starting a turn if the session is idle. Instead of HTTP-ing the server
from inside the server, the events router hands its ``_post_event_impl``
closure out through ``register_startup_dispatch``; :func:`build_wake_delivery`
calls it directly as the subscription's owner.

A launch subscription instead starts a fresh session: :func:`build_session_launcher`
turns it into a :class:`~omnigent.server.scheduled.fire.LaunchRequest` and
hands it to the scheduled-task fire path, which owns target resolution,
session creation, the owner grant, the cost cap and the runner launch.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.datastructures import Headers

from omnigent.db.db_models import workspace_scope
from omnigent.entities import Subscription
from omnigent.server.events.bus import SessionLaunch, WakeDelivery
from omnigent.server.scheduled.fire import LaunchRequest, start_session_launch
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import StartupDispatch, StartupInputContext
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

# Labels stamped on a session a subscription launched, so it is traceable.
LAUNCHED_BY_LABEL = "omnigent.launched_by"
SUBSCRIPTION_LABEL = "omnigent.subscription"
EVENT_LABEL = "omnigent.event"


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
        conversation_id = subscription.conversation_id
        if conversation_id is None:
            raise ValueError(
                f"subscription {subscription.id} launches sessions; it has no wake target"
            )
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
            await dispatch(context, conversation_id, wake_event(text))

    return deliver


# Upper bound on the auto-generated title of a launched session.
_LAUNCH_TITLE_MAX_CHARS = 120


def launch_request_for(subscription: Subscription, prompt: str) -> LaunchRequest:
    """
    Describe the session a launch subscription starts for one event.

    The new session carries labels naming the subscription and its event
    ``source/kind`` so it stays traceable to what launched it.

    :param subscription: A launch subscription (``agent_id`` set).
    :param prompt: The rendered first user message.
    :returns: The launch request for the scheduled-task fire path.
    :raises ValueError: If the subscription has no ``agent_id``.
    """
    if subscription.agent_id is None:
        raise ValueError(f"subscription {subscription.id} does not launch sessions")
    title = f"{subscription.kind}: {subscription.note}" if subscription.note else subscription.kind
    return LaunchRequest(
        owner_user_id=subscription.user_id,
        agent_id=subscription.agent_id,
        prompt=prompt,
        title=title[:_LAUNCH_TITLE_MAX_CHARS],
        source_id=f"subscription:{subscription.id}",
        workspace_id=subscription.workspace_id,
        account_generation=subscription.account_generation,
        execution_target=subscription.execution_target or "connected_host",
        host_id=subscription.host_id,
        workspace=subscription.workspace,
        max_cost_usd=subscription.max_cost_usd,
        model_override=subscription.model_override,
        labels={
            LAUNCHED_BY_LABEL: "subscription",
            SUBSCRIPTION_LABEL: subscription.id,
            EVENT_LABEL: f"{subscription.source}/{subscription.kind}",
        },
    )


def build_session_launcher(app: Any) -> SessionLaunch:
    """
    Build the bus's launch callable bound to a FastAPI app.

    The fire dependencies are looked up on ``app.state`` at call time
    (``scheduled_fire_deps``), because the server lifespan builds them after
    the bus is constructed. The launch runs in the background via
    :func:`start_session_launch` so the ingest request returns promptly.

    :param app: The FastAPI application whose lifespan wires the fire path.
    :returns: An ``async launch(subscription, prompt)`` for :class:`EventBus`.
    """

    async def launch(subscription: Subscription, prompt: str) -> None:
        deps = getattr(app.state, "scheduled_fire_deps", None)
        if deps is None:
            raise RuntimeError("the scheduled-task fire path is not configured on the app")
        start_session_launch(deps, launch_request_for(subscription, prompt))

    return launch
