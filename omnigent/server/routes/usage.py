"""API route for the per-user LLM cost report (``omni usage``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.entities import Conversation
from omnigent.errors import restart_on_stale_cursor
from omnigent.runtime.policies.builder import load_session_usage_and_tree
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._sessions.helpers import (
    _resolve_harness_impl,
    _resolve_llm_model,
)
from omnigent.server.schemas import DailyCost, EfficiencyReport, SessionUsage, UsageReport
from omnigent.server.usage_efficiency import aggregate, score_session, session_cost_usd
from omnigent.stores import ConversationStore

# The daily rollup floor for an "all-time" sum: earlier than any real row, so
# ``sum_daily_cost`` with this lower bound totals every recorded day.
_EPOCH_DAY = "0000-00-00"


def _utc_today() -> str:
    """Return the current UTC calendar day as ``"YYYY-MM-DD"``."""
    from omnigent.db.utils import now_epoch

    return datetime.fromtimestamp(now_epoch(), tz=UTC).date().isoformat()


def _day_offset(day_utc: str, *, days: int) -> str:
    """Return the UTC day *days* before *day_utc*, as ``"YYYY-MM-DD"``."""
    base = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=UTC)
    return (base - timedelta(days=days)).date().isoformat()


def _window_start_epoch(today_utc: str, *, days: int) -> int:
    """
    Return the Unix epoch of 00:00 UTC on the first day of a *days*-day window.

    Same inclusive calendar-day semantics as the ``cost_last_7d`` /
    ``cost_last_30d`` summary: today plus the ``days - 1`` prior UTC days.

    :param today_utc: The current UTC day, ``"YYYY-MM-DD"``.
    :param days: Window length in days, at least 1.
    :returns: Epoch seconds of the window's first midnight.
    """
    first_day = _day_offset(today_utc, days=days - 1)
    return int(datetime.strptime(first_day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _session_models(usage: dict[str, Any]) -> dict[str, float]:
    """
    Project a session's ``by_model`` map into a ``{model_id: cost_usd}`` dict.

    Mirrors the web session sidebar's per-model list: each model's recorded
    cost, keyed by the raw harness model id, shown faithfully. Models with no
    recorded cost are omitted. NOT guaranteed to sum to the session's
    ``total_cost_usd`` — see :class:`SessionUsage`.

    :param usage: A subtree-summed ``session_usage`` dict.
    :returns: Per-model cost map (empty when no per-model cost was recorded).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict):
        return {}
    models: dict[str, float] = {}
    for name, bucket in by_model.items():
        if not isinstance(bucket, dict) or "total_cost_usd" not in bucket:
            continue
        try:
            models[str(name)] = float(bucket["total_cost_usd"])
        except (TypeError, ValueError):
            continue
    return models


def _collect_other_harnesses(
    primary: str | None,
    tree: list[Conversation],
    root_id: str,
) -> list[str] | None:
    """Distinct harnesses used by sub-agents, excluding the primary."""
    seen: set[str] = set()
    for conv in tree:
        if conv.id == root_id:
            continue
        h = _resolve_session_harness(conv)
        if h and h != primary:
            seen.add(h)
    return sorted(seen) if seen else None


def _resolve_session_harness(conv: Conversation) -> str | None:
    """
    Best-effort harness resolution for the usage report.

    Checks, in order: (1) per-session harness override, (2) the
    ``omnigent.wrapper`` label stamped at creation by native wrappers,
    (3) agent-spec resolution via the runtime (works when the server has
    a live agent cache). Returns a display-friendly name or ``None``.
    """
    if conv.harness_override:
        return conv.harness_override
    wrapper = conv.labels.get(WRAPPER_LABEL_KEY)
    if wrapper:
        # Strip the "-ui" suffix for display: "claude-code-native-ui" → "claude-code-native"
        return wrapper.removesuffix("-ui") if wrapper.endswith("-ui") else wrapper
    return _resolve_harness_impl(conv)


def _session_cost(usage: dict[str, Any]) -> float:
    """
    Read a session's authoritative cumulative cost, or ``0.0`` when unpriced.

    ``total_cost_usd`` is present only on priced sessions (the "priced ⟺ key
    present" contract); an absent or malformed value reads as ``0.0``.
    Shared with the efficiency scorecard so both reports price a session
    identically.
    """
    return session_cost_usd(usage)


