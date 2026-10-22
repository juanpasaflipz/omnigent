"""Tests for the subscription CRUD routes (``/v1/subscriptions``).

Mounts the router on a bare FastAPI app over real SQLite-backed subscription,
conversation and permission stores with header auth, so ownership and
conversation-access checks run against actual grants without the full
``create_app`` surface or a runner.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.subscriptions import create_subscriptions_router
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
    }


@pytest.fixture()
def client(stores: dict[str, Any]) -> TestClient:
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
        ),
        prefix="/v1",
    )
    for user in (ALICE, BOB):
        stores["permission"].ensure_user(user, is_admin=False)
    return TestClient(app)


@pytest.fixture()
def conversation_id(stores: dict[str, Any]) -> str:
    """A conversation Alice owns and Bob can only read."""
    conversation = stores["conversation"].create_conversation(title="alice's session")
    stores["permission"].grant(ALICE, conversation.id, LEVEL_OWNER)
    stores["permission"].grant(BOB, conversation.id, LEVEL_READ)
    return conversation.id


def _headers(email: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": email}


def _create_body(conversation_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": conversation_id,
        "source": "github",
        "kind": "check_run.completed",
        "match": {"repo": "acme/api", "pull_request": 412},
        "note": "CI on my PR",
    }
    body.update(overrides)
    return body


# ── create ───────────────────────────────────────────────────────────────────


def test_create_returns_row_owned_by_caller(client: TestClient, conversation_id: str) -> None:
    response = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner_user_id"] == ALICE
    assert body["conversation_id"] == conversation_id
    assert body["source"] == "github"
    assert body["kind"] == "check_run.completed"
    assert body["match"] == {"repo": "acme/api", "pull_request": 412}
    assert body["note"] == "CI on my PR"
    assert body["state"] == "active"
    assert body["fire_count"] == 0
    assert body["last_fired_at"] is None
    assert len(body["id"]) == 32


def test_create_without_match_or_note_defaults_to_empty_filter(
    client: TestClient, conversation_id: str
) -> None:
    response = client.post(
        "/v1/subscriptions",
        json={"conversation_id": conversation_id, "source": "webhook", "kind": "deploy"},
        headers=_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["match"] == {}
    assert response.json()["note"] is None


def test_create_requires_authentication(client: TestClient, conversation_id: str) -> None:
    response = client.post("/v1/subscriptions", json=_create_body(conversation_id))
    assert response.status_code == 401


def test_create_requires_edit_access_to_conversation(
    client: TestClient, conversation_id: str, stores: dict[str, Any]
) -> None:
    # Bob can read Alice's session but not post into it → 403.
    denied = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers(BOB)
    )
    assert denied.status_code == 403

    # Edit access is the bar, because a wake notice starts a turn.
    stores["permission"].grant(BOB, conversation_id, LEVEL_EDIT)
    allowed = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers(BOB)
    )
    assert allowed.status_code == 200, allowed.text


def test_create_for_unknown_conversation_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/subscriptions", json=_create_body(uuid.uuid4().hex), headers=_headers()
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"source": "slack"}, "source must be one of"),
        ({"kind": "Check Run"}, "kind must look like"),
        ({"kind": "a.b.c"}, "kind must look like"),
        ({"match": {"nested": {"x": 1}}}, "must be a string, number or boolean"),
        ({"match": {"n": None}}, "must be a string, number or boolean"),
        ({"match": {f"k{i}": i for i in range(17)}}, "at most 16 keys"),
        ({"note": "x" * 513}, "at most 512"),
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_create_validation_errors(
    client: TestClient, conversation_id: str, overrides: dict[str, Any], fragment: str
) -> None:
    response = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id, **overrides), headers=_headers()
    )
    assert response.status_code == 422, response.text
    assert fragment in response.text


# ── list / get ───────────────────────────────────────────────────────────────


def test_list_and_get_are_owner_scoped(
    client: TestClient, conversation_id: str, stores: dict[str, Any]
) -> None:
    stores["permission"].grant(BOB, conversation_id, LEVEL_EDIT)
    alice_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers()
    ).json()["id"]
    bob_id = client.post(
        "/v1/subscriptions",
        json=_create_body(conversation_id, note="bob's"),
        headers=_headers(BOB),
    ).json()["id"]

    alice_list = client.get("/v1/subscriptions", headers=_headers()).json()["subscriptions"]
    assert [row["id"] for row in alice_list] == [alice_id]

    assert client.get(f"/v1/subscriptions/{alice_id}", headers=_headers()).status_code == 200
    # Someone else's row 404s (not 403) so ids are not enumerable across users.
    assert client.get(f"/v1/subscriptions/{bob_id}", headers=_headers()).status_code == 404
    unknown = client.get(f"/v1/subscriptions/{uuid.uuid4().hex}", headers=_headers())
    assert unknown.status_code == 404


# ── patch ────────────────────────────────────────────────────────────────────


def test_patch_pauses_resumes_and_edits_note_and_match(
    client: TestClient, conversation_id: str
) -> None:
    subscription_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers()
    ).json()["id"]
    url = f"/v1/subscriptions/{subscription_id}"

    paused = client.patch(url, json={"state": "paused"}, headers=_headers())
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "paused"
    assert paused.json()["updated_at"] is not None

    edited = client.patch(
        url,
        json={"state": "active", "note": None, "match": {"repo": "acme/web"}},
        headers=_headers(),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["state"] == "active"
    assert edited.json()["note"] is None
    assert edited.json()["match"] == {"repo": "acme/web"}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"state": "deleted"}, "use DELETE"),
        ({"state": "bogus"}, "state must be"),
        ({"match": {"k": [1]}}, "must be a string, number or boolean"),
        ({"source": "webhook"}, "Extra inputs"),
    ],
)
def test_patch_validation_errors(
    client: TestClient, conversation_id: str, body: dict[str, Any], fragment: str
) -> None:
    subscription_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers()
    ).json()["id"]

    response = client.patch(f"/v1/subscriptions/{subscription_id}", json=body, headers=_headers())

    assert response.status_code == 422, response.text
    assert fragment in response.text


def test_patch_someone_elses_subscription_is_404(
    client: TestClient, conversation_id: str, stores: dict[str, Any]
) -> None:
    stores["permission"].grant(BOB, conversation_id, LEVEL_EDIT)
    bob_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers(BOB)
    ).json()["id"]

    response = client.patch(
        f"/v1/subscriptions/{bob_id}", json={"state": "paused"}, headers=_headers()
    )

    assert response.status_code == 404


# ── delete ───────────────────────────────────────────────────────────────────


def test_delete_hides_row_and_is_not_repeatable(
    client: TestClient, conversation_id: str, stores: dict[str, Any]
) -> None:
    subscription_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers()
    ).json()["id"]
    url = f"/v1/subscriptions/{subscription_id}"

    deleted = client.delete(url, headers=_headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": subscription_id}

    assert client.get("/v1/subscriptions", headers=_headers()).json()["subscriptions"] == []
    assert client.get(url, headers=_headers()).status_code == 404
    assert client.delete(url, headers=_headers()).status_code == 404
    # The row survives as a tombstone the bus will never deliver to.
    assert stores["subscription"].list_active("github", "check_run.completed") == []


def test_delete_someone_elses_subscription_is_404(
    client: TestClient, conversation_id: str, stores: dict[str, Any]
) -> None:
    stores["permission"].grant(BOB, conversation_id, LEVEL_EDIT)
    bob_id = client.post(
        "/v1/subscriptions", json=_create_body(conversation_id), headers=_headers(BOB)
    ).json()["id"]

    assert client.delete(f"/v1/subscriptions/{bob_id}", headers=_headers()).status_code == 404
    assert client.get(f"/v1/subscriptions/{bob_id}", headers=_headers(BOB)).status_code == 200
