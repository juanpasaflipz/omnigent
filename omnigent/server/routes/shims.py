"""API route for the shim registry report (``GET /v1/shims``)."""

from __future__ import annotations

import asyncio
import importlib
import logging

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.runtime.shims import ShimStatus, snapshot
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_admin
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

_DEFAULT_IDLE_DAYS = 30
_MAX_IDLE_DAYS = 3650

# Shims register at import time. The server does not otherwise import every
# module that declares one (tool dispatch runs in the runner), so the report
# loads them first; the firing log then supplies the runner-side counts.
_SHIM_DECLARING_MODULES: tuple[str, ...] = (
    "omnigent.runtime.workflow",
    "omnigent.runtime.llm_retry",
    "omnigent.runner.tool_dispatch",
)


class ShimStatusResponse(BaseModel):
    """
    One registered shim with its lifetime firing record.

    :param name: Stable dotted identifier, e.g.
        ``"compaction.bare_model_provider_prefix"``.
    :param reason: Why the patch exists.
    :param model_gap: The model or provider behaviour the patch covers.
    :param added_in: Omnigent version that introduced the shim.
    :param review_by: Version by which the shim must be re-justified or deleted.
    :param owner: Team or module that owns the shim.
    :param fire_count: Lifetime firings across the server, the runner and the
        firing log on this machine.
    :param last_fired_at: ISO-8601 UTC timestamp of the latest firing, or
        ``None`` when it never fired.
    :param by_model: Firings per model id; ``"unknown"`` when the call site has
        no model in scope.
    :param retirable: ``True`` when the shim has been idle for ``idle_days``.
    """

    name: str
    reason: str
    model_gap: str
    added_in: str
    review_by: str
    owner: str
    fire_count: int = Field(ge=0)
    last_fired_at: str | None
    by_model: dict[str, int]
    retirable: bool


class ShimReport(BaseModel):
    """
    The ``GET /v1/shims`` payload.

    :param idle_days: The idleness window ``retirable`` was computed against.
    :param shims: Every registered shim, in registration order.
    """

    idle_days: int
    shims: list[ShimStatusResponse]


def _load_shim_declaring_modules() -> None:
    """Import the modules whose registrations the report must include."""
    for module_name in _SHIM_DECLARING_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            _logger.warning("could not load shim module %s", module_name, exc_info=True)


def _build_shim_report(idle_days: int) -> ShimReport:
    """
    Assemble the report from the registry and the firing log.

    :param idle_days: Days without a firing after which a shim is retirable.
    :returns: The report payload.
    """
    _load_shim_declaring_modules()
    statuses: list[ShimStatus] = snapshot(idle_days=idle_days)
    return ShimReport(
        idle_days=idle_days,
        shims=[ShimStatusResponse(**status.__dict__) for status in statuses],
    )


def create_shims_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """
    Create the shim registry report router.

    :param auth_provider: Auth provider for user identity. ``None`` disables
        auth (single-user / local mode).
    :param permission_store: Permission store for the admin check. Firing
        counts are process-wide and span every workspace, so the report is
        admin-only in multi-user deployments.
    :returns: The configured router (mounted under ``/v1``).
    """
    router = APIRouter()

    @router.get("/shims", response_model=ShimReport)
    async def get_shims(
        request: Request,
        idle_days: int = Query(default=_DEFAULT_IDLE_DAYS, ge=1, le=_MAX_IDLE_DAYS),
    ) -> ShimReport:
        """
        List every registered shim with its firing record.

        :param request: The incoming request, for identity.
        :param idle_days: Days without a firing after which a shim is retirable.
        :returns: The shim report.
        """
        await require_admin(
            request, auth_provider, permission_store, action="read the shim registry"
        )
        return await asyncio.to_thread(_build_shim_report, idle_days)

    return router