def _walk_session_usage(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    updated_since: int | None = None,
) -> Iterator[tuple[Conversation, dict[str, Any], list[Conversation]]]:
    """
    Yield every top-level session the caller can read, with its subtree usage.

    One walk serves both the cost report and the efficiency scorecard so the
    two stay consistent: same listing filters, same user scoping, same
    newest-activity-first order. Each session's sums and spawn tree come
    from ONE tree load (see :func:`load_session_usage_and_tree`).

    Pages a cursor to completion, so the caller must run under
    :func:`restart_on_stale_cursor` and consume the iterator fully in one
    call (a deleted cursor row raises mid-walk and the whole walk restarts).

    :param conversation_store: Store to page sessions from.
    :param user_id: The caller / ACL scope (``None`` in single-user mode).
    :param updated_since: When set, stop at the first session whose
        ``updated_at`` is older than this epoch. Valid because the walk is
        ordered by ``updated_at`` descending.
    :returns: ``(conversation, usage, tree)`` per session.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            limit=200,
            after=after,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
        )
        for conv in page.data:
            if conv.agent_id is None:
                continue
            if updated_since is not None and conv.updated_at < updated_since:
                return
            # The list page already holds each row, so pass its root to skip
            # re-reading the same conversation once per listed session.
            usage, tree = load_session_usage_and_tree(
                conv.id,
                conversation_store,
                root_conversation_id=conv.root_conversation_id,
            )
            yield conv, usage, tree
        if not page.has_more:
            return
        after = page.last_id


@restart_on_stale_cursor
def _build_efficiency_report(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    days: int,
    limit: int,
) -> EfficiencyReport:
    """
    Build the efficiency scorecard over the caller's recent sessions.

    Walks the same sessions as :func:`_build_usage_report` (via
    :func:`_walk_session_usage`), keeps those whose last activity falls in
    the last *days* UTC calendar days, scores each from its cumulative
    subtree usage and rolls them up with
    :func:`omnigent.server.usage_efficiency.aggregate`. The harness is
    always resolved here (the scorecard groups by it), independent of the
    ``usage_page`` flag that gates it on the cost report.

    :param conversation_store: Store to read sessions from.
    :param user_id: The caller / ACL scope (``None`` in single-user mode).
    :param days: Window length in UTC days, including today.
    :param limit: Maximum number of session rows in the result.
    :returns: The populated :class:`EfficiencyReport`.
    """
    window_start = _window_start_epoch(_utc_today(), days=days)
    scored = [
        score_session(
            usage,
            session_id=conv.id,
            created_at=conv.created_at,
            title=conv.title,
            harness=_resolve_session_harness(conv),
        )
        for conv, usage, _tree in _walk_session_usage(
            conversation_store, user_id, updated_since=window_start
        )
    ]
    return aggregate(scored, window_days=days, limit=limit)


@restart_on_stale_cursor
def _build_usage_report(
    conversation_store: ConversationStore,
    user_id: str | None,
    *,
    include_page_details: bool = False,
) -> UsageReport:
    """
    Build the usage report: a daily-rollup cost summary plus session detail.

    A session deleted while the detail enumeration is paging invalidates the
    page cursor; the whole report is rebuilt (via
    :func:`restart_on_stale_cursor`) so the detail list is never silently
    truncated.

    The summary (today / last 7 days / last 30 days / all-time) is summed
    from the per-user daily-cost rollup (``user_daily_cost``), which
    attributes spend to the UTC day it occurred on — so the windows reflect
    when spend actually happened, not merely a session's last-activity time.

    The per-session detail is a separate view over each top-level session's
    cumulative ``session_usage`` (rolled up across its sub-agent subtree via
    :func:`load_session_usage_and_tree`), newest activity first, carrying the
    authoritative session cost and the per-model breakdown.

    :param conversation_store: Store to read the rollup and sessions from.
    :param user_id: The caller / ACL scope. ``None`` in single-user mode maps
        to the reserved local owner the daily rollup and grants are keyed by.
    :param include_page_details: Populate the timeline and display metadata
        used only by the release-gated web Usage page.
    :returns: The populated :class:`UsageReport`.
    """
    # The daily rollup and session-permission grants key spend by the resolved
    # owner, which is the reserved local sentinel in single-user mode (where
    # require_user yields None). Map None -> "local" so the summary reads the
    # same rows the write path recorded.
    rollup_user = user_id if user_id is not None else RESERVED_USER_LOCAL

    today = _utc_today()
    cost_today = conversation_store.sum_daily_cost(rollup_user, today)
    cost_7d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=6))
    cost_30d = conversation_store.sum_daily_cost(rollup_user, _day_offset(today, days=29))
    total = conversation_store.sum_daily_cost(rollup_user, _EPOCH_DAY)

    sessions: list[SessionUsage] = []
    for conv, usage, tree in _walk_session_usage(conversation_store, user_id):
        # The harness roll-up needs the same tree the sums came from.
        primary_harness = _resolve_session_harness(conv) if include_page_details else None
        other_harnesses = (
            _collect_other_harnesses(primary_harness, tree, conv.id)
            if include_page_details
            else None
        )
        sessions.append(
            SessionUsage(
                id=conv.id,
                created_at=conv.created_at,
                updated_at=conv.updated_at,
                title=conv.title,
                cost_usd=_session_cost(usage),
                models=_session_models(usage),
                harness=primary_harness,
                other_harnesses=other_harnesses,
                llm_model=(
                    conv.model_override or _resolve_llm_model(conv)
                    if include_page_details
                    else None
                ),
                agent_name=conv.sub_agent_name if include_page_details else None,
            )
        )

    daily_costs_raw = (
        conversation_store.list_daily_costs(rollup_user, _EPOCH_DAY)
        if include_page_details
        else []
    )

    return UsageReport(
        cost_today=cost_today,
        cost_last_7d=cost_7d,
        cost_last_30d=cost_30d,
        total_cost_usd=total,
        daily_costs=[DailyCost(day=d, cost_usd=c) for d, c in daily_costs_raw],
        sessions=sessions,
    )


def create_usage_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    feature_flags: FeatureFlags | None = None,
) -> APIRouter:
    """
    Create the per-user usage-report router.

    The report is user-scoped, not session-scoped, so it lives in its own
    router rather than under the sessions router.

    :param conversation_store: Store for the daily rollup and session reads.
    :param auth_provider: Auth provider for user identity. ``None`` disables
        auth (single-user / local mode).
    :param feature_flags: Immutable deployment release-feature snapshot.
        When omitted, resolves ``OMNIGENT_FEATURES`` at router construction.
    :returns: The configured router (mounted under ``/v1``).
    """
    flags = feature_flags or resolve_feature_flags()
    router = APIRouter()

    @router.get("/usage", response_model=UsageReport)
    async def get_usage(request: Request) -> UsageReport:
        """
        Aggregate the calling user's LLM spend across their sessions.

        require_user, not get_user_id: the aggregation scopes to the caller,
        so a request slipping through as ``None`` in multi-user mode would
        read another scope. Fail closed with 401 instead (``user_id`` is
        ``None`` only when auth is disabled — the single-user / local case).
        """
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(
            _build_usage_report,
            conversation_store,
            user_id,
            include_page_details=flags.enabled(Feature.USAGE_PAGE),
        )

    @router.get("/usage/efficiency", response_model=EfficiencyReport)
    async def get_usage_efficiency(
        request: Request,
        days: int = Query(default=30, ge=1, le=366),
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> EfficiencyReport:
        """
        Score the calling user's recent sessions for token efficiency.

        Same caller scoping as ``GET /v1/usage`` (fail closed with 401 in
        multi-user mode), and like it, not gated by the ``usage_page`` flag:
        the scorecard is an API / CLI surface (``omni usage --efficiency``).

        :param days: Window in UTC calendar days including today.
        :param limit: Maximum number of top-cost session rows to return.
        """
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(
            _build_efficiency_report,
            conversation_store,
            user_id,
            days=days,
            limit=limit,
        )

    return router
