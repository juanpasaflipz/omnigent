"""Route tests for launch subscriptions (``agent_id`` + ``prompt_template``).

Mounts the subscriptions router on a bare FastAPI app over real SQLite-backed
stores with header auth, like ``test_subscriptions_routes.py``, plus a real
agent store so the agent-permission check runs the same
``validate_session_agent`` scheduled-task create uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.utils import generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.subscriptions import create_subscriptions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.subscription_store.sqlalchemy_store import SqlAlchemySubscriptionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def stores(db_uri: str) -> dict[str, Any]:
    return {
        "subscription": SqlAlchemySubscriptionStore(db_uri),
        "conversation": SqlAlchemyConversationStore(db_uri),
        "permission": SqlAlchemyPermissionStore(db_uri),
        "agent": SqlAlchemyAgentStore(db_uri),
    }


def _app(stores: dict[str, Any], *, with_agents: bool = True) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

    app.include_router(
        create_subscriptions_router(
            stores["subscription"],
            conversation_store=stores["conversation"],
            permission_store=stores["permission"],
            auth_provider=UnifiedAuthProvider(source="header"),
            agent_store=stores["agent"] if with_agents else None,
        ),
        prefix="/v1",
    )
    for user in (ALICE, BOB):
        stores["permission"].ensure_user(user, is_admin=False)
    return app


@pytest.fixture()
def client(stores: dict[str, Any]) -> TestClient:
    return TestClient(_app(stores))


@pytest.fixture()
def agent_id(stores: dict[str, Any]) -> str:
    """An operator-installed agent anyone may start sessions with."""
    agent_id = generate_agent_id()
    stores["agent"].create(agent_id, "triage", f"{agent_id}/bundle")
    return agent_id


def _headers(email: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": email}


def _launch_body(agent_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "prompt_template": "Triage this issue: {fields_json}",
        "source": "github",
        "kind": "issues.opened",
        "match": {"repo": "acme/api"},
        "note": "new issues",
    }
    body.update(overrides)
    return body


def test_create_launch_subscription_round_trips_launch_fields(
    client: TestClient, agent_id: str
) -> None:
    response = client.post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, max_cost_usd=2.5, model_override="claude-opus-4-7"),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["conversation_id"] is None
    assert body["agent_id"] == agent_id
    assert body["prompt_template"] == "Triage this issue: {fields_json}"
    assert body["execution_target"] == "connected_host"
    assert body["max_cost_usd"] == 2.5
    assert body["model_override"] == "claude-opus-4-7"
    assert body["owner_user_id"] == ALICE
    fetched = client.get(f"/v1/subscriptions/{body['id']}", headers=_headers()).json()
    assert fetched == body


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"conversation_id": "0" * 32, "agent_id": "AGENT"},
    ],
    ids=["neither", "both"],
)
def test_exactly_one_target_is_required(
    client: TestClient, agent_id: str, target: dict[str, str]
) -> None:
    body = {"source": "github", "kind": "issues.opened", "prompt_template": "x"}
    body.update({k: (agent_id if v == "AGENT" else v) for k, v in target.items()})
    response = client.post("/v1/subscriptions", json=body, headers=_headers())
    assert response.status_code == 400, response.text
    assert "exactly one of conversation_id and agent_id" in response.text


def test_unknown_execution_target_is_400(client: TestClient, agent_id: str) -> None:
    response = client.post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, execution_target="the_cloud"),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "execution_target" in response.text


def test_prompt_template_is_required_with_agent_id(client: TestClient, agent_id: str) -> None:
    response = client.post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, prompt_template="   "),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "prompt_template" in response.text


def test_launch_fields_are_rejected_on_a_conversation_subscription(
    client: TestClient, stores: dict[str, Any]
) -> None:
    conversation = stores["conversation"].create_conversation(title="alice's session")
    stores["permission"].grant(ALICE, conversation.id, LEVEL_OWNER)
    response = client.post(
        "/v1/subscriptions",
        json={
            "conversation_id": conversation.id,
            "source": "github",
            "kind": "push",
            "prompt_template": "nope",
        },
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "prompt_template" in response.text


def test_unknown_agent_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions", json=_launch_body(generate_agent_id()), headers=_headers()
    )
    assert response.status_code == 404, response.text


def test_another_users_private_agent_is_not_bindable(
    client: TestClient, stores: dict[str, Any]
) -> None:
    """Same agent-visibility bar scheduled-task create applies."""
    bobs_agent = generate_agent_id()
    stores["agent"].create_user_agent(bobs_agent, "bob-private", f"{bobs_agent}/b", owner=BOB)
    denied = client.post("/v1/subscriptions", json=_launch_body(bobs_agent), headers=_headers())
    assert denied.status_code == 404, denied.text
    allowed = client.post(
        "/v1/subscriptions", json=_launch_body(bobs_agent), headers=_headers(BOB)
    )
    assert allowed.status_code == 200, allowed.text


def test_managed_sandbox_requires_server_support(client: TestClient, agent_id: str) -> None:
    response = client.post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, execution_target="managed_sandbox"),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "managed sandboxes are not configured" in response.text


def test_managed_sandbox_with_pinned_host_is_400(stores: dict[str, Any], agent_id: str) -> None:
    app = _app(stores)
    app.state.sandbox_config = SimpleNamespace(managed_launch_supported=True)
    response = TestClient(app).post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, execution_target="managed_sandbox", host_id="h" * 32),
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "do not set host_id or workspace" in response.text


def test_managed_sandbox_launch_subscription_is_created_when_supported(
    stores: dict[str, Any], agent_id: str
) -> None:
    app = _app(stores)
    app.state.sandbox_config = SimpleNamespace(managed_launch_supported=True)
    response = TestClient(app).post(
        "/v1/subscriptions",
        json=_launch_body(agent_id, execution_target="managed_sandbox"),
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["execution_target"] == "managed_sandbox"


def test_workspace_without_host_is_400(client: TestClient, agent_id: str) -> None:
    response = client.post(
        "/v1/subscriptions", json=_launch_body(agent_id, workspace="/repo"), headers=_headers()
    )
    assert response.status_code == 400, response.text
    assert "host_id required" in response.text


def test_launch_subscriptions_need_an_agent_store(stores: dict[str, Any], agent_id: str) -> None:
    response = TestClient(_app(stores, with_agents=False)).post(
        "/v1/subscriptions", json=_launch_body(agent_id), headers=_headers()
    )
    assert response.status_code == 400, response.text
    assert "not available" in response.text


def test_launch_subscription_is_owner_scoped(client: TestClient, agent_id: str) -> None:
    created = client.post("/v1/subscriptions", json=_launch_body(agent_id), headers=_headers())
    subscription_id = created.json()["id"]
    assert (
        client.get(f"/v1/subscriptions/{subscription_id}", headers=_headers(BOB)).status_code
        == 404
    )
    listed = client.get("/v1/subscriptions", headers=_headers()).json()["subscriptions"]
    assert [row["id"] for row in listed] == [subscription_id]
