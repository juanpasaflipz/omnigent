"""In-memory GitHub repository-webhooks API for route tests.

Not a test module. :class:`FakeGitHubHooks` answers the ``/repos/{owner}/{repo}/hooks``
REST calls through ``httpx.MockTransport``, so a real
:class:`~omnigent.server.github_app_client.GitHubAppClient` is exercised down
to its HTTP requests without reaching GitHub.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.github_app_client import GitHubAppClient
from tests.server.github_app_fixtures import make_config

_HOOKS_PATH = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/hooks(?:/(?P<hook_id>\d+))?$")


class FakeGitHubHooks:
    """Per-repository webhook lists, plus a log of every request made."""

    def __init__(self) -> None:
        self.hooks: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.secrets: dict[int, str] = {}
        # When set, every call answers with this status (403, 404, 500 …).
        self.fail_status: int | None = None
        self._next_id = 1000

    def client(self) -> GitHubAppClient:
        return GitHubAppClient(make_config(), transport=httpx.MockTransport(self.handle))

    def add_hook(self, repo: str, url: str, events: list[str]) -> int:
        hook_id = self._next_id
        self._next_id += 1
        self.hooks.setdefault(repo, []).append(
            {
                "id": hook_id,
                "name": "web",
                "active": True,
                "events": list(events),
                "config": {"url": url, "content_type": "json", "insecure_ssl": "0"},
            }
        )
        return hook_id

    def methods(self) -> list[str]:
        return [request.method for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"message": "refused"})
        matched = _HOOKS_PATH.match(request.url.path)
        if matched is None:
            return httpx.Response(404, json={"message": "Not Found"})
        repo = matched["repo"]
        hooks = self.hooks.setdefault(repo, [])
        if request.method == "GET":
            return httpx.Response(200, json=hooks)
        body = json.loads(request.content)
        if request.method == "POST":
            config = dict(body["config"])
            hook_id = self.add_hook(repo, config["url"], body["events"])
            self.secrets[hook_id] = config["secret"]
            return httpx.Response(201, json=hooks[-1])
        if request.method == "PATCH":
            hook = next(h for h in hooks if h["id"] == int(matched["hook_id"]))
            hook["events"] = [*hook["events"], *body.get("add_events", [])]
            return httpx.Response(200, json=hook)
        return httpx.Response(405)


class SecretBox:
    """Test double for the KMS SecretCipher: key- and context-bound."""

    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        payload = {"k": self._key, "c": dict(context), "p": plaintext}
        return base64.b64encode(json.dumps(payload).encode()).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        decoded = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        return (
            decoded["p"] if decoded["k"] == self._key and decoded["c"] == dict(context) else None
        )


def github_connection_store(db_uri: str) -> GithubConnectionStore:
    return GithubConnectionStore(db_uri, SecretBox("store-enc-key"))


def connect_github(store: GithubConnectionStore, user_id: str) -> None:
    """Record a non-expiring GitHub connection for *user_id*."""
    store.upsert(
        user_id,
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
