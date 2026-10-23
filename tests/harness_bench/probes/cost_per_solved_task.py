"""Cost-per-solved-task probe: what does a harness spend to get a right answer?

``cost_tracking`` asks whether a turn's spend is visible at all. This probe
uses that visibility to make efficiency part of the conformance matrix: it runs
a small fixed suite of tasks through the driver, checks each final reply, and
divides the suite's total spend by the number of tasks solved. Failed tasks
still count toward spend, so a harness that is cheap per turn but often wrong
does not look cheap per solved task.

The tasks need no filesystem or tools, so every transport can run them, and
their answers are checked from the final text alone (see :func:`is_solved`).
Rates reuse the efficiency scorecard's formulas
(:class:`omnigent.server.usage_efficiency.TokenCounters`), so the probe's cache
hit rate means the same thing as ``omnigent usage --efficiency``.

The probe runs immediately after Basic turn so every transport measures it on a
session holding only that one short exchange; later probes would add long
essays and tool turns to the context and make the number depend on which
dimensions were selected.

Verdicts:
- **SUPPORTED**: at least one task solved and the spend was priced; the
  measurement carries the USD cost per solved task.
- **PARTIAL**: tasks were solved but the model is unpriced (tokens only), or
  usage was observed but no task was solved. The vocabulary has no
  "measured, nothing passed" verdict: UNSUPPORTED would claim the harness
  lacks a capability, which a wrong arithmetic answer does not show. PARTIAL
  is what ``cost_tracking`` uses for a degraded but real observation.
- **SKIPPED**: the transport cannot run arbitrary prompts, a turn hit an
  infrastructure failure or timed out, or no usage was surfaced at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from omnigent.server.usage_efficiency import TokenCounters, counters_from_usage
from tests.harness_bench.driver import TurnResult, infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


@dataclass(frozen=True)
class BenchTask:
    """One task in the fixed suite.

    :param name: Stable identifier reported per task, e.g. ``"arithmetic"``.
    :param prompt: The user message sent to the harness.
    :param expected: The answer a solved reply must normalize to.
    """

    name: str
    prompt: str
    expected: str


TASKS: tuple[BenchTask, ...] = (
    BenchTask(
        name="arithmetic",
        prompt="What is 17 * 23? Reply with only the number and nothing else.",
        expected="391",
    ),
    BenchTask(
        name="reverse",
        prompt=(
            "Reverse the letters of the word 'harness'. "
            "Reply with only the reversed word and nothing else."
        ),
        expected="ssenrah",
    ),
    BenchTask(
        name="sort",
        prompt=(
            "Sort these words alphabetically: pear, apple, fig. Reply with only the "
            "sorted words separated by commas and nothing else."
        ),
        expected="apple, fig, pear",
    ),
)

# Markdown emphasis, code ticks and quotes a model may wrap a bare answer in.
_WRAPPERS = "`*_\"'"


def normalize_answer(text: str) -> str:
    """Normalize a reply for comparison.

    Case-insensitive; trims surrounding whitespace, markdown/quote wrappers and
    trailing ``.`` / ``!``; collapses internal whitespace; and spaces commas
    as ``", "`` so ``"apple,fig , pear"`` matches ``"apple, fig, pear"``.
    """
    normalized = text.strip().lower()
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = normalized.strip().strip(_WRAPPERS).rstrip(".!")
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    return re.sub(r"\s+", " ", normalized)


def is_solved(text: str, expected: str) -> bool:
    """Whether *text* answers with *expected*.

    The whole reply or its last non-empty line must normalize to the expected
    answer, so a reply such as ``"Sure.\\n391"`` passes but ``"391 or 392"``
    and an answer buried mid-sentence do not.
    """
    target = normalize_answer(expected)
    lines = [line for line in text.splitlines() if line.strip()]
    candidates = [text, lines[-1]] if lines else [text]
    return any(normalize_answer(candidate) == target for candidate in candidates)


def _task_counters(result: TurnResult) -> TokenCounters:
    return counters_from_usage(result.usage) if result.usage else TokenCounters()


def _task_tokens(result: TurnResult) -> int:
    """Tokens one task used: per-bucket counters when present, else the turn total."""
    counters = _task_counters(result)
    tokens = counters.prompt_tokens + counters.output_tokens
    if tokens:
        return tokens
    return result.total_tokens if result.total_tokens and result.total_tokens > 0 else 0


def _task_cost(result: TurnResult) -> float | None:
    """Priced USD cost of one task; ``None`` when unpriced (a zero reads as unpriced)."""
    if result.total_cost_usd is not None and result.total_cost_usd > 0:
        return result.total_cost_usd
    return None


def summarize(
    outcomes: list[tuple[BenchTask, TurnResult, bool]],
) -> dict[str, Any]:
    """Compute the probe measurement from per-task outcomes.

    ``cost_per_solved_task_usd`` divides the spend of *every* attempted task by
    the number solved. Rates are ``None`` when their denominator is zero, as in
    the efficiency scorecard, so "not measured" is distinct from ``0``.
    """
    solved = sum(1 for _, _, ok in outcomes if ok)
    costs = [cost for _, result, _ in outcomes if (cost := _task_cost(result)) is not None]
    total_cost = sum(costs) if costs else None
    total_tokens = sum(_task_tokens(result) for _, result, _ in outcomes)
    counters = sum((_task_counters(result) for _, result, _ in outcomes), TokenCounters())
    return {
        "solved": solved,
        "attempted": len(outcomes),
        "total_cost_usd": total_cost,
        "cost_per_solved_task_usd": (
            total_cost / solved if total_cost is not None and solved else None
        ),
        "tokens_per_solved_task": (total_tokens / solved if total_tokens and solved else None),
        "cache_hit_rate": counters.cache_hit_rate,
    }


class CostPerSolvedTaskProbe(CapabilityProbe):
    name = "cost_per_solved_task"
    title = "Cost/solved task"
    # Report-only: efficiency is measured, never declared, so it cannot drift.
    priority = Priority.P1
    applies_to = Applicability.BOTH

    def headline(self, measurement: dict[str, Any]) -> str:
        cost = measurement.get("cost_per_solved_task_usd")
        if isinstance(cost, (int, float)):
            return f"${cost:.4f}"
        return f"{measurement.get('solved', 0)}/{measurement.get('attempted', 0)} solved"

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        run_task = getattr(driver, "run_task_turn", None)
        if run_task is None:
            return ProbeResult.skipped(
                f"the {getattr(driver, 'transport', 'selected')} driver cannot run task prompts"
            )

        outcomes: list[tuple[BenchTask, TurnResult, bool]] = []
        for task in TASKS:
            result: TurnResult = await run_task(task.prompt)
            infra = infra_failure_reason(result)
            if infra is not None:
                return ProbeResult(Verdict.SKIPPED, note=f"task {task.name!r}: {infra}")
            if result.timed_out:
                # A busy session would fail every later task, so stop here.
                return ProbeResult(Verdict.SKIPPED, note=f"task {task.name!r} timed out")
            # A model-level failure is an unsolved attempt, not a skip.
            solved = result.completed and is_solved(result.text, task.expected)
            outcomes.append((task, result, solved))

        measurement = summarize(outcomes)
        detail = {
            "tasks": [
                {
                    "name": task.name,
                    "completed": result.completed,
                    "solved": solved,
                    "cost_usd": _task_cost(result),
                    "tokens": _task_tokens(result),
                    "reply": result.text[:200],
                }
                for task, result, solved in outcomes
            ]
        }
        solved, attempted = measurement["solved"], measurement["attempted"]
        total_cost = measurement["total_cost_usd"]
        tokens_observed = any(entry["tokens"] for entry in detail["tasks"])

        if total_cost is None and not tokens_observed:
            return ProbeResult(
                Verdict.SKIPPED,
                note=(
                    f"{solved}/{attempted} solved, but the transport surfaced no "
                    "usage/cost to divide"
                ),
                detail=detail,
            )
        if solved == 0:
            return ProbeResult(
                Verdict.PARTIAL,
                note=f"0/{attempted} tasks solved; cost per solved task is undefined",
                detail=detail,
                measurement=measurement,
            )
        if total_cost is None:
            return ProbeResult(
                Verdict.PARTIAL,
                note=(
                    f"{solved}/{attempted} solved, "
                    f"{measurement['tokens_per_solved_task']:.0f} tokens per solved task, "
                    "but no priced cost (unpriced model)"
                ),
                detail=detail,
                measurement=measurement,
            )
        return ProbeResult(
            Verdict.SUPPORTED,
            note=(
                f"{solved}/{attempted} solved, "
                f"${measurement['cost_per_solved_task_usd']:.6f} per solved task"
            ),
            detail=detail,
            measurement=measurement,
        )
