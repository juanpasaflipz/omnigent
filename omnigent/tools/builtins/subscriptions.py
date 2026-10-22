"""Built-in tools for event subscriptions (wake this session on external events).

A subscription registers the calling session's interest in events of one
``(source, kind)`` whose fields satisfy a filter. When a matching event reaches
the server (``POST /v1/events/github`` or ``POST /v1/events/webhook``), the
server posts a wake notice into the session, exactly like a timer firing. The
runner dispatches each tool to the server's ``/v1/subscriptions`` REST
endpoints (same posture as ``sys_scheduled_task_*``) and fills in the calling
``conversation_id`` itself — the model never names a session.

* ``sys_subscribe`` — create a subscription for this session.
* ``sys_unsubscribe`` — delete a subscription by id.
* ``sys_subscription_list`` — list the caller's subscriptions.

Unlike ``sys_scheduled_task_create``, ``sys_subscribe`` is not approval-gated:
it can only wake the caller's own session, never launch new work elsewhere.

Registration is gated on ``subscriptions: true`` in the agent spec, like
``timers:``. See ``designs/HARNESS_FLYWHEEL.md`` §2.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_SOURCE_DESC = (
    "Event source: 'github' for GitHub webhook events delivered to the server, "
    "or 'webhook' for events posted to the generic /v1/events/webhook endpoint."
)
_KIND_DESC = (
    "Event kind to wake on. GitHub kinds: 'check_run.completed', "
    "'check_suite.completed', 'pull_request.opened', 'pull_request.synchronize', "
    "'pull_request.closed', 'issues.opened', 'issues.closed', "
    "'issue_comment.created', 'push'. Webhook kinds are whatever the poster "
    "sends, e.g. 'deploy.finished'."
)
_MATCH_DESC = (
    "Optional field filter: every key must be present in the event with an equal "
    "value for the subscription to fire. GitHub events carry 'repo' "
    "(owner/name), 'action', 'conclusion', 'status', 'name', 'pull_request' "
    "(number), 'issue' (number), 'sender', 'ref' and 'url'. Example: "
    '{"repo": "acme/api", "conclusion": "failure", "pull_request": 412}.'
)


class SysSubscribeTool(Tool):
    """Subscribe this session to an event. Dispatched to ``POST /v1/subscriptions``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_subscribe"``."""
        return "sys_subscribe"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Wake this session when an external event arrives: subscribe to a "
            "source/kind with an optional field filter. When a matching event "
            "reaches the server you receive a '[System: event ...]' message "
            "containing the event fields and a new turn starts. Use it to wait "
            "for CI on a pull request, a new issue, or a custom webhook instead "
            "of polling. The subscription stays active until sys_unsubscribe."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["github", "webhook"],
                            "description": _SOURCE_DESC,
                        },
                        "kind": {"type": "string", "description": _KIND_DESC},
                        "match": {
                            "type": "object",
                            "description": _MATCH_DESC,
                            "additionalProperties": True,
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "Short label echoed in the wake notice so you remember "
                                "why you subscribed, e.g. 'CI on my PR'."
                            ),
                        },
                    },
                    "required": ["source", "kind"],
                    "additionalProperties": False,
                },
            },
        }


class SysUnsubscribeTool(Tool):
    """Delete a subscription. Dispatched to ``DELETE /v1/subscriptions/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_unsubscribe"``."""
        return "sys_unsubscribe"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Delete an event subscription so it no longer wakes this session."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subscription_id": {
                            "type": "string",
                            "description": (
                                "The subscription to delete (from sys_subscribe or "
                                "sys_subscription_list)."
                            ),
                        },
                    },
                    "required": ["subscription_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysSubscriptionListTool(Tool):
    """List the caller's subscriptions. Dispatched to ``GET /v1/subscriptions``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_subscription_list"``."""
        return "sys_subscription_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List your event subscriptions with their source, kind, filter, state "
            "and how many times each has fired."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
