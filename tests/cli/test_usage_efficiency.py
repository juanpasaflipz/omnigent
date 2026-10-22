"""Tests for ``omnigent usage --efficiency`` and its renderer."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import _efficiency_outlier_ids, _render_usage_efficiency, cli

_REPORT: dict[str, Any] = {
    "object": "usage_efficiency_report",
    "window_days": 30,
    "totals": {
        "cost_usd": 142.1,
        "prompt_tokens": 1_000_000,
        "output_tokens": 90_000,
        "cache_hit_rate": 0.71,
        "output_share": 0.09,
        "sessions": 61,
        "cost_per_session_usd": 2.33,
    },
    "by_harness": {
        "claude-native": {
            "cost_usd": 120.0,
            "prompt_tokens": 900_000,
            "output_tokens": 80_000,
            "cache_hit_rate": 0.75,
            "output_share": 0.08,
            "sessions": 50,
            "cost_per_session_usd": 2.4,
        },
        "codex-native": {
            "cost_usd": 22.1,
            "prompt_tokens": 100_000,
            "output_tokens": 10_000,
            "cache_hit_rate": None,
            "output_share": 0.1,
            "sessions": 11,
            "cost_per_session_usd": 2.01,
        },
    },
    "by_model": {
        "claude-opus-5-5": {
            "cost_usd": 116.5,
            "prompt_tokens": 800_000,
            "output_tokens": 70_000,
            "cache_hit_rate": 0.74,
            "output_share": 0.08,
            "cost_share": 0.82,
        },
        "gpt-5-codex": {
            "cost_usd": 25.6,
            "prompt_tokens": 200_000,
            "output_tokens": 20_000,
            "cache_hit_rate": 0.6,
            "output_share": 0.09,
            "cost_share": 0.18,
        },
    },
    "sessions": [
        {
            "id": "conv_churn",
            "title": "rewrite everything",
            "harness": "claude-native",
            "model_ids": ["claude-opus-5-5"],
            "cost_usd": 9.1,
            # 0.4 is 35 points below the claude-native average of 0.75 -> flagged.
            "cache_hit_rate": 0.4,
            "output_share": 0.3,
            "prompt_tokens": 50_000,
            "output_tokens": 20_000,
            "created_at": 1_700_000_000,
        },
        {
            "id": "conv_fine",
            "title": None,
            "harness": "claude-native",
            "model_ids": ["claude-opus-5-5"],
            "cost_usd": 4.0,
            "cache_hit_rate": 0.7,
            "output_share": 0.05,
            "prompt_tokens": 40_000,
            "output_tokens": 2_000,
            "created_at": 1_700_000_000,
        },
        {
            "id": "conv_codex",
            "title": "no rate",
            "harness": "codex-native",
            "model_ids": ["gpt-5-codex"],
            "cost_usd": 1.0,
            # Harness average unknown -> never flagged.
            "cache_hit_rate": 0.1,
            "output_share": 0.1,
            "prompt_tokens": 1_000,
            "output_tokens": 100,
            "created_at": 1_700_000_000,
        },
    ],
}


def test_efficiency_outlier_ids_flags_sessions_far_below_harness_average() -> None:
    assert _efficiency_outlier_ids(_REPORT) == {"conv_churn"}


def test_efficiency_outlier_ids_uses_unknown_bucket_for_unresolved_harness() -> None:
    report = {
        "by_harness": {"unknown": {"cache_hit_rate": 0.9}},
        "sessions": [
            {"id": "a", "harness": None, "cache_hit_rate": 0.5},
            {"id": "b", "harness": None, "cache_hit_rate": 0.7},
            {"id": "c", "harness": None, "cache_hit_rate": None},
        ],
    }
    assert _efficiency_outlier_ids(report) == {"a"}
    assert _efficiency_outlier_ids({}) == set()


def test_render_usage_efficiency_tables(capsys: pytest.CaptureFixture[str]) -> None:
    _render_usage_efficiency(_REPORT)
    out = capsys.readouterr().out
    assert "Totals (last 30 days)" in out
    assert "$142.10" in out and "61" in out and "71%" in out and "9%" in out and "$2.33" in out
    assert "By harness" in out and "claude-native" in out and "codex-native" in out
    assert "By model" in out and "claude-opus-5-5" in out and "82%" in out and "18%" in out
    assert "Top sessions by cost" in out and "(top 3)" in out
    # The title column is the flexible one and may wrap, so match a single word.
    assert "conv_churn" in out and "rewrite" in out
    assert "conv_fine" in out and "conv_codex" in out
    # Unknown rates render as a dash rather than 0%.
    assert "—" in out
    # The outlier legend only prints when something was flagged.
    assert "prompt churn" in out


def test_render_usage_efficiency_marks_outlier_row_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_usage_efficiency(_REPORT)
    lines = capsys.readouterr().out.splitlines()
    churn = next(line for line in lines if "conv_churn" in line)
    fine = next(line for line in lines if "conv_fine" in line)
    assert churn.lstrip().startswith("!")
    assert not fine.lstrip().startswith("!")


def test_render_usage_efficiency_empty(capsys: pytest.CaptureFixture[str]) -> None:
    _render_usage_efficiency({"window_days": 7, "totals": {"sessions": 0}, "sessions": []})
    out = capsys.readouterr().out
    assert "Totals (last 7 days)" in out
    assert "No usage recorded in this window." in out
    assert "By harness" not in out


def test_usage_command_help_lists_efficiency_options() -> None:
    result = CliRunner().invoke(cli, ["usage", "--help"])
    assert result.exit_code == 0
    assert "--efficiency" in result.output
    assert "--days" in result.output


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return _REPORT


class _FakeClient:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        _FakeClient.calls.append((path, params))
        return _FakeResponse()


@pytest.fixture()
def _stub_efficiency_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda *a, **k: "http://x")
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **k: {})
    monkeypatch.setattr(httpx, "Client", _FakeClient)


@pytest.mark.usefixtures("_stub_efficiency_fetch")
def test_usage_efficiency_command_calls_endpoint_and_renders() -> None:
    result = CliRunner().invoke(
        cli, ["usage", "--efficiency", "--days", "7", "--limit", "20", "--server", "http://x"]
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.calls == [("/v1/usage/efficiency", {"days": 7, "limit": 20})]
    assert "Totals (last 30 days)" in result.output
    assert "conv_churn" in result.output


@pytest.mark.usefixtures("_stub_efficiency_fetch")
def test_usage_efficiency_command_json() -> None:
    result = CliRunner().invoke(cli, ["usage", "--efficiency", "--json", "--server", "http://x"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["window_days"] == 30
    assert payload["by_model"]["claude-opus-5-5"]["cost_share"] == 0.82


@pytest.mark.usefixtures("_stub_efficiency_fetch")
def test_usage_efficiency_rejects_zero_days() -> None:
    result = CliRunner().invoke(
        cli, ["usage", "--efficiency", "--days", "0", "--server", "http://x"]
    )
    assert result.exit_code != 0
    assert "--days" in result.output
