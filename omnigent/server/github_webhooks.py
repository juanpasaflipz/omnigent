"""Register this server's subscription webhook on a user's GitHub repository.

The web UI's "Connect a repository" flow and the software-factory template
both need GitHub to deliver repository events to ``POST /v1/events/github``.
This module creates (or reuses) that repository webhook with the caller's own
GitHub connection token, so nobody has to copy a payload URL or the webhook
secret by hand. The secret never leaves the server. See
``docs/SUBSCRIPTIONS.md`` ("From the web UI").
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from httpx import HTTPError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.github_app import GitHubAppError
from omnigent.server.github_app_client import GitHubApiError
from omnigent.server.github_identity import resolve_access_token
from omnigent.server.paas_env import resolve_public_base_url
from omnigent.server.routes.connections_github import _valid_github_name
from omnigent.server.routes.events import GITHUB_INGESTED_EVENTS, GITHUB_WEBHOOK_SECRET_ENV_VAR

_logger = logging.getLogger(__name__)

# Path of the signed GitHub ingest route the webhook delivers to.
GITHUB_EVENTS_PATH = "/v1/events/github"

NO_GITHUB_APP_MESSAGE = (
    "GitHub is not set up on this server, so repositories cannot be connected. "
    "An operator must configure the GitHub App (docs/GITHUB_APP_SETUP.md)."
)
NO_GITHUB_CONNECTION_MESSAGE = (
    "Connect your GitHub account first: Settings → Sandbox Integrations → Connect GitHub."
)
WEBHOOK_PERMISSION_MESSAGE = (
    "GitHub refused to manage webhooks on {repo}. The GitHub App needs the Repository "
    "permission 'Webhooks: Read and write', must be installed on the repository's owner, "
    "and you need admin access to the repository. After the App's permissions change, "
    "accept them under Settings → Applications → Installed GitHub Apps."
)
WEBHOOK_SECRET_MISSING_MESSAGE = (
    f"{GITHUB_WEBHOOK_SECRET_ENV_VAR} is not set on the server, so GitHub deliveries "
    "cannot be verified. Set it (for example to the output of `openssl rand -hex 32`) "
    "and restart the server."
)
NO_PUBLIC_URL_MESSAGE = (
    "This server has no public URL that GitHub can reach (it answers on {candidate}). "
    "Deploy it with a public address, or set OMNIGENT_ACCOUNTS_BASE_URL or "
    "OMNIGENT_DOMAIN to its public URL."
)


@dataclass(frozen=True)
class RepositoryHook:
    """Outcome of connecting a repository.

    :param status: ``"created"`` for a new webhook, ``"existing"`` when one
        already pointed at this server.
    :param hook_id: GitHub's numeric webhook id.
    :param events: The GitHub events the webhook delivers.
    """

    status: str
    hook_id: int
    events: list[str]

    def to_response(self) -> dict[str, Any]:
        """The JSON body returned to the client (never includes the secret)."""
        return {"status": self.status, "hook_id": self.hook_id, "events": self.events}


def validate_repository(owner: str, repo: str) -> str:
    """Return ``owner/repo`` after checking both are safe GitHub name segments.

    :raises OmnigentError: ``INVALID_INPUT`` for a name that could smuggle a path.
    """
    if not _valid_github_name(owner) or not _valid_github_name(repo):
        raise OmnigentError("Invalid repository name", code=ErrorCode.INVALID_INPUT)
    return f"{owner}/{repo}"


def subscription_webhook_url(request: Request) -> str:
    """The public URL GitHub should deliver repository events to.

    :raises OmnigentError: ``CONFLICT`` when the server only knows a local
        address (``localhost``, a private IP), which GitHub cannot reach.
    """
    base_path = getattr(request.app.state, "base_path", "") or ""
    request_base = f"{request.url.scheme}://{request.url.netloc}{base_path}"
    public, candidate = resolve_public_base_url(os.environ, request_base)
    if public is None:
        raise OmnigentError(
            NO_PUBLIC_URL_MESSAGE.format(candidate=candidate or request_base),
            code=ErrorCode.CONFLICT,
        )
    return f"{public}{GITHUB_EVENTS_PATH}"


def subscription_webhook_secret() -> str:
    """The shared secret the ingest route verifies deliveries with.

    Read per call, like the ingest route, so a rotated secret applies at once.

    :raises OmnigentError: ``CONFLICT`` when the secret is unset.
    """
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV_VAR) or ""
    if not secret:
        raise OmnigentError(WEBHOOK_SECRET_MISSING_MESSAGE, code=ErrorCode.CONFLICT)
    return secret


async def github_token_for(user_id: str | None, *, store: Any, client: Any) -> str | None:
    """The caller's GitHub user access token, or ``None`` when not connected.

    :raises OmnigentError: ``CONFLICT`` when the server has no GitHub App.
    """
    if store is None or client is None:
        raise OmnigentError(NO_GITHUB_APP_MESSAGE, code=ErrorCode.CONFLICT)
    return await resolve_access_token(
        user_id if user_id is not None else RESERVED_USER_LOCAL, store=store, client=client
    )


def _github_failure(exc: Exception, full_name: str) -> Exception:
    """Translate a GitHub call failure into the error the client should see."""
    if isinstance(exc, GitHubApiError) and exc.status_code in (403, 404):
        return OmnigentError(
            WEBHOOK_PERMISSION_MESSAGE.format(repo=full_name),
            code=ErrorCode.UPSTREAM_PERMISSION_DENIED,
        )
    _logger.warning("GitHub webhook call for %s failed: %s", full_name, exc)
    return HTTPException(status_code=502, detail=f"GitHub request for {full_name} failed")


def _hook_events(hook: dict[str, Any]) -> list[str]:
    events = hook.get("events")
    return [str(event) for event in events] if isinstance(events, list) else []


async def find_repository_hook(
    client: Any, token: str, full_name: str, url: str
) -> dict[str, Any] | None:
    """The webhook on *full_name* that already delivers to *url*, if any."""
    try:
        hooks = await client.list_repo_hooks(token, full_name)
    except (GitHubAppError, HTTPError, ValueError) as exc:
        raise _github_failure(exc, full_name) from exc
    for hook in hooks:
        config = hook.get("config")
        if isinstance(config, dict) and str(config.get("url", "")).rstrip("/") == url:
            return hook
    return None


async def ensure_repository_hook(
    client: Any, token: str, full_name: str, *, url: str, secret: str
) -> RepositoryHook:
    """Create the subscription webhook on *full_name*, or reuse the one already there.

    A reused webhook that lacks some ingested events gets them added; its URL
    and secret are left as they are.
    """
    wanted = list(GITHUB_INGESTED_EVENTS)
    existing = await find_repository_hook(client, token, full_name, url)
    if existing is None:
        try:
            created = await client.create_repo_hook(
                token, full_name, url=url, secret=secret, events=wanted
            )
        except GitHubApiError as exc:
            # 422: a webhook for this URL appeared since the listing (a race).
            if exc.status_code != 422:
                raise _github_failure(exc, full_name) from exc
            existing = await find_repository_hook(client, token, full_name, url)
            if existing is None:
                raise _github_failure(exc, full_name) from exc
        except (GitHubAppError, HTTPError, ValueError) as exc:
            raise _github_failure(exc, full_name) from exc
        else:
            _logger.info("Created subscription webhook %s on %s", created.get("id"), full_name)
            return RepositoryHook("created", int(created["id"]), _hook_events(created))
    events = _hook_events(existing)
    missing = [event for event in wanted if event not in events and "*" not in events]
    if missing:
        try:
            updated = await client.add_repo_hook_events(
                token, full_name, int(existing["id"]), missing
            )
        except (GitHubAppError, HTTPError, ValueError) as exc:
            raise _github_failure(exc, full_name) from exc
        events = _hook_events(updated) or [*events, *missing]
    return RepositoryHook("existing", int(existing["id"]), events)


async def connect_repository(
    request: Request,
    user_id: str | None,
    owner: str,
    repo: str,
    *,
    store: Any,
    client: Any,
) -> RepositoryHook:
    """Make *owner*/*repo* deliver its events to this server, as *user_id*.

    Checks, in order: the repository name, the webhook secret, the public
    URL, then the caller's GitHub connection, so every failure is a typed
    4xx with a message the UI shows verbatim.
    """
    full_name = validate_repository(owner, repo)
    secret = subscription_webhook_secret()
    url = subscription_webhook_url(request)
    token = await github_token_for(user_id, store=store, client=client)
    if token is None:
        raise OmnigentError(NO_GITHUB_CONNECTION_MESSAGE, code=ErrorCode.CONFLICT)
    return await ensure_repository_hook(client, token, full_name, url=url, secret=secret)
