"""Process-wide registry of deterministic patches over model gaps ("shims").

Every harness accumulates small deterministic fixes for things a model or
provider gets wrong: a provider prefix the caller forgot, an overflow detector
because the provider reports it only as prose, a rewording so the parent model
does not misread an empty hand-off. Left as comments, those patches outlive the
gap they cover and nobody notices. Registering each one here, and recording
every firing centrally, turns them into data: ``GET /v1/shims`` reports which
shims still fire, and the ``examples/shim-janitor`` automation opens a PR to
delete the ones that went quiet. See ``designs/HARNESS_FLYWHEEL.md`` section 3.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes

from omnigent.process_logging import data_dir
from omnigent.runtime.telemetry import telemetry_guarded

_logger = logging.getLogger(__name__)

_OTEL_METER_NAME = "omnigent.shims"
FIRED_METRIC_NAME = "omnigent.shim.fired"

#: Relative location of the firing log under the runtime data dir.
FIRINGS_RELATIVE_PATH = Path("shims") / "firings.jsonl"


@dataclass(frozen=True)
class Shim:
    """
    One registered deterministic patch over a model or provider gap.

    :param name: Stable dotted identifier, e.g.
        ``"compaction.bare_model_provider_prefix"``.
    :param reason: Why the patch exists, in one sentence.
    :param model_gap: The behaviour the model (or provider) gets wrong.
    :param added_in: Omnigent version that introduced the shim, e.g. ``"0.19.0"``.
    :param review_by: Version by which the shim must be re-justified or deleted.
    :param owner: Team or module that owns the shim, e.g. ``"runtime"``.
    """

    name: str
    reason: str
    model_gap: str
    added_in: str
    review_by: str
    owner: str


@dataclass(frozen=True)
class ShimStatus:
    """
    A registered shim merged with its lifetime firing record.

    :param name: The shim's registered name.
    :param reason: Why the patch exists.
    :param model_gap: The behaviour the patch covers.
    :param added_in: Version that introduced the shim.
    :param review_by: Version by which the shim must be re-justified or deleted.
    :param owner: Team or module that owns the shim.
    :param fire_count: Lifetime firings across this process and the firing log.
    :param last_fired_at: ISO-8601 UTC timestamp of the latest firing, or
        ``None`` when the shim has never fired.
    :param by_model: Firings per model id; unknown models are keyed ``"unknown"``.
    :param retirable: ``True`` when the shim never fired or has been idle longer
        than the snapshot's ``idle_days``.
    """

    name: str
    reason: str
    model_gap: str
    added_in: str
    review_by: str
    owner: str
    fire_count: int
    last_fired_at: str | None
    by_model: dict[str, int] = field(default_factory=dict)
    retirable: bool = False


@dataclass
class _FiringRecord:
    """
    Mutable per-shim counters kept in process memory.

    :param total: Firings observed by this process.
    :param persisted: Firings this process appended to the firing log; the
        snapshot subtracts them so a merge with the log counts each once.
    :param by_model: Per-model firings observed by this process.
    :param persisted_by_model: Per-model firings this process appended to the log.
    :param last_fired: Epoch seconds of the latest firing, or ``None``.
    """

    total: int = 0
    persisted: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    persisted_by_model: dict[str, int] = field(default_factory=dict)
    last_fired: float | None = None


_UNKNOWN_MODEL = "unknown"

_lock = threading.Lock()
# Keyed by code-defined shim names; firing counts are process telemetry, not tenant data.
# custom-lint: disable-next=workspace-scoped-cache -- shim names are global constants
_registry: dict[str, Shim] = {}
# custom-lint: disable-next=workspace-scoped-cache -- keyed by global shim name
_records: dict[str, _FiringRecord] = {}


class _CounterLike(Protocol):
    """Subset of the OpenTelemetry counter API used here."""

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """Add a value with optional metric attributes."""


def register_shim(
    name: str,
    *,
    reason: str,
    model_gap: str,
    added_in: str,
    review_by: str,
    owner: str,
) -> Shim:
    """
    Register a shim, or return the existing registration with the same fields.

    Registration is idempotent by name so a module can be re-imported (tests,
    reloads) without tripping; two different definitions under one name is a
    programming error.

    :param name: Stable dotted identifier for the shim.
    :param reason: Why the patch exists, in one sentence.
    :param model_gap: The behaviour the model or provider gets wrong.
    :param added_in: Version that introduced the shim.
    :param review_by: Version by which the shim must be re-justified or deleted.
    :param owner: Team or module that owns the shim.
    :returns: The registered :class:`Shim`.
    :raises ValueError: When ``name`` is already registered with different fields.
    """
    shim = Shim(
        name=name,
        reason=reason,
        model_gap=model_gap,
        added_in=added_in,
        review_by=review_by,
        owner=owner,
    )
    with _lock:
        existing = _registry.get(name)
        if existing is None:
            _registry[name] = shim
            _records.setdefault(name, _FiringRecord())
            return shim
        if existing != shim:
            raise ValueError(
                f"shim {name!r} is already registered with different fields: {existing} != {shim}"
            )
        return existing


def registered_shims() -> list[Shim]:
    """
    Return every registered shim in registration order.

    :returns: Registered shims.
    """
    with _lock:
        return list(_registry.values())


def firings_path() -> Path:
    """
    Return the JSONL firing log path under the runtime data dir.

    Resolved on every call so ``OMNIGENT_DATA_DIR`` set after import (tests,
    worktree isolation) is honoured.

    :returns: ``<data dir>/shims/firings.jsonl``.
    """
    return data_dir() / FIRINGS_RELATIVE_PATH


@lru_cache(maxsize=1)
def _fired_counter() -> _CounterLike:
    """Return the process-wide ``omnigent.shim.fired`` counter instrument."""
    return otel_metrics.get_meter(_OTEL_METER_NAME).create_counter(
        FIRED_METRIC_NAME,
        unit="{firing}",
        description="Deterministic model-gap shims fired, labelled by shim, model and harness.",
    )


@telemetry_guarded
def _record_fired_metric(shim: Shim, model: str, harness: str) -> None:
    """Emit one ``omnigent.shim.fired`` increment."""
    _fired_counter().add(1, attributes={"shim": shim.name, "model": model, "harness": harness})


def _append_firing(line: dict[str, object]) -> bool:
    """
    Append one firing to the JSONL log.

    :param line: The JSON-serialisable firing record.
    :returns: ``True`` when the line was written.
    """
    path = firings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, separators=(",", ":")) + "\n")
    except OSError:
        _logger.debug("could not append shim firing to %s", path, exc_info=True)
        return False
    return True


def shim_fired(shim: Shim, *, model: str | None = None, harness: str | None = None) -> None:
    """
    Record that ``shim`` just patched over its gap.

    Updates the in-memory counters, emits the ``omnigent.shim.fired``
    OpenTelemetry counter, logs one INFO line, and appends to the JSONL firing
    log. None of the side channels can raise into the caller: a shim must never
    be the reason a turn fails.

    :param shim: The registered shim that fired.
    :param model: Model id the shim fired for, when known.
    :param harness: Harness id the shim fired under, when known.
    """
    fired_at = time.time()
    model_key = model or _UNKNOWN_MODEL
    harness_key = harness or _UNKNOWN_MODEL
    with _lock:
        record = _records.setdefault(shim.name, _FiringRecord())
        record.total += 1
        record.by_model[model_key] = record.by_model.get(model_key, 0) + 1
        record.last_fired = fired_at

    _record_fired_metric(shim, model_key, harness_key)
    _logger.info(
        "shim fired: %s model=%s harness=%s",
        shim.name,
        model_key,
        harness_key,
        extra={"shim": shim.name, "model": model_key, "harness": harness_key},
    )

    persisted = _append_firing(
        {"ts": fired_at, "shim": shim.name, "model": model, "harness": harness}
    )
    if persisted:
        with _lock:
            record = _records[shim.name]
            record.persisted += 1
            record.persisted_by_model[model_key] = record.persisted_by_model.get(model_key, 0) + 1


def _read_firing_log() -> dict[str, _FiringRecord]:
    """
    Aggregate the JSONL firing log into per-shim records.

    Malformed lines are skipped so one bad write cannot hide every other firing.

    :returns: Per-shim totals, per-model counts and latest timestamp from the log.
    """
    aggregated: dict[str, _FiringRecord] = {}
    path = firings_path()
    try:
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return aggregated
    for raw_line in lines:
        try:
            entry = json.loads(raw_line)
            name = str(entry["shim"])
            fired_at = float(entry["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        model_key = str(entry.get("model") or _UNKNOWN_MODEL)
        record = aggregated.setdefault(name, _FiringRecord())
        record.total += 1
        record.by_model[model_key] = record.by_model.get(model_key, 0) + 1
        if record.last_fired is None or fired_at > record.last_fired:
            record.last_fired = fired_at
    return aggregated


def _merge_records(memory: _FiringRecord, logged: _FiringRecord | None) -> _FiringRecord:
    """
    Combine this process's counters with the firing log.

    Firings this process already persisted are present in ``logged``, so only
    the unpersisted remainder of ``memory`` is added on top.

    :param memory: Counters observed by this process.
    :param logged: Counters aggregated from the log, or ``None`` when absent.
    :returns: The merged record.
    """
    merged = _FiringRecord(total=memory.total - memory.persisted, last_fired=memory.last_fired)
    for model_key, count in memory.by_model.items():
        unpersisted = count - memory.persisted_by_model.get(model_key, 0)
        if unpersisted > 0:
            merged.by_model[model_key] = unpersisted
    if logged is None:
        return merged
    merged.total += logged.total
    for model_key, count in logged.by_model.items():
        merged.by_model[model_key] = merged.by_model.get(model_key, 0) + count
    if logged.last_fired is not None and (
        merged.last_fired is None or logged.last_fired > merged.last_fired
    ):
        merged.last_fired = logged.last_fired
    return merged


def _iso_utc(epoch_seconds: float) -> str:
    """Format epoch seconds as an ISO-8601 UTC timestamp."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def snapshot(*, idle_days: int = 30, now: float | None = None) -> list[ShimStatus]:
    """
    Report every registered shim with its merged firing record.

    The in-memory counters cover this process; the JSONL log adds firings from
    other processes on the same machine (the runner and the server share one
    data dir), so the server can report a shim that only ever fires in the
    runner.

    :param idle_days: Days without a firing after which a shim is ``retirable``.
    :param now: Epoch seconds to measure idleness against; defaults to the wall clock.
    :returns: One :class:`ShimStatus` per registered shim, in registration order.
    """
    reference = time.time() if now is None else now
    idle_seconds = idle_days * 86400
    logged = _read_firing_log()
    with _lock:
        shims = list(_registry.values())
        memory = {name: _records.get(name, _FiringRecord()) for name in _registry}
    statuses: list[ShimStatus] = []
    for shim in shims:
        merged = _merge_records(memory[shim.name], logged.get(shim.name))
        last_fired = merged.last_fired
        retirable = last_fired is None or (reference - last_fired) > idle_seconds
        statuses.append(
            ShimStatus(
                **asdict(shim),
                fire_count=merged.total,
                last_fired_at=None if last_fired is None else _iso_utc(last_fired),
                by_model=dict(sorted(merged.by_model.items())),
                retirable=retirable,
            )
        )
    return statuses


def reset_for_tests() -> None:
    """Forget every registration, counter and cached instrument."""
    with _lock:
        _registry.clear()
        _records.clear()
    _fired_counter.cache_clear()


__all__ = [
    "FIRED_METRIC_NAME",
    "FIRINGS_RELATIVE_PATH",
    "Shim",
    "ShimStatus",
    "firings_path",
    "register_shim",
    "registered_shims",
    "reset_for_tests",
    "shim_fired",
    "snapshot",
]
