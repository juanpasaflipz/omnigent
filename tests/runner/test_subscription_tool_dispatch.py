"""Tests for the event-subscription (``sys_subscribe`` family) tool surface.

Covers the runner-side half:

- ``_execute_subscription_tool``: subscribe/list/unsubscribe proxy the correct
  ``/v1/subscriptions`` REST verb + payload over ``server_client``, the runner
  injects the calling ``conversation_id``, unknown fields are dropped, and a
  server 4xx/5xx, a missing id or a missing session context become clean error
  JSON.
- Registration: the three names register only when the spec sets
  ``subscriptions: true`` and are members of the local-dispatch and
  native-relay tool sets.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _SUBSCRIPTION_TOOLS,
    _execute_subscription_tool,
)

_ALL_NAMES = {"sys_subscribe", "sys_unsubscribe", "sys_subscription_list"}
_SUBSCRIPTION_ID = "0123456789abcdef0123456789abcdef"
_CONVERSATION_ID = "fedcba9876543210fedcba9876543210"


class _Resp:
    def __init__(self, *, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    """Records the verb/url/json of each call and returns a scripted response."""

    def __init__(self, response: _Resp | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self._response = response or _Resp(body={"id": _SUBSCRIPTION_ID})

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("GET", url, None))
        return self._response

    async def post(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        self.calls.append(("POST", url, json))
        return self._response

    async def delete(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("DELETE", url, None))
        return self._response


@pytest.mark.asyncio
async def test_subscribe_posts_payload_with_runner_supplied_conversation() -> None:
    client = _RecordingClient(_Resp(body={"id": _SUBSCRIPTION_ID, "state": "active"}))
    out = await _execute_subscription_tool(
        "sys_subscribe",
        json.dumps(
            {
                "source": "github",
                "kind": "check_run.completed",
                "match": {"repo": "acme/api", "pull_request": 412},
                "note": "CI on my PR",
                # The model must not pick the target session or smuggle fields.
                "conversation_id": "someone-elses",
                "unexpected": "dropped",
            }
        ),
        server_client=client,
        conversation_id=_CONVERSATION_ID,
    )
    verb, url, body = client.calls[0]
    assert (verb, url) == ("POST", "/v1/subscriptions")
    assert body == {
        "source": "github",
        "kind": "check_run.completed",
        "match": {"repo": "acme/api", "pull_request": 412},
        "note": "CI on my PR",
        "conversation_id": _CONVERSATION_ID,
    }
    assert json.loads(out)["id"] == _SUBSCRIPTION_ID


@pytest.mark.asyncio
async def test_subscribe_with_agent_id_targets_the_agent_not_the_session() -> None:
    """A launch subscription forwards its launch fields and no conversation_id."""
    client = _RecordingClient(_Resp(body={"id": _SUBSCRIPTION_ID, "state": "active"}))
    await _execute_subscription_tool(
        "sys_subscribe",
        json.dumps(
            {
                "source": "github",
                "kind": "issues.opened",
                "agent_id": "ag_triage",
                "prompt_template": "Triage {fields_json}",
                "execution_target": "managed_sandbox",
                "max_cost_usd": 2.0,
            }
        ),
        server_client=client,
        conversation_id=_CONVERSATION_ID,
    )
    _, _, body = client.calls[0]
    assert body == {
        "source": "github",
        "kind": "issues.opened",
        "agent_id": "ag_triage",
        "prompt_template": "Triage {fields_json}",
        "execution_target": "managed_sandbox",
        "max_cost_usd": 2.0,
    }


@pytest.mark.asyncio
async def test_subscribe_without_session_context_errors() -> None:
    client = _RecordingClient()
    out = await _execute_subscription_tool(
        "sys_subscribe",
        json.dumps({"source": "github", "kind": "push"}),
        server_client=client,
        conversation_id=None,
    )
    assert "session context" in json.loads(out)["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_list_gets() -> None:
    client = _RecordingClient(_Resp(body={"subscriptions": []}))
    out = await _execute_subscription_tool(
        "sys_subscription_list", "", server_client=client, conversation_id=_CONVERSATION_ID
    )
    assert client.calls[0] == ("GET", "/v1/subscriptions", None)
    assert json.loads(out) == {"subscriptions": []}


@pytest.mark.asyncio
async def test_unsubscribe_deletes_by_id() -> None:
    client = _RecordingClient(_Resp(body={"deleted": True, "id": _SUBSCRIPTION_ID}))
    await _execute_subscription_tool(
        "sys_unsubscribe",
        json.dumps({"subscription_id": _SUBSCRIPTION_ID.upper()}),
        server_client=client,
        conversation_id=_CONVERSATION_ID,
    )
    assert client.calls[0] == ("DELETE", f"/v1/subscriptions/{_SUBSCRIPTION_ID}", None)


@pytest.mark.asyncio
async def test_unsubscribe_rejects_path_confusion_id() -> None:
    client = _RecordingClient()
    out = await _execute_subscription_tool(
        "sys_unsubscribe",
        json.dumps({"subscription_id": f"../{_SUBSCRIPTION_ID}"}),
        server_client=client,
        conversation_id=_CONVERSATION_ID,
    )
    assert "canonical 32-character hex" in json.loads(out)["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_unsubscribe_without_id_errors() -> None:
    client = _RecordingClient()
    out = await _execute_subscription_tool(
        "sys_unsubscribe", "{}", server_client=client, conversation_id=_CONVERSATION_ID
    )
    assert "subscription_id" in json.loads(out)["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_server_error_becomes_clean_json() -> None:
    client = _RecordingClient(_Resp(status_code=422, body={"error": "source must be one of"}))
    out = await _execute_subscription_tool(
        "sys_subscribe",
        json.dumps({"source": "slack", "kind": "message"}),
        server_client=client,
        conversation_id=_CONVERSATION_ID,
    )
    assert "server returned 422" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_malformed_arguments_error() -> None:
    client = _RecordingClient()
    out = await _execute_subscription_tool(
        "sys_subscribe", "{not json", server_client=client, conversation_id=_CONVERSATION_ID
    )
    assert "malformed JSON" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_no_server_client_errors() -> None:
    out = await _execute_subscription_tool(
        "sys_subscription_list", "", server_client=None, conversation_id=_CONVERSATION_ID
    )
    assert "requires server access" in json.loads(out)["error"]


def test_tools_register_only_with_spec_optin() -> None:
    from omnigent.spec.types import AgentSpec
    from omnigent.tools.manager import ToolManager

    enabled = {
        s["function"]["name"]
        for s in ToolManager(AgentSpec(spec_version=1, subscriptions=True)).get_tool_schemas()
    }
    disabled = {
        s["function"]["name"] for s in ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    }
    assert enabled >= _ALL_NAMES
    assert _ALL_NAMES.isdisjoint(disabled)


def test_subscribe_schema_requires_source_and_kind_only() -> None:
    from omnigent.tools.builtins.subscriptions import SysSubscribeTool

    schema = SysSubscribeTool().get_schema()["function"]["parameters"]
    assert set(schema["required"]) == {"source", "kind"}
    assert set(schema["properties"]) == {
        "source",
        "kind",
        "match",
        "note",
        "agent_id",
        "prompt_template",
        "execution_target",
        "host_id",
        "workspace",
        "max_cost_usd",
        "model_override",
    }
    # The runner supplies the session; the model never names one.
    assert "conversation_id" not in schema["properties"]


def test_tools_in_dispatch_and_relay_sets() -> None:
    assert _SUBSCRIPTION_TOOLS == _ALL_NAMES
    assert _ALL_NAMES <= _ALL_LOCAL_TOOLS
    assert _ALL_NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS
