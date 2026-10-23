"""Tests for launch subscriptions on the event bus (subscriptions v2).

A subscription with ``agent_id`` starts a fresh session per matching event
instead of waking an existing one. These tests cover prompt-template
rendering, the bus routing launch rows to the injected launcher (while
conversation rows still get wake notices), cooldown, launcher-failure
isolation, and the translation of a subscription into the scheduled-task fire
path's :class:`LaunchRequest`. No database or runner is involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.entities import Subscription
from omnigent.server.events import (
    CanonicalEvent,
    EventBus,
    PublishResult,
    format_launch_prompt,
    format_wake_notice,
    render_prompt_template,
)
from omnigent.server.events.delivery import (
    EVENT_LABEL,
    LAUNCHED_BY_LABEL,
    SUBSCRIPTION_LABEL,
    launch_request_for,
)
from tests.server.events.test_bus import FakeSubscriptionStore, RecordingDelivery

_EVENT = CanonicalEvent(
    source="github",
    kind="issues.opened",
    fields={"repo": "acme/api", "issue": 7, "sender": "octocat"},
    received_at=2_000,
)


def _launch_subscription(
    subscription_id: str = "sub_launch",
    *,
    template: str = "Triage {kind} from {source}: {fields_json}",
    last_fired_at: int | None = None,
    **overrides: Any,
) -> Subscription:
    base: dict[str, Any] = {
        "id": subscription_id,
        "user_id": "alice@example.com",
        "conversation_id": None,
        "source": "github",
        "kind": "issues.opened",
        "created_at": 1_000,
        "workspace_id": 3,
        "match": {"repo": "acme/api"},
        "note": "new issues",
        "last_fired_at": last_fired_at,
        "agent_id": "ag_triage",
        "prompt_template": template,
        "execution_target": "connected_host",
        "account_generation": "gen_1",
    }
    base.update(overrides)
    return Subscription(**base)


def _wake_subscription(subscription_id: str = "sub_wake") -> Subscription:
    return Subscription(
        id=subscription_id,
        user_id="alice@example.com",
        conversation_id="conv_1",
        source="github",
        kind="issues.opened",
        created_at=1_000,
        match={"repo": "acme/api"},
        note="watch",
    )


class RecordingLauncher:
    """Collects every launch the bus hands over, optionally failing."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def __call__(self, subscription: Subscription, prompt: str) -> None:
        self.calls.append((subscription.id, prompt))
        if self.fail:
            raise RuntimeError("fire path not configured")


# ── template rendering ───────────────────────────────────────────────────────


def test_template_renders_every_supported_placeholder() -> None:
    subscription = _launch_subscription(
        template="{source}|{kind}|{subscription_id}|{note}|{fields_json}"
    )
    rendered = render_prompt_template(subscription.prompt_template or "", _EVENT, subscription)
    source, kind, sub_id, note, fields_json = rendered.split("|", 4)
    assert (source, kind, sub_id, note) == ("github", "issues.opened", "sub_launch", "new issues")
    assert json.loads(fields_json) == _EVENT.fields


def test_template_leaves_unknown_placeholders_literal() -> None:
    subscription = _launch_subscription(template="Issue {issue} in {repo} ({kind})")
    rendered = render_prompt_template(subscription.prompt_template or "", _EVENT, subscription)
    assert rendered == "Issue {issue} in {repo} (issues.opened)"


@pytest.mark.parametrize("template", ["unbalanced { brace", "{fields_json.attr}", "{0}"])
def test_unformattable_template_is_returned_unchanged(template: str) -> None:
    subscription = _launch_subscription(template=template)
    assert render_prompt_template(template, _EVENT, subscription) == template


def test_missing_note_renders_empty() -> None:
    subscription = _launch_subscription(template="[{note}]", note=None)
    assert render_prompt_template("[{note}]", _EVENT, subscription) == "[]"


def test_launch_prompt_is_template_followed_by_wake_notice() -> None:
    subscription = _launch_subscription(template="Triage {kind}")
    prompt = format_launch_prompt(_EVENT, subscription)
    assert prompt == f"Triage issues.opened\n\n{format_wake_notice(_EVENT, subscription)}"


