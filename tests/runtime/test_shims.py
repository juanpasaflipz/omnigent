"""Tests for the shim registry: registration, firing records, and snapshots."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from omnigent.runtime import shims
from omnigent.runtime.shims import (
    Shim,
    firings_path,
    first_seen_path,
    register_shim,
    reset_for_tests,
    shim_fired,
    snapshot,
)

_DAY_SECONDS = 86400.0


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Give every test an empty registry and a private firing log.

    :param tmp_path: Pytest temporary directory for the data dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Iterator yielding once, with the registry cleared on both sides.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    production_shims = shims.registered_shims()
    reset_for_tests()
    yield
    reset_for_tests()
    # Production modules register on import only once, so put theirs back.
    for shim in production_shims:
        register_shim(**asdict(shim))


def _register(name: str = "test.example", **overrides: str) -> Shim:
    """
    Register a shim with sensible defaults.

    :param name: Shim name.
    :param overrides: Field overrides for the registration.
    :returns: The registered shim.
    """
    fields = {
        "reason": "Example reason.",
        "model_gap": "Example gap.",
        "added_in": "0.19.0",
        "review_by": "0.24.0",
        "owner": "tests",
    }
    fields.update(overrides)
    return register_shim(name, **fields)


def _status(name: str, **kwargs: object) -> shims.ShimStatus:
    """
    Return the snapshot entry for one shim.

    :param name: The shim name to look up.
    :param kwargs: Keyword arguments forwarded to :func:`snapshot`.
    :returns: The matching status.
    """
    by_name = {status.name: status for status in snapshot(**kwargs)}  # type: ignore[arg-type]
    return by_name[name]


def test_register_shim_is_idempotent_for_identical_fields() -> None:
    """Re-registering the same definition returns the original object."""
    first = _register()
    second = _register()
    assert second is first
    assert [shim.name for shim in shims.registered_shims()] == ["test.example"]


def test_register_shim_rejects_conflicting_fields() -> None:
    """The same name with a different definition is a programming error."""
    _register()
    with pytest.raises(ValueError, match="already registered with different fields"):
        _register(reason="A different reason.")


def test_shim_fired_counts_total_and_per_model() -> None:
    """Firings accumulate overall and per model, with unknown models bucketed."""
    shim = _register()
    shim_fired(shim, model="claude-haiku-4-5", harness="claude-sdk")
    shim_fired(shim, model="claude-haiku-4-5")
    shim_fired(shim, model="gpt-4o")
    shim_fired(shim)

    status = _status("test.example")
    assert status.fire_count == 4
    assert status.by_model == {"claude-haiku-4-5": 2, "gpt-4o": 1, "unknown": 1}
    assert status.last_fired_at is not None
    assert status.retirable is False


def test_shim_fired_emits_one_info_log_line(caplog: pytest.LogCaptureFixture) -> None:
    """Each firing is visible as a structured INFO record naming the shim."""
    shim = _register()
    with caplog.at_level(logging.INFO, logger="omnigent.runtime.shims"):
        shim_fired(shim, model="gpt-4o", harness="openai-agents")
    records = [record for record in caplog.records if "shim fired" in record.getMessage()]
    assert len(records) == 1
    assert records[0].shim == "test.example"  # type: ignore[attr-defined]
    assert records[0].model == "gpt-4o"  # type: ignore[attr-defined]
    assert records[0].harness == "openai-agents"  # type: ignore[attr-defined]


def test_shim_fired_emits_otel_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``omnigent.shim.fired`` counter carries shim, model and harness attributes."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "1")
    added: list[tuple[int, dict[str, str]]] = []
    created: list[str] = []

    class _Counter:
        def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
            added.append((amount, dict(attributes or {})))

    class _Meter:
        def create_counter(self, name: str, unit: str = "", description: str = "") -> _Counter:
            created.append(name)
            return _Counter()

    # The fixture cleared the cached instrument, so the next firing builds it here.
    monkeypatch.setattr(shims.otel_metrics, "get_meter", lambda name: _Meter())
    shim = _register()
    shim_fired(shim, model="gpt-4o", harness="openai-agents")
    assert created == ["omnigent.shim.fired"]
    assert added == [(1, {"shim": "test.example", "model": "gpt-4o", "harness": "openai-agents"})]


def test_shim_fired_appends_jsonl_line() -> None:
    """Every firing lands in ``<data dir>/shims/firings.jsonl`` with its fields."""
    shim = _register()
    shim_fired(shim, model="gpt-4o", harness="openai-agents")

    lines = firings_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["shim"] == "test.example"
    assert entry["model"] == "gpt-4o"
    assert entry["harness"] == "openai-agents"
    assert isinstance(entry["ts"], float)


def test_snapshot_merges_firings_from_other_processes() -> None:
    """A line another process appended counts once, next to this process's own."""
    shim = _register()
    shim_fired(shim, model="gpt-4o")
    other_process_line = {"ts": 1_700_000_000.0, "shim": "test.example", "model": "claude-x"}
    with firings_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(other_process_line) + "\n")
        handle.write("not json\n")

    status = _status("test.example")
    assert status.fire_count == 2
    assert status.by_model == {"claude-x": 1, "gpt-4o": 1}


