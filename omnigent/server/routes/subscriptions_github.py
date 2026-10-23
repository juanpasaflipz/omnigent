"""Connect a GitHub repository to subscriptions (``/v1/subscriptions/github``).

``POST /v1/subscriptions/github/connect`` registers this server's
``/v1/events/github`` webhook on one of the caller's repositories with their
GitHub connection token; ``GET /v1/subscriptions/github/status`` reports
whether that webhook exists. The shared logic lives in
:mod:`omnigent.server.github_webhooks`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.github_webhooks import (
    connect_repository,
    find_repository_hook,
    github_token_for,
    subscription_webhook_url,
    validate_repository,
)
from omnigent.server.routes._auth_helpers import require_user

# GitHub caps owner logins at 39 characters and repository names at 100.
_MAX_GITHUB_NAME_CHARS = 100


class ConnectRepositoryRequest(BaseModel):
    """Body for ``POST /v1/subscriptions/github/connect``."""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)
    repo: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)


def create_subscriptions_github_router(
    *,
    auth_provider: AuthProvider | None = None,
    github_store: Any | None = None,
    github_client: Any | None = None,
) -> APIRouter:
    """Build the repository-connect router.

    Mounted with ``prefix="/v1"`` next to the subscriptions router.

    :param auth_provider: Identifies the caller; ``None`` disables auth.
    :param github_store: The per-user GitHub connection store, or ``None``
        when the server has no GitHub App (every call then answers 409).
    :param github_client: The GitHub App client used for the REST calls.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/subscriptions/github/connect")
    async def connect_github_repository(
        request: Request, body: ConnectRepositoryRequest
    ) -> dict[str, Any]:
        """Create (or reuse) this server's webhook on ``owner/repo``.

        Returns ``{status: "created" | "existing", hook_id, events}``. The
        webhook secret is set on GitHub but never returned.
        """
        user_id = require_user(request, auth_provider)
        hook = await connect_repository(
            request, user_id, body.owner, body.repo, store=github_store, client=github_client
        )
        return hook.to_response()

    @router.get("/subscriptions/github/status")
    async def github_repository_status(
        request: Request,
        owner: str = Query(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS),
        repo: str = Query(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS),
    ) -> dict[str, Any]:
        """Report whether this server's webhook exists on ``owner/repo``.

        ``connected: false`` (and no hook) when the caller has not linked
        GitHub, so the UI can point them at Settings instead of failing.
        """
        user_id = require_user(request, auth_provider)
        full_name = validate_repository(owner, repo)
        url = subscription_webhook_url(request)
        token = await github_token_for(user_id, store=github_store, client=github_client)
        if token is None:
            return {
                "repo": full_name,
                "connected": False,
                "hook_exists": False,
                "hook_id": None,
                "events": [],
            }
        hook = await find_repository_hook(github_client, token, full_name, url)
        return {
            "repo": full_name,
            "connected": True,
            "hook_exists": hook is not None,
            "hook_id": hook.get("id") if hook is not None else None,
            "events": hook.get("events", []) if hook is not None else [],
        }

    return router
