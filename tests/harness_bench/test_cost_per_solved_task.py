"""Unit tests for the cost_per_solved_task probe and its report rendering.

Network-free: fake drivers return canned per-task TurnResults, so every verdict
branch, the measurement arithmetic, answer checking, the snapshot-delta helper
and the report/JSON/rich rendering are asserted without a live gateway.
"""

from __future__ import annotations

import json

import pytest

from tests.harness_bench.bench import BenchMatrix, HarnessReport, _cell, run_harness
from tests.harness_bench.driver import (
    TurnResult,
    fill_snapshot_usage_delta,
    settled_usage_snapshot,
)
from tests.harness_bench.probes import ALL_PROBES, PROBES_BY_NAME
from tests.harness_bench.probes.cost_per_solved_task import (
    TASKS,
    CostPerSolvedTaskProbe,
    is_solved,
    normalize_answer,
    summarize,
)
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.report import render_json, render_markdown, render_table
from tests.harness_bench.verdict import Priority, ProbeResult, Verdict

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")
_ANSWERS = {task.prompt: task.expected for task in TASKS}


def _usage(input_tokens: int, cache_read: int = 0, output: int = 0) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": 0,
        "output_tokens": output,
    }


def _ok(prompt: str, **kwargs) -> TurnResult:
    return TurnResult(completed=True, text=_ANSWERS[prompt], **kwargs)


class _Driver:
    """Fake driver answering each task prompt through *answer*."""

    transport = "full-server"

    def __init__(self, answer) -> None:
        self._answer = answer
        self.prompts: list[str] = []

    async def run_task_turn(self, prompt: str) -> TurnResult:
        self.prompts.append(prompt)
        return self._answer(prompt)


async def _run(answer) -> ProbeResult:
    return await CostPerSolvedTaskProbe().run(_Driver(answer), _PROFILE)


# --- registration -----------------------------------------------------------


def test_registered_as_p1_after_basic_turn() -> None:
    probe = PROBES_BY_NAME["cost_per_solved_task"]
    assert probe.priority is Priority.P1
    assert [p.name for p in ALL_PROBES[:2]] == ["basic_turn", "cost_per_solved_task"]


def test_suite_is_small_and_answers_are_self_consistent() -> None:
    assert len(TASKS) == 3
    assert len({task.name for task in TASKS}) == 3
    assert all(is_solved(task.expected, task.expected) for task in TASKS)


# --- verdict branches --------------------------------------------------------


async def test_all_solved_and_priced_is_supported() -> None:
    driver = _Driver(lambda p: _ok(p, total_cost_usd=0.002, usage=_usage(80, 20, 10)))
    r = await CostPerSolvedTaskProbe().run(driver, _PROFILE)
    assert r.verdict is Verdict.SUPPORTED
    assert driver.prompts == [task.prompt for task in TASKS]
    assert r.measurement == {
        "solved": 3,
        "attempted": 3,
        "total_cost_usd": pytest.approx(0.006),
        "cost_per_solved_task_usd": pytest.approx(0.002),
        "tokens_per_solved_task": pytest.approx(110),
        "cache_hit_rate": pytest.approx(0.2),
    }
    assert "3/3 solved" in r.note
    assert [t["solved"] for t in r.detail["tasks"]] == [True, True, True]


async def test_failed_tasks_still_count_toward_spend() -> None:
    first = TASKS[0].prompt

    def answer(prompt: str) -> TurnResult:
        if prompt == first:
            return _ok(prompt, total_cost_usd=0.003, total_tokens=300)
        return TurnResult(completed=True, text="wrong", total_cost_usd=0.003, total_tokens=300)

    r = await _run(answer)
    assert r.verdict is Verdict.SUPPORTED
    assert r.measurement is not None
    assert r.measurement["solved"] == 1
    assert r.measurement["cost_per_solved_task_usd"] == pytest.approx(0.009)
    assert r.measurement["tokens_per_solved_task"] == pytest.approx(900)
    # No per-bucket counters were reported, so the cache rate is unmeasured.
    assert r.measurement["cache_hit_rate"] is None


