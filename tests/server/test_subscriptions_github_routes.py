"""Route tests for ``/v1/subscriptions/github/{connect,status}``.

A bare FastAPI app mounts the router with header auth, a real GitHub
connection store and a real :class:`GitHubAppClient` whose HTTP transport is
the in-memory :class:`FakeGitHubHooks`, so nothing reaches GitHub.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.events import GITHUB_INGESTED_EVENTS
from omnigent.server.routes.subscriptions_github import create_subscriptions_github_router
from tests.server.github_webhook_fixtures import (
    FakeGitHubHooks,
    connect_github,
    github_connection_store,
)

ALICE = "alice@example.com"
PUBLIC_URL = "https://omni.example.com"
HOOK_URL = f"{PUBLIC_URL}/v1/events/github"
WEBHOOK_VALUE = "test-webhook-value"
_PUBLIC_URL_VARS = (
    "OMNIGENT_ACCOUNTS_BASE_URL",
    "OMNIGENT_DOMAIN",
    "RENDER_EXTERNAL_URL",
    "RAILWAY_PUBLIC_DOMAIN",
    "FLY_APP_NAME",
    "SPACE_HOST",
)


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PUBLIC_URL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", PUBLIC_URL)
    monkeypatch.setenv("OMNIGENT_GITHUB_WEBHOOK_SECRET", WEBHOOK_VALUE)


@pytest.fixture()
def github() -> FakeGitHubHooks:
    return FakeGitHubHooks()


@pytest.fixture()
def store(db_uri: str):
    return github_connection_store(db_uri)


def _client(store: Any, github: FakeGitHubHooks, *, base_url: str = "http://testserver"):
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_subscriptions_github_router(
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            github_store=store,
            github_client=github.client() if store is not None else None,
        ),
        prefix="/v1",
    )
    return TestClient(app, base_url=base_url)


def _connect(client: TestClient, repo: str = "api") -> Any:
    return client.post(
        "/v1/subscriptions/github/connect",
        json={"owner": "acme", "repo": repo},
        headers={"X-Forwarded-Email": ALICE},
    )


def _message(response: Any) -> str:
    return response.json()["error"]["message"]


def test_connect_creates_webhook_with_server_secret(store, github: FakeGitHubHooks) -> None:
    connect_github(store, ALICE)

    response = _connect(_client(store, github))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["events"] == list(GITHUB_INGESTED_EVENTS)
    assert set(body["events"]) == {
        "check_run",
        "check_suite",
        "issue_comment",
        "issues",
        "pull_request",
        "push",
    }
    assert WEBHOOK_VALUE not in response.text
    [hook] = github.hooks["acme/api"]
    assert body["hook_id"] == hook["id"]
    assert hook["config"] == {"url": HOOK_URL, "content_type": "json", "insecure_ssl": "0"}
    assert github.secrets[hook["id"]] == WEBHOOK_VALUE
    post = next(request for request in github.requests if request.method == "POST")
    assert post.headers["Authorization"] == "Bearer ghu_a"


def test_connect_reuses_existing_webhook(store, github: FakeGitHubHooks) -> None:
    connect_github(store, ALICE)
    client = _client(store, github)

    first = _connect(client).json()
    second = _connect(client)

    assert second.status_code == 200, second.text
    assert second.json() == {**first, "status": "existing"}
    assert len(github.hooks["acme/api"]) == 1
    assert github.methods() == ["GET", "POST", "GET"]


def test_connect_adds_missing_events_to_existing_webhook(store, github: FakeGitHubHooks) -> None:
    connect_github(store, ALICE)
    hook_id = github.add_hook("acme/api", HOOK_URL + "/", ["push"])
    github.add_hook("acme/api", "https://elsewhere.example.com/hook", ["issues"])

    response = _connect(_client(store, github))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "existing"
    assert body["hook_id"] == hook_id
    assert sorted(body["events"]) == sorted(GITHUB_INGESTED_EVENTS)
    assert github.methods() == ["GET", "PATCH"]


def test_connect_without_github_connection_points_to_settings(
    store, github: FakeGitHubHooks
) -> None:
    response = _connect(_client(store, github))

    assert response.status_code == 409
    assert "Settings → Sandbox Integrations → Connect GitHub" in _message(response)
    assert github.requests == []


def test_connect_without_github_app_on_server(github: FakeGitHubHooks) -> None:
    response = _connect(_client(None, github))

    assert response.status_code == 409
    assert "GITHUB_APP_SETUP" in _message(response)


@pytest.mark.parametrize("status", [403, 404])
def test_connect_without_webhook_permission(store, github: FakeGitHubHooks, status: int) -> None:
    connect_github(store, ALICE)
    github.fail_status = status

    response = _connect(_client(store, github))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "upstream_permission_denied"
    assert "'Webhooks: Read and write'" in _message(response)
    assert "acme/api" in _message(response)


def test_connect_reports_other_github_failures_as_bad_gateway(
    store, github: FakeGitHubHooks
) -> None:
    connect_github(store, ALICE)
    github.fail_status = 500

    response = _connect(_client(store, github))

    assert response.status_code == 502


def test_connect_without_webhook_secret(
    store, github: FakeGitHubHooks, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_github(store, ALICE)
    monkeypatch.delenv("OMNIGENT_GITHUB_WEBHOOK_SECRET")

    response = _connect(_client(store, github))

    assert response.status_code == 409
    assert "OMNIGENT_GITHUB_WEBHOOK_SECRET is not set" in _message(response)
    assert github.requests == []


def test_connect_without_public_url(
    store, github: FakeGitHubHooks, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_github(store, ALICE)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://127.0.0.1:6767")

    response = _connect(_client(store, github, base_url="http://localhost:6767"))

    assert response.status_code == 409
    assert "no public URL" in _message(response)
    assert "http://127.0.0.1:6767" in _message(response)
    assert github.requests == []


def test_connect_falls_back_to_the_platform_domain(
    store, github: FakeGitHubHooks, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_github(store, ALICE)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://0.0.0.0:8000")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "omni.up.railway.app")

    response = _connect(_client(store, github))

    assert response.status_code == 200, response.text
    [hook] = github.hooks["acme/api"]
    assert hook["config"]["url"] == "https://omni.up.railway.app/v1/events/github"


def test_connect_rejects_path_smuggling_repository_names(store, github: FakeGitHubHooks) -> None:
    connect_github(store, ALICE)

    response = _connect(_client(store, github), repo="..")

    assert response.status_code == 400
    assert github.requests == []


def test_status_reports_whether_the_webhook_exists(store, github: FakeGitHubHooks) -> None:
    client = _client(store, github)
    headers = {"X-Forwarded-Email": ALICE}
    params = {"owner": "acme", "repo": "api"}

    unconnected = client.get("/v1/subscriptions/github/status", params=params, headers=headers)
    assert unconnected.json() == {
        "repo": "acme/api",
        "connected": False,
        "hook_exists": False,
        "hook_id": None,
        "events": [],
    }

    connect_github(store, ALICE)
    before = client.get("/v1/subscriptions/github/status", params=params, headers=headers)
    assert before.json()["connected"] is True
    assert before.json()["hook_exists"] is False

    hook_id = _connect(client).json()["hook_id"]
    after = client.get("/v1/subscriptions/github/status", params=params, headers=headers)
    assert after.status_code == 200
    assert after.json()["hook_exists"] is True
    assert after.json()["hook_id"] == hook_id
