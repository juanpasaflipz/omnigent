"""Forward Slack messages to the Omnigent event bus (opt-in).

With ``OMNIGENT_SLACK_FORWARD_EVENTS=true`` the bot posts every channel
message and thread reply it sees (never its own, never DMs) to the server's
generic ``POST /v1/events/webhook`` as kind ``slack.message``. An agent can
then ``sys_subscribe`` to ``source="webhook", kind="slack.message"`` with a
filter such as ``{"channel": "C123"}`` or ``{"thread_ts": "1712.0001"}`` and
wake (or launch a session) when someone posts there — without the Slack
daemon owning any routing of its own. See ``docs/SUBSCRIPTIONS.md``.

This module is pure: it only builds the event body. The service decides when
to forward and sends it with the posting user's authenticated server client.
"""

from __future__ import annotations

from typing import Any

#: Event kind the forwarded messages are published under (source ``webhook``).
SLACK_MESSAGE_KIND = "slack.message"

#: Message subtypes that carry a person's post. Everything else (joins, topic
#: changes, edits, deletions, bot posts) is channel housekeeping, not forwarded.
_FORWARDED_SUBTYPES = frozenset({"thread_broadcast", "file_share"})

#: Upper bound on the forwarded message text, matching the bound the server
#: applies to the JSON echoed in a wake notice.
MAX_FORWARDED_TEXT_CHARS = 4_000


def slack_message_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Build the ``/v1/events/webhook`` body for one Slack message event.

    ``thread_ts`` is the thread root: the message's own ``ts`` for a top-level
    message, so a subscription filtered on a thread matches its first message
    and every reply alike. ``is_thread_reply`` tells the two apart.

    :param event: The Slack ``message`` event payload.
    :returns: ``{"kind": ..., "fields": {...}}``, or ``None`` when the event
        is housekeeping (a non-post subtype) or lacks a channel or timestamp.
    """
    subtype = event.get("subtype")
    if subtype is not None and subtype not in _FORWARDED_SUBTYPES:
        return None
    channel = event.get("channel")
    ts = event.get("ts")
    if not isinstance(channel, str) or not channel or not isinstance(ts, str) or not ts:
        return None
    raw_thread_ts = event.get("thread_ts")
    thread_ts = raw_thread_ts if isinstance(raw_thread_ts, str) and raw_thread_ts else ts
    text = event.get("text")
    text = text if isinstance(text, str) else ""
    if len(text) > MAX_FORWARDED_TEXT_CHARS:
        text = text[:MAX_FORWARDED_TEXT_CHARS]
    user = event.get("user")
    return {
        "kind": SLACK_MESSAGE_KIND,
        "fields": {
            "channel": channel,
            "thread_ts": thread_ts,
            "ts": ts,
            "user": user if isinstance(user, str) else "",
            "text": text,
            "is_thread_reply": thread_ts != ts,
        },
    }
