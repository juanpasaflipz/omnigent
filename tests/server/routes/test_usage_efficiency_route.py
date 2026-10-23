"""Tests for ``GET /v1/usage/efficiency`` and its report builder."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.routes.usage import (
    _build_efficiency_report,
    _window_start_epoch,
    create_usage_router,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_DAY = 86_400
# 2026-07-22T00:00:00Z; the builder reads "today" from omnigent.db.utils.now_epoch.
_TODAY_EPOCH = 1_784_678_400
# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def test_window_start_epoch_inclusive_days() -> None:
    # A 1-day window starts at today's midnight; 30 days reaches back 29 days.
    assert _window_start_epoch("2026-07-22", days=1) == _TODAY_EPOCH
    assert _window_start_epoch("2026-07-22", days=30) == _TODAY_EPOCH - 29 * _DAY


def _add_session(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ts: int,
    usage: dict[str, Any],
    title: str,
) -> str:
    # create_conversation stamps updated_at from now_epoch; pin it so the
    # session lands at a specific time. set_session_usage does not touch it.
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: ts,
    )
    conv = store.create_conversation(title=title, agent_id=_AGENT_ID)
    store.set_session_usage(conv.id, usage)
    return conv.id


def _usage(
    *, cost: float, model: str, input_tokens: int, cache_read: int, output: int
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
        "total_cost_usd": cost,
        "by_model": {
            model: {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
                "total_cost_usd": cost,
            }
        },
    }


@pytest.fixture()
def seeded_store(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[SqlAlchemyConversationStore, dict[str, str]]:
    """Three sessions: two recent (different harnesses), one outside a 7-day window."""
    store = SqlAlchemyConversationStore(db_uri)
    ids = {
        "dear": _add_session(
            store,
            monkeypatch,
            ts=_TODAY_EPOCH + 3600,
            usage=_usage(cost=9.0, model="opus", input_tokens=100, cache_read=900, output=100),
            title="dear",
        ),
        "cheap": _add_session(
            store,
            monkeypatch,
            ts=_TODAY_EPOCH - 2 * _DAY,
            usage=_usage(cost=1.0, model="haiku", input_tokens=100, cache_read=0, output=100),
            title="cheap",
        ),
        "old": _add_session(
            store,
            monkeypatch,
            ts=_TODAY_EPOCH - 20 * _DAY,
            usage=_usage(cost=50.0, model="opus", input_tokens=100, cache_read=100, output=10),
            title="old",
        ),
    }
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: _TODAY_EPOCH)
    harness_by_title = {"dear": "claude-native", "cheap": "codex-native", "old": "claude-native"}
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda conv: harness_by_title[conv.title],
    )
    return store, ids


def test_build_efficiency_report_well_formed(
    seeded_store: tuple[SqlAlchemyConversationStore, dict[str, str]],
) -> None:
    store, ids = seeded_store
    report = _build_efficiency_report(store, None, days=30, limit=50)

    assert report.window_days == 30
    assert report.totals.sessions == 3
    assert report.totals.cost_usd == pytest.approx(60.0)
    assert report.totals.cost_per_session_usd == pytest.approx(20.0)
    # prompt = 300 + 1000 = 1300 (input 300, cache 1000); output 210.
    assert report.totals.prompt_tokens == 1300
    assert report.totals.output_tokens == 210
    assert report.totals.cache_hit_rate == pytest.approx(1000 / 1300)
    assert report.totals.output_share == pytest.approx(210 / 1510)

    # Sorted by cost descending, worst offenders first.
    assert [row.id for row in report.sessions] == [ids["old"], ids["dear"], ids["cheap"]]
    dear = report.sessions[1]
    assert dear.harness == "claude-native"
    assert dear.model_ids == ["opus"]
    assert dear.title == "dear"
    assert dear.cache_hit_rate == pytest.approx(0.9)
    assert dear.output_share == pytest.approx(100 / 1100)

    assert report.by_harness["claude-native"].sessions == 2
    assert report.by_harness["codex-native"].cache_hit_rate == pytest.approx(0.0)
    assert set(report.by_model) == {"opus", "haiku"}
    assert report.by_model["opus"].cost_share == pytest.approx(59 / 60)
    assert sum(m.cost_share or 0.0 for m in report.by_model.values()) == pytest.approx(1.0)


def test_build_efficiency_report_honors_days_and_limit(
    seeded_store: tuple[SqlAlchemyConversationStore, dict[str, str]],
) -> None:
    store, ids = seeded_store
    report = _build_efficiency_report(store, None, days=7, limit=1)

    # The 20-day-old session is outside the window: totals and groupings
    # exclude it, and limit truncates only the session rows.
    assert report.window_days == 7
    assert report.totals.sessions == 2
    assert report.totals.cost_usd == pytest.approx(10.0)
    assert report.by_harness["claude-native"].sessions == 1
    assert [row.id for row in report.sessions] == [ids["dear"]]


def test_build_efficiency_report_empty(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: _TODAY_EPOCH)
    report = _build_efficiency_report(store, None, days=30, limit=50)
    assert report.sessions == []
    assert report.totals.sessions == 0
    assert report.totals.cache_hit_rate is None
    assert report.by_harness == {}


def test_usage_efficiency_endpoint(
    seeded_store: tuple[SqlAlchemyConversationStore, dict[str, str]],
) -> None:
    store, ids = seeded_store
    app = FastAPI()
    app.include_router(create_usage_router(store), prefix="/v1")

    with TestClient(app) as client:
        response = client.get("/v1/usage/efficiency", params={"days": 7, "limit": 1})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["object"] == "usage_efficiency_report"
        assert body["window_days"] == 7
        assert body["totals"]["sessions"] == 2
        assert [row["id"] for row in body["sessions"]] == [ids["dear"]]
        assert set(body["by_harness"]) == {"claude-native", "codex-native"}
        assert "cost_share" in body["by_model"]["opus"]

        # Defaults: 30-day window, up to 50 rows.
        default = client.get("/v1/usage/efficiency")
        assert default.status_code == 200
        assert default.json()["window_days"] == 30
        assert default.json()["totals"]["sessions"] == 3

        # Out-of-range query values are rejected, not clamped.
        assert client.get("/v1/usage/efficiency", params={"days": 0}).status_code == 422
        assert client.get("/v1/usage/efficiency", params={"limit": 0}).status_code == 422