def test_snapshot_ignores_log_lines_for_unregistered_shims() -> None:
    """Firings of a shim that was since deleted do not resurrect it."""
    _register()
    firings_path().parent.mkdir(parents=True)
    firings_path().write_text(
        json.dumps({"ts": 1_700_000_000.0, "shim": "deleted.shim"}) + "\n", encoding="utf-8"
    )
    assert [status.name for status in snapshot()] == ["test.example"]


def test_snapshot_retirable_uses_idle_days_against_injected_now() -> None:
    """Observed for the window and quiet in it means retirable; a firing proves observation."""
    never = _register("test.never")
    recent = _register("test.recent")
    stale = _register("test.stale")
    now = 1_800_000_000.0
    firings_path().parent.mkdir(parents=True)
    firings_path().write_text(
        json.dumps({"ts": now - 5 * _DAY_SECONDS, "shim": "test.recent"})
        + "\n"
        + json.dumps({"ts": now - 45 * _DAY_SECONDS, "shim": "test.stale"})
        + "\n",
        encoding="utf-8",
    )

    # First seen just now: a never-fired shim is not retirable yet.
    assert _status(never.name, idle_days=30, now=now).retirable is False
    assert _status(never.name, idle_days=30, now=now).last_fired_at is None
    assert _status(recent.name, idle_days=30, now=now).retirable is False
    assert _status(stale.name, idle_days=30, now=now).retirable is True
    # Widening the window keeps the stale shim alive.
    assert _status(stale.name, idle_days=60, now=now).retirable is False


def test_snapshot_last_fired_is_latest_across_memory_and_log() -> None:
    """The later of the in-memory and logged firing wins."""
    shim = _register()
    shim_fired(shim)
    far_future = 4_000_000_000.0
    with firings_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": far_future, "shim": shim.name}) + "\n")
    status = _status(shim.name, now=far_future)
    assert status.last_fired_at == "2096-10-02T07:06:40+00:00"


def test_shim_fired_never_raises_when_log_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable data dir degrades to in-memory counting only."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(blocker))
    shim = _register()

    shim_fired(shim, model="gpt-4o")

    status = _status(shim.name)
    assert status.fire_count == 1
    assert status.by_model == {"gpt-4o": 1}
    assert not (blocker / "shims").exists()


def test_reset_for_tests_clears_registry_and_counters() -> None:
    """After a reset the registry is empty and a re-registration starts from zero."""
    shim = _register()
    shim_fired(shim)
    reset_for_tests()
    assert shims.registered_shims() == []
    firings_path().unlink()
    _register()
    assert _status("test.example").fire_count == 0


def test_fresh_deployment_reports_nothing_retirable_until_the_window_passes() -> None:
    """A never-fired shim becomes retirable only after a full window of observation."""
    shim = _register("test.fresh")
    start = 1_800_000_000.0

    first = _status(shim.name, idle_days=30, now=start)
    assert first.retirable is False
    assert first.first_seen_at == "2027-01-15T08:00:00+00:00"
    assert _status(shim.name, idle_days=30, now=start + 29 * _DAY_SECONDS).retirable is False
    # The first-seen time is persisted, not reset by later snapshots.
    assert _status(shim.name, idle_days=30, now=start + 30 * _DAY_SECONDS).retirable is True
    assert json.loads(first_seen_path().read_text(encoding="utf-8")) == {shim.name: start}


def test_unwritable_first_seen_record_never_makes_a_shim_retirable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the record cannot be read or written, every shim counts as just seen."""
    shim = _register("test.unwritable")
    first_seen_path().parent.mkdir(parents=True, exist_ok=True)
    first_seen_path().mkdir()  # a directory where the file should be: reads and writes fail
    for days in (0, 365):
        assert (
            _status(shim.name, idle_days=30, now=1_800_000_000.0 + days * _DAY_SECONDS).retirable
            is False
        )
