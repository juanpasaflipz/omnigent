"""REST CRUD for event subscriptions (``/v1/subscriptions``).

A subscription ties one session to external events of a ``(source, kind)``
whose fields satisfy a filter; the server's event bus posts a wake notice into
the session when one arrives. These endpoints let a client (or, via the
runner, an agent) create, list, read, update and delete subscriptions.

Ownership mirrors scheduled tasks: rows are scoped to the calling user
(``"local"`` when auth is disabled) and a row owned by someone else 404s so
subscriptions are not enumerable across users. Creating one additionally
requires edit access to the target conversation, since the wake notice starts
a turn there.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import Subscription, SubscriptionState
from omnigent.entities.subscription import SUBSCRIPTION_SOURCES
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.stores.subscription_store import SubscriptionStore

# ``kind`` is a dotted lowercase identifier: ``push``, ``check_run.completed``.
KIND_PATTERN = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")
# Bound the filter so a subscription cannot carry an arbitrary document.
MAX_MATCH_KEYS = 16
MAX_NOTE_CHARS = 512
_SCALAR_TYPES = (str, int, float, bool)


def _validate_match(match: dict[str, Any] | None) -> dict[str, Any]:
    """
    Validate a ``match`` filter: at most :data:`MAX_MATCH_KEYS` scalar values.

    :param match: The caller-supplied filter, or ``None``.
    :returns: The filter (empty dict for ``None``).
    :raises ValueError: On too many keys, empty keys or non-scalar values.
    """
    if match is None:
        return {}
    if len(match) > MAX_MATCH_KEYS:
        raise ValueError(f"match may have at most {MAX_MATCH_KEYS} keys")
    for key, value in match.items():
        if not key:
            raise ValueError("match keys must be non-empty strings")
        if value is None or not isinstance(value, _SCALAR_TYPES):
            raise ValueError(f"match value for {key!r} must be a string, number or boolean")
    return match


class CreateSubscriptionRequest(BaseModel):
    """Body for ``POST /v1/subscriptions``."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    source: str
    kind: str
    match: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)

    @model_validator(mode="after")
    def _validate_create(self) -> CreateSubscriptionRequest:
        if self.source not in SUBSCRIPTION_SOURCES:
            raise ValueError(f"source must be one of {sorted(SUBSCRIPTION_SOURCES)}")
        if not KIND_PATTERN.fullmatch(self.kind):
            raise ValueError("kind must look like 'push' or 'check_run.completed'")
        self.match = _validate_match(self.match)
        return self


class UpdateSubscriptionRequest(BaseModel):
    """Body for ``PATCH /v1/subscriptions/{id}``. Unset fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    state: str | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    match: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateSubscriptionRequest:
        if self.state is not None and self.state not in {
            SubscriptionState.ACTIVE.value,
            SubscriptionState.PAUSED.value,
        }:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete")
        if self.match is not None:
            self.match = _validate_match(self.match)
        return self


def _to_response(subscription: Subscription) -> dict[str, Any]:
    """Serialize a :class:`Subscription` to a JSON-safe dict."""
    return {
        "id": subscription.id,
        "owner_user_id": subscription.user_id,
        "conversation_id": subscription.conversation_id,
        "source": subscription.source,
        "kind": subscription.kind,
        "match": subscription.match,
        "note": subscription.note,
        "state": subscription.state,
        "created_at": subscription.created_at,
        "updated_at": subscription.updated_at,
        "last_fired_at": subscription.last_fired_at,
        "fire_count": subscription.fire_count,
    }


def create_subscriptions_router(
    store: SubscriptionStore,
    *,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the subscriptions router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/subscriptions[/{id}]``.

    :param store: The shared :class:`SubscriptionStore`.
    :param conversation_store: Resolves the target conversation on create.
    :param permission_store: Grants checked for conversation access; ``None``
        skips the check (single-user mode).
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` disables auth (owner resolves to ``"local"``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        """Resolve the calling user, mapping the auth-disabled case to
        ``RESERVED_USER_LOCAL`` so single-user rows are always owned."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _require_owned(subscription_id: str, owner_id: str | None) -> Subscription:
        """Load a subscription the caller owns, or raise 404."""
        subscription = store.get(subscription_id)
        if (
            subscription is None
            or subscription.state == SubscriptionState.DELETED.value
            or subscription.user_id != owner_id
        ):
            raise OmnigentError("Subscription not found", code=ErrorCode.NOT_FOUND)
        return subscription

    @router.post("/subscriptions")
    async def create_subscription(
        request: Request,
        body: CreateSubscriptionRequest,
    ) -> dict[str, Any]:
        """Create a subscription for a conversation the caller may edit."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        # Wake notices start turns in the target session, so the caller needs
        # edit access there (same bar as posting a message).
        await require_access(
            owner_id, body.conversation_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if (
            permission_store is None
            and conversation_store.get_conversation(body.conversation_id) is None
        ):
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        subscription = store.create(
            uuid.uuid4().hex,
            user_id=owner_id,
            conversation_id=body.conversation_id,
            source=body.source,
            kind=body.kind,
            match=body.match,
            note=body.note,
        )
        return _to_response(subscription)

    @router.get("/subscriptions")
    async def list_subscriptions(request: Request) -> dict[str, list[dict[str, Any]]]:
        """List the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        return {"subscriptions": [_to_response(s) for s in store.list_for_user(owner_id)]}

    @router.get("/subscriptions/{subscription_id}")
    async def get_subscription(request: Request, subscription_id: str) -> dict[str, Any]:
        """Fetch one of the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        return _to_response(_require_owned(subscription_id, owner_id))

    @router.patch("/subscriptions/{subscription_id}")
    async def update_subscription(
        request: Request,
        subscription_id: str,
        body: UpdateSubscriptionRequest,
    ) -> dict[str, Any]:
        """Pause/resume a subscription or change its note / filter."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(subscription_id, owner_id)
        fields = body.model_dump(exclude_unset=True)
        kwargs: dict[str, Any] = {}
        if "state" in fields:
            kwargs["state"] = fields["state"]
        if "note" in fields:
            kwargs["note"] = fields["note"]
        if "match" in fields and fields["match"] is not None:
            kwargs["match"] = fields["match"]
        updated = store.update(subscription_id, **kwargs)
        if updated is None:
            raise OmnigentError("Subscription not found", code=ErrorCode.NOT_FOUND)
        return _to_response(updated)

    @router.delete("/subscriptions/{subscription_id}")
    async def delete_subscription(request: Request, subscription_id: str) -> dict[str, Any]:
        """Soft-delete one of the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(subscription_id, owner_id)
        store.delete(subscription_id)
        return {"deleted": True, "id": subscription_id}

    return router