async def test_solved_but_unpriced_is_partial() -> None:
    r = await _run(lambda p: _ok(p, total_tokens=500))
    assert r.verdict is Verdict.PARTIAL
    assert r.measurement is not None
    assert r.measurement["total_cost_usd"] is None
    assert r.measurement["cost_per_solved_task_usd"] is None
    assert r.measurement["tokens_per_solved_task"] == pytest.approx(500)
    assert "unpriced" in r.note


async def test_zero_cost_reads_as_unpriced() -> None:
    r = await _run(lambda p: _ok(p, total_cost_usd=0.0, total_tokens=500))
    assert r.verdict is Verdict.PARTIAL


async def test_nothing_solved_with_usage_is_partial() -> None:
    r = await _run(lambda p: TurnResult(completed=True, text="no idea", total_cost_usd=0.01))
    assert r.verdict is Verdict.PARTIAL
    assert r.measurement is not None
    assert r.measurement["solved"] == 0
    assert r.measurement["total_cost_usd"] == pytest.approx(0.03)
    assert r.measurement["cost_per_solved_task_usd"] is None
    assert "0/3" in r.note


async def test_model_failure_is_an_unsolved_attempt() -> None:
    r = await _run(
        lambda p: TurnResult(failed=True, error={"message": "model refused"}, total_tokens=50)
    )
    assert r.verdict is Verdict.PARTIAL
    assert r.measurement is not None
    assert (r.measurement["solved"], r.measurement["attempted"]) == (0, 3)


async def test_no_usage_is_skipped() -> None:
    r = await _run(lambda p: _ok(p))
    assert r.verdict is Verdict.SKIPPED
    assert "no usage" in r.note
    assert r.measurement is None


async def test_infra_failure_is_skipped_and_stops_the_suite() -> None:
    driver = _Driver(lambda p: TurnResult(failed=True, error={"message": "403 Forbidden"}))
    r = await CostPerSolvedTaskProbe().run(driver, _PROFILE)
    assert r.verdict is Verdict.SKIPPED
    assert "auth rejected" in r.note
    assert len(driver.prompts) == 1


async def test_timeout_is_skipped_and_stops_the_suite() -> None:
    first = TASKS[0].prompt

    def answer(prompt: str) -> TurnResult:
        if prompt == first:
            return _ok(prompt, total_cost_usd=0.01)
        return TurnResult(timed_out=True)

    driver = _Driver(answer)
    r = await CostPerSolvedTaskProbe().run(driver, _PROFILE)
    assert r.verdict is Verdict.SKIPPED
    assert "timed out" in r.note
    assert len(driver.prompts) == 2


async def test_driver_without_task_turns_is_skipped() -> None:
    class _Bare:
        transport = "custom"

    r = await CostPerSolvedTaskProbe().run(_Bare(), _PROFILE)
    assert r.verdict is Verdict.SKIPPED
    assert "custom" in r.note


# --- arithmetic --------------------------------------------------------------


def test_summarize_zero_denominators_are_none() -> None:
    task = TASKS[0]
    m = summarize([(task, TurnResult(completed=True), False)])
    assert m == {
        "solved": 0,
        "attempted": 1,
        "total_cost_usd": None,
        "cost_per_solved_task_usd": None,
        "tokens_per_solved_task": None,
        "cache_hit_rate": None,
    }


def test_summarize_cache_rate_uses_scorecard_formula() -> None:
    # cache_read / (input + cache_read + cache_creation), summed across tasks.
    a = TurnResult(completed=True, usage={**_usage(60, 30, 5), "cache_creation_input_tokens": 10})
    b = TurnResult(completed=True, usage=_usage(0, 100, 5))
    m = summarize([(TASKS[0], a, True), (TASKS[1], b, True)])
    assert m["cache_hit_rate"] == pytest.approx(130 / 200)
    assert m["tokens_per_solved_task"] == pytest.approx(210 / 2)


def test_summarize_mixed_priced_and_unpriced_tasks() -> None:
    priced = TurnResult(completed=True, total_cost_usd=0.004, total_tokens=10)
    unpriced = TurnResult(completed=True, total_tokens=10)
    m = summarize([(TASKS[0], priced, True), (TASKS[1], unpriced, True)])
    assert m["total_cost_usd"] == pytest.approx(0.004)
    assert m["cost_per_solved_task_usd"] == pytest.approx(0.002)


