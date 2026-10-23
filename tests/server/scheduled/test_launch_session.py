"""Tests for :func:`launch_session`, the fire path's reusable launch entry point.

Launch subscriptions (subscriptions v2) start sessions through the same
machinery as scheduled fires. These tests drive :func:`launch_session` with
the fakes from ``test_fire.py`` and an injected launch seam, and check that it
creates, labels, grants, caps and dispatches a session for the request's owner
without ever touching the scheduled-task store.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.scheduled import fire as fire_mod
from omnigent.server.scheduled.fire import LaunchRequest, launch_session, start_session_launch
from tests.server.scheduled.test_fire import (
    FakeConversationStore,
    FakePermissionStore,
    FakePolicyStore,
    FakeScheduledTaskStore,
    _deps,
    _FakeSandboxConfig,
)


def _request(**overrides: Any) -> LaunchRequest:
    base: dict[str, Any] = {
        "owner_user_id": "alice@example.com",
        "agent_id": "ag_1",
        "prompt": "Triage issue 7",
        "title": "issues.opened: new issues",
        "source_id": "subscription:sub_1",
        "workspace_id": 0,
        "host_id": "host_1",
        "workspace": "/repo",
        "labels": {"omnigent.subscription": "sub_1"},
    }
    base.update(overrides)
    return LaunchRequest(**base)


class _RecordingLaunch:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.workspace_ids: list[int] = []
        self.fail = fail

    async def __call__(self, conv: Any, task: Any) -> None:
        self.calls.append((conv, task))
        self.workspace_ids.append(current_workspace_id())
        if self.fail:
            raise RuntimeError("runner never connected")


@pytest.mark.asyncio
async def test_launch_creates_labels_grants_and_dispatches_the_prompt() -> None:
    sched = FakeScheduledTaskStore()
    conv_store = FakeConversationStore()
    perm = FakePermissionStore()
    launch = _RecordingLaunch()

    outcome = await launch_session(
        _deps(sched, conversation_store=conv_store, permission_store=perm),
        _request(),
        launch_dispatch=launch,
    )

    assert outcome.status == "running"
    assert outcome.conversation_id == "conv_1"
    assert conv_store.created[0]["agent_id"] == "ag_1"
    assert conv_store.created[0]["title"] == "issues.opened: new issues"
    assert conv_store.label_writes["conv_1"]["omnigent.subscription"] == "sub_1"
    assert perm.grants[0] == ("alice@example.com", "conv_1", LEVEL_OWNER)
    # The dispatched task carries the request's prompt for the first message.
    assert launch.calls[0][1].prompt == "Triage issue 7"
    # A one-off launch never writes scheduled-task runs or rows.
    assert sched.runs == []
    assert sched.updates == []


@pytest.mark.asyncio
async def test_launch_attaches_cost_cap_and_model_override() -> None:
    conv_store = FakeConversationStore()
    policy_store = FakePolicyStore()

    outcome = await launch_session(
        _deps(FakeScheduledTaskStore(), conversation_store=conv_store, policy_store=policy_store),
        _request(max_cost_usd=3.0, model_override="claude-opus-4-7"),
        launch_dispatch=_RecordingLaunch(),
    )

    assert outcome.status == "running"
    assert policy_store.created[0]["factory_params"] == {"max_cost_usd": 3.0}
    assert conv_store.updated[0]["model_override"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_launch_runs_under_the_request_workspace_scope() -> None:
    conv_store = FakeConversationStore()
    launch = _RecordingLaunch()

    await launch_session(
        _deps(FakeScheduledTaskStore(), conversation_store=conv_store),
        _request(workspace_id=7),
        launch_dispatch=launch,
    )

    assert conv_store.create_workspace_ids == [7]
    assert launch.workspace_ids == [7]


@pytest.mark.asyncio
async def test_managed_sandbox_launch_is_hostless() -> None:
    conv_store = FakeConversationStore()

    outcome = await launch_session(
        _deps(
            FakeScheduledTaskStore(),
            conversation_store=conv_store,
            sandbox_config=_FakeSandboxConfig(managed_launch_supported=True),
        ),
        _request(execution_target="managed_sandbox", host_id=None, workspace=None),
        launch_dispatch=_RecordingLaunch(),
    )

    assert outcome.status == "running"
    assert conv_store.created[0]["host_id"] is None
    assert conv_store.created[0]["workspace"] is None


@pytest.mark.asyncio
async def test_unconfigured_managed_sandbox_fails_without_a_session() -> None:
    conv_store = FakeConversationStore()

    outcome = await launch_session(
        _deps(FakeScheduledTaskStore(), conversation_store=conv_store),
        _request(execution_target="managed_sandbox", host_id=None, workspace=None),
        launch_dispatch=_RecordingLaunch(),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "managed_sandbox_unavailable"
    assert conv_store.created == []


@pytest.mark.asyncio
async def test_deleted_owner_launches_nothing_and_touches_no_task() -> None:
    sched = FakeScheduledTaskStore()
    conv_store = FakeConversationStore()

    outcome = await launch_session(
        _deps(
            sched, conversation_store=conv_store, permission_store=FakePermissionStore(users=set())
        ),
        _request(),
        launch_dispatch=_RecordingLaunch(),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "owner_deleted"
    assert conv_store.created == []
    assert sched.updates == []


@pytest.mark.asyncio
async def test_dispatch_failure_is_reported_not_raised() -> None:
    outcome = await launch_session(
        _deps(FakeScheduledTaskStore()),
        _request(),
        launch_dispatch=_RecordingLaunch(fail=True),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "launch_failed"
    assert outcome.conversation_id == "conv_1"


@pytest.mark.asyncio
async def test_start_session_launch_runs_in_the_background() -> None:
    launch = _RecordingLaunch()

    task = start_session_launch(
        _deps(FakeScheduledTaskStore()), _request(), launch_dispatch=launch
    )

    assert task in fire_mod._PENDING_FIRES
    outcome = await task
    assert outcome.status == "running"
    assert len(launch.calls) == 1
