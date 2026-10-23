"""Tests for forwarding Slack messages to the Omnigent event bus.

Covers the pure ``slack_message_event`` body builder and the service's opt-in
forwarding: channel messages and thread replies are published with the
poster's client, the bot's own messages and DMs are not, and a forwarding
failure never breaks normal thread-to-session handling.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from omnigent_slack.forwarding import (
    MAX_FORWARDED_TEXT_CHARS,
    SLACK_MESSAGE_KIND,
    slack_message_event,
)
from omnigent_slack.omnigent import AuthRequiredError
from omnigent_slack.service import SlackOmnigentService
from test_service import (
    FakeOmnigentClient,
    FakePool,
    FakeSetup,
    FakeSlackClient,
    _store,
    _wait_for_turns,
)


class ForwardingClient(FakeOmnigentClient):
    """Records published events; optionally fails like an unreachable server."""

    def __init__(self, *, fail: BaseException | None = None) -> None:
        super().__init__()
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish_event(self, kind: str, fields: dict[str, Any]) -> None:
        self.published.append((kind, fields))
        if self.fail is not None:
            raise self.fail


class RecordingPool(FakePool):
    def __init__(self, client: FakeOmnigentClient) -> None:
        super().__init__(client)
        self.users: list[str] = []

    async def get(self, server_url: str, user_id: str = "") -> FakeOmnigentClient:
        self.users.append(user_id)
        return await super().get(server_url, user_id)


def _service(store: Any, client: FakeOmnigentClient, *, forward: bool = True):
    pool = RecordingPool(client)
    service = SlackOmnigentService(
        store=store,
        pool=pool,  # type: ignore[arg-type]
        setup=FakeSetup(),  # type: ignore[arg-type]
        server_url="http://omnigent.test",
        forward_events=forward,
    )
    return service, pool


async def _drain_forwards(service: SlackOmnigentService) -> None:
    await asyncio.gather(*list(service._forward_tasks), return_exceptions=True)


def _channel_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "channel": "C1",
        "channel_type": "channel",
        "ts": "101.1",
        "user": "U1",
        "text": "deploy is red again",
    }
    event.update(overrides)
    return event


# ── slack_message_event ─────────────────────────────────────────────────────


def test_top_level_message_uses_its_own_ts_as_thread() -> None:
    payload = slack_message_event(_channel_event())
    assert payload == {
        "kind": SLACK_MESSAGE_KIND,
        "fields": {
            "channel": "C1",
            "thread_ts": "101.1",
            "ts": "101.1",
            "user": "U1",
            "text": "deploy is red again",
            "is_thread_reply": False,
        },
    }


def test_thread_reply_carries_the_root_thread_ts() -> None:
    payload = slack_message_event(_channel_event(thread_ts="100.1"))
    assert payload is not None
    assert payload["fields"]["thread_ts"] == "100.1"
    assert payload["fields"]["is_thread_reply"] is True


def test_long_text_is_truncated() -> None:
    payload = slack_message_event(_channel_event(text="x" * (MAX_FORWARDED_TEXT_CHARS + 50)))
    assert payload is not None
    assert len(payload["fields"]["text"]) == MAX_FORWARDED_TEXT_CHARS


@pytest.mark.parametrize(
    "overrides",
    [{"channel": None}, {"ts": ""}, {"subtype": "channel_join"}, {"subtype": "channel_topic"}],
)
def test_unidentifiable_or_housekeeping_events_are_not_forwarded(
    overrides: dict[str, Any],
) -> None:
    assert slack_message_event(_channel_event(**overrides)) is None


def test_thread_broadcast_is_forwarded() -> None:
    assert slack_message_event(_channel_event(subtype="thread_broadcast")) is not None


# ── service forwarding ──────────────────────────────────────────────────────


async def test_channel_message_is_forwarded_with_the_posters_client(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = ForwardingClient()
    service, pool = _service(store, client)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event=_channel_event(thread_ts="100.1"),
        client=FakeSlackClient(),
        context={"bot_user_id": "B1"},
    )
    await _drain_forwards(service)
    await service.shutdown()

    assert client.published == [
        (
            "slack.message",
            {
                "channel": "C1",
                "thread_ts": "100.1",
                "ts": "101.1",
                "user": "U1",
                "text": "deploy is red again",
                "is_thread_reply": True,
            },
        )
    ]
    assert pool.users == ["T1:U1"]
    # Forwarding does not make the bot join a channel thread it wasn't asked into.
    assert client.created == []
    assert client.turns == []


async def test_forwarding_is_off_by_default(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = ForwardingClient()
    service, _pool = _service(store, client, forward=False)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event=_channel_event(),
        client=FakeSlackClient(),
        context={"bot_user_id": "B1"},
    )
    await _drain_forwards(service)
    await service.shutdown()

    assert client.published == []


@pytest.mark.parametrize(
    "overrides",
    [{"user": "B1"}, {"bot_id": "BOT"}, {"subtype": "bot_message"}],
    ids=["own-user", "bot-id", "bot-subtype"],
)
async def test_bot_messages_are_not_forwarded(tmp_path: Path, overrides: dict[str, Any]) -> None:
    store = await _store(tmp_path)
    client = ForwardingClient()
    service, _pool = _service(store, client)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event=_channel_event(**overrides),
        client=FakeSlackClient(),
        context={"bot_user_id": "B1"},
    )
    await _drain_forwards(service)
    await service.shutdown()

    assert client.published == []


async def test_direct_messages_are_not_forwarded(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    client = ForwardingClient()
    service, _pool = _service(store, client)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event=_channel_event(channel="D1", channel_type="im", text="private"),
        client=FakeSlackClient(),
        context={"bot_user_id": "B1"},
    )
    await _drain_forwards(service)
    await _wait_for_turns(service)
    await service.shutdown()

    assert client.published == []


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("server down"), AuthRequiredError("sign in")],
    ids=["server-error", "not-signed-in"],
)
async def test_forward_failure_does_not_break_dm_turns(
    tmp_path: Path, failure: BaseException
) -> None:
    """A failing forward is logged; the normal DM session flow still runs."""
    store = await _store(tmp_path)
    client = ForwardingClient(fail=failure)
    service, _pool = _service(store, client)

    # A channel message whose forward fails...
    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event=_channel_event(),
        client=FakeSlackClient(),
        context={"bot_user_id": "B1"},
    )
    await _drain_forwards(service)
    assert len(client.published) == 1
    # ...leaves no task behind and raises nothing.
    assert service._forward_tasks == set()
    await service.shutdown()
