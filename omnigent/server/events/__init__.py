"""Server-side event bus: external events wake subscribed sessions.

:mod:`omnigent.server.events.bus` holds :class:`CanonicalEvent` (a normalized
event from any source) and :class:`EventBus`, which matches an event against
active subscriptions and posts a wake notice into each matching conversation.
"""

from omnigent.server.events.bus import (
    CanonicalEvent,
    EventBus,
    PublishResult,
    SessionLaunch,
    WakeDelivery,
    event_matches,
    format_launch_prompt,
    format_wake_notice,
    render_prompt_template,
)

__all__ = [
    "CanonicalEvent",
    "EventBus",
    "PublishResult",
    "SessionLaunch",
    "WakeDelivery",
    "event_matches",
    "format_launch_prompt",
    "format_wake_notice",
    "render_prompt_template",
]
