"""Integration test: a webhook event reaches the fire path for a launch subscription.

Builds the real app with the ``subscriptions`` flag on, enters its lifespan
(which wires the scheduled-task fire dependencies the bus's launcher reads),
creates a launch subscription over HTTP and posts a matching webhook event.
The fire path's background launch is replaced with a recorder, so no runner
or host is needed; ``tests/server/scheduled/test_launch_session.py`` covers
the launch itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.events import delivery
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "subscriptions"}),
    )


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient
) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    mock_llm.release_all()


async def test_webhook_event_launches_a_session_through_the_fire_path(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        delivery, "start_session_launch", lambda deps, request: started.append((deps, request))
    )
    agent_id = builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)

    created = await client.post(
        "/v1/subscriptions",
        json={
            "agent_id": agent_id,
            "source": "webhook",
            "kind": "deploy.finished",
            "match": {"service": "api"},
            "note": "check deploy",
            "prompt_template": "Verify the {kind} event: {fields_json}",
        },
    )
    assert created.status_code == 200, created.text
    subscription = created.json()

    published = await client.post(
        "/v1/events/webhook",
        json={"kind": "deploy.finished", "fields": {"service": "api", "version": "1.4.2"}},
    )
    assert published.status_code == 202, published.text
    assert published.json() == {"matched": 1, "delivered": 1, "suppressed": 0}

    assert len(started) == 1
    deps, request = started[0]
    assert deps is app.state.scheduled_fire_deps
    assert request.agent_id == subscription["agent_id"]
    assert request.prompt.startswith('Verify the deploy.finished event: {"service":"api"')
    assert "[System: event webhook/deploy.finished matched subscription" in request.prompt
    assert request.labels["omnigent.subscription"] == subscription["id"]
    assert request.title == "deploy.finished: check deploy"

    # Unmatched events launch nothing.
    other = await client.post(
        "/v1/events/webhook", json={"kind": "deploy.finished", "fields": {"service": "web"}}
    )
    assert other.json() == {"matched": 0, "delivered": 0, "suppressed": 0}
    assert len(started) == 1