def test_summarize_prompt_without_cache_reads_has_zero_rate() -> None:
    m = summarize([(TASKS[0], TurnResult(completed=True, usage=_usage(100)), True)])
    assert m["cache_hit_rate"] == 0.0


# --- answer checking ----------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("391", "391"),
        ("  391.\n", "391"),
        ("**391**", "391"),
        ("`391`", "391"),
        ("Sure, here it is:\n\n391", "391"),
        ("SSENRAH", "ssenrah"),
        ('"ssenrah"', "ssenrah"),
        ("apple,fig , pear", "apple, fig, pear"),
        ("Apple, Fig, Pear.", "apple, fig, pear"),
        ("apple,   fig,\tpear", "apple, fig, pear"),
    ],
)
def test_is_solved_tolerates_formatting(reply: str, expected: str) -> None:
    assert is_solved(reply, expected)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("", "391"),
        ("392", "391"),
        ("391 or 392", "391"),
        ("The answer is 391 for sure", "391"),
        ("391\nActually, 392", "391"),
        ("pear, apple, fig", "apple, fig, pear"),
        ("harness", "ssenrah"),
    ],
)
def test_is_solved_rejects_wrong_answers(reply: str, expected: str) -> None:
    assert not is_solved(reply, expected)


def test_normalize_answer() -> None:
    assert normalize_answer("  *`Apple ,Fig`*!  ") == "apple, fig"


# --- snapshot usage delta ------------------------------------------------------


def _snapshot(cost: float | None, input_tokens: int, cache_read: int, last: int) -> dict:
    return {
        "total_cost_usd": cost,
        "last_total_tokens": last,
        "usage_by_model": {
            "m": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": None,
                "output_tokens": 10,
                "total_tokens": None,
                "total_cost_usd": cost,
            }
        },
    }


def test_snapshot_delta_attributes_only_this_turn() -> None:
    r = TurnResult(completed=True)
    fill_snapshot_usage_delta(r, _snapshot(0.01, 100, 0, 110), _snapshot(0.015, 130, 50, 90))
    assert r.total_cost_usd == pytest.approx(0.005)
    assert r.usage == {
        "input_tokens": 30,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
    }
    assert r.total_tokens == 80


def test_snapshot_delta_unpriced_and_without_buckets() -> None:
    r = TurnResult(completed=True)
    fill_snapshot_usage_delta(r, {}, {"total_cost_usd": None, "last_total_tokens": 42})
    assert r.total_cost_usd is None
    assert r.usage is None
    # Falls back to the snapshot's per-task total.
    assert r.total_tokens == 42


def test_snapshot_delta_first_priced_turn() -> None:
    r = TurnResult(completed=True)
    fill_snapshot_usage_delta(r, {"total_cost_usd": None}, _snapshot(0.02, 10, 0, 20))
    assert r.total_cost_usd == pytest.approx(0.02)


def test_settled_usage_snapshot_waits_for_usage_to_move() -> None:
    before = _snapshot(0.01, 100, 0, 110)
    after = _snapshot(0.02, 150, 0, 60)
    reads = iter([before, before, after])
    assert settled_usage_snapshot(lambda: next(reads), before, poll_interval=0) is after


def test_settled_usage_snapshot_gives_up() -> None:
    before = _snapshot(0.01, 100, 0, 110)
    got = settled_usage_snapshot(lambda: before, before, poll_interval=0, settle_s=0)
    assert got is before


# --- report rendering ------------------------------------------------------------

_MEASUREMENT = {
    "solved": 2,
    "attempted": 3,
    "total_cost_usd": 0.009,
    "cost_per_solved_task_usd": 0.0045,
    "tokens_per_solved_task": 412.5,
    "cache_hit_rate": None,
}