# ── bus routing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bus_launches_agent_subscription_and_still_wakes_conversations() -> None:
    launch_row, wake_row = _launch_subscription(), _wake_subscription()
    store = FakeSubscriptionStore([launch_row, wake_row])
    deliver, launcher = RecordingDelivery(), RecordingLauncher()
    bus = EventBus(store, deliver, launch=launcher, clock=lambda: 5_000)

    result = await bus.publish(_EVENT)

    assert result == PublishResult(matched=2, delivered=2, suppressed=0)
    assert launcher.calls == [("sub_launch", format_launch_prompt(_EVENT, launch_row))]
    assert deliver.calls == [("sub_wake", format_wake_notice(_EVENT, wake_row))]
    assert sorted(store.fires) == [("sub_launch", 5_000), ("sub_wake", 5_000)]


@pytest.mark.asyncio
async def test_cooldown_applies_to_launch_subscriptions() -> None:
    store = FakeSubscriptionStore([_launch_subscription(last_fired_at=4_995)])
    launcher = RecordingLauncher()
    bus = EventBus(store, RecordingDelivery(), launch=launcher, clock=lambda: 5_000)

    result = await bus.publish(_EVENT)

    assert result == PublishResult(matched=1, delivered=0, suppressed=1)
    assert launcher.calls == []
    assert store.fires == []


@pytest.mark.asyncio
async def test_launcher_failure_is_isolated_and_still_stamps_the_fire() -> None:
    store = FakeSubscriptionStore([_launch_subscription(), _wake_subscription()])
    deliver = RecordingDelivery()
    bus = EventBus(store, deliver, launch=RecordingLauncher(fail=True), clock=lambda: 5_000)

    result = await bus.publish(_EVENT)

    assert result == PublishResult(matched=2, delivered=1, suppressed=0)
    assert [call[0] for call in deliver.calls] == ["sub_wake"]
    assert ("sub_launch", 5_000) in store.fires


@pytest.mark.asyncio
async def test_launch_subscription_without_launcher_is_matched_not_delivered() -> None:
    store = FakeSubscriptionStore([_launch_subscription()])
    deliver = RecordingDelivery()
    bus = EventBus(store, deliver, clock=lambda: 5_000)

    result = await bus.publish(_EVENT)

    assert result == PublishResult(matched=1, delivered=0, suppressed=0)
    # A launch row is never mistaken for a wake target.
    assert deliver.calls == []


# ── subscription → LaunchRequest ─────────────────────────────────────────────


def test_launch_request_mirrors_the_subscription_and_labels_the_session() -> None:
    subscription = _launch_subscription(
        execution_target="managed_sandbox",
        max_cost_usd=2.5,
        model_override="claude-opus-4-7",
    )
    request = launch_request_for(subscription, "the prompt")

    assert request.owner_user_id == "alice@example.com"
    assert request.agent_id == "ag_triage"
    assert request.prompt == "the prompt"
    assert request.title == "issues.opened: new issues"
    assert request.workspace_id == 3
    assert request.account_generation == "gen_1"
    assert request.execution_target == "managed_sandbox"
    assert request.max_cost_usd == 2.5
    assert request.model_override == "claude-opus-4-7"
    assert request.labels == {
        LAUNCHED_BY_LABEL: "subscription",
        SUBSCRIPTION_LABEL: "sub_launch",
        EVENT_LABEL: "github/issues.opened",
    }


def test_launch_request_rejects_a_wake_subscription() -> None:
    with pytest.raises(ValueError, match="does not launch"):
        launch_request_for(_wake_subscription(), "prompt")


# ── server launcher wiring ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_launcher_hands_the_request_to_the_fire_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omnigent.server.events import delivery

    started: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        delivery, "start_session_launch", lambda deps, request: started.append((deps, request))
    )
    deps = object()
    app = SimpleNamespace(state=SimpleNamespace(scheduled_fire_deps=deps))

    await delivery.build_session_launcher(app)(_launch_subscription(), "prompt text")

    assert len(started) == 1
    assert started[0][0] is deps
    assert started[0][1].prompt == "prompt text"
    assert started[0][1].agent_id == "ag_triage"


@pytest.mark.asyncio
async def test_server_launcher_without_fire_deps_raises() -> None:
    from types import SimpleNamespace

    from omnigent.server.events.delivery import build_session_launcher

    launcher = build_session_launcher(SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(RuntimeError, match="fire path is not configured"):
        await launcher(_launch_subscription(), "prompt")