def _matrix() -> BenchMatrix:
    basic = PROBES_BY_NAME["basic_turn"]
    cost = PROBES_BY_NAME["cost_per_solved_task"]
    cells = [
        _cell(basic, _PROFILE, ProbeResult(Verdict.SUPPORTED, note="ok")),
        _cell(
            cost,
            _PROFILE,
            ProbeResult(Verdict.SUPPORTED, note="2/3 solved", measurement=_MEASUREMENT),
        ),
    ]
    return BenchMatrix(reports=[HarnessReport(_PROFILE, cells, transport="full-server")])


def test_text_and_markdown_render_measurements() -> None:
    expected = (
        "- fake / Cost/solved task: solved=2, attempted=3, total_cost_usd=0.009, "
        "cost_per_solved_task_usd=0.0045, tokens_per_solved_task=412.5, cache_hit_rate=—"
    )
    table = render_table(_matrix())
    assert "Measurements:" in table
    assert expected in table
    markdown = render_markdown(_matrix())
    assert "## Measurements" in markdown
    assert expected in markdown


def test_json_carries_measurement_only_on_measuring_cells() -> None:
    cells = json.loads(render_json(_matrix()))["harnesses"][0]["cells"]
    basic, cost = cells
    assert "measurement" not in basic
    assert cost["measurement"] == _MEASUREMENT


def test_reports_without_measurements_have_no_section() -> None:
    basic = PROBES_BY_NAME["basic_turn"]
    matrix = BenchMatrix(
        reports=[HarnessReport(_PROFILE, [_cell(basic, _PROFILE, ProbeResult(Verdict.SUPPORTED))])]
    )
    assert "Measurements" not in render_table(matrix)
    assert "Measurements" not in render_markdown(matrix)


def test_headline_formats_cost_or_solved_count() -> None:
    probe = CostPerSolvedTaskProbe()
    assert probe.headline(_MEASUREMENT) == "$0.0045"
    assert probe.headline({**_MEASUREMENT, "cost_per_solved_task_usd": None}) == "2/3 solved"


def test_rich_cell_shows_headline() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from tests.harness_bench.events import HarnessStarted, ProbeFinished
    from tests.harness_bench.richreport import _RichLiveSink

    probes = [PROBES_BY_NAME["basic_turn"], PROBES_BY_NAME["cost_per_solved_task"]]
    console = Console(record=True, width=120, force_terminal=False)
    sink = _RichLiveSink(console, probes=probes)
    try:
        sink.emit(HarnessStarted("fake", "full-server", "m"))
        sink.emit(
            ProbeFinished(
                "fake",
                "cost_per_solved_task",
                "Cost/solved task",
                Verdict.SUPPORTED,
                measurement=_MEASUREMENT,
            )
        )
        assert "$0.0045" in sink._rows["fake"]["Cost/solved task"]
    finally:
        sink.close()


# --- orchestration / offline -------------------------------------------------------


async def test_offline_run_skips_without_failing() -> None:
    probe = PROBES_BY_NAME["cost_per_solved_task"]
    report = await run_harness(_PROFILE, probes=[probe], live=False)
    (cell,) = report.cells
    assert cell.observed is Verdict.SKIPPED
    assert cell.verdict is Verdict.SKIPPED
    assert cell.measurement is None
    assert not report.has_drift


async def test_live_run_threads_measurement_into_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    class _LiveDriver(_Driver):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(lambda p: _ok(p, total_cost_usd=0.001, total_tokens=20))

        @staticmethod
        def unavailable(*args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def run_basic_turn(self, marker: str) -> TurnResult:
            return TurnResult(completed=True, text=marker)

    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _LiveDriver,
    )
    probes = [PROBES_BY_NAME["basic_turn"], PROBES_BY_NAME["cost_per_solved_task"]]
    report = await run_harness(_PROFILE, probes=probes, databricks_profile="x", live=True)
    cost = report.cells[1]
    assert cost.verdict is Verdict.SUPPORTED
    assert cost.measurement is not None
    assert cost.measurement["cost_per_solved_task_usd"] == pytest.approx(0.001)
    payload = json.loads(render_json(BenchMatrix(reports=[report])))
    assert payload["harnesses"][0]["cells"][1]["measurement"]["solved"] == 3
