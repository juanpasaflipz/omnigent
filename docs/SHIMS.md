# Shims

A **shim** is a deterministic patch over a model or provider gap: code that
exists only because a model emits a bare provider id, a provider reports a
context overflow as prose, or a parent model misreads an empty hand-off. Each
one is correct the day it is written and dead weight the day the gap closes.
The shim registry (`omnigent/runtime/shims.py`) turns those patches into data
so the harness only keeps the ones that still fire.

## Registering a shim

Register once at module level, fire at the exact line that patches the gap:

```python
from omnigent.runtime.shims import register_shim, shim_fired

COMPACTION_BARE_MODEL_SHIM = register_shim(
    "compaction.bare_model_provider_prefix",
    reason="Generic LLM client routes prefix-less ids to OpenAI.",
    model_gap="Model (or caller) emits a bare provider model id.",
    added_in="0.19.0",
    review_by="0.24.0",
    owner="runtime",
)


def _route_bare_model_for_compaction(llm_config: LLMConfig) -> LLMConfig:
    ...
    if model.startswith("claude-"):
        shim_fired(COMPACTION_BARE_MODEL_SHIM, model=model)
        return replace(llm_config, model=f"anthropic/{model}")
    return llm_config
```

- Names are stable dotted identifiers: `<area>.<what_it_patches>`.
- `register_shim` is idempotent by name; the same name with different fields
  raises `ValueError`, so two modules cannot silently claim one shim.
- Fire only on the branch that actually rewrites behaviour, never on the
  pass-through, otherwise the count measures traffic instead of the gap.
- Pass `model=` and `harness=` when they are in scope. A missing model is
  bucketed as `"unknown"`.
- **Every shim must carry a `review_by` version.** It is the latest release in
  which the shim may ship without someone re-justifying it in a pull request;
  `added_in` records when it arrived. There is no open-ended shim.

Each firing increments an in-memory counter, emits the OpenTelemetry counter
`omnigent.shim.fired` with `shim`, `model` and `harness` attributes (subject to
the `OMNIGENT_TELEMETRY_ENABLED` opt-in like every other metric), logs one INFO
line, and appends `{"ts", "shim", "model", "harness"}` to
`<data dir>/shims/firings.jsonl`. Firing never raises into the caller: an
unwritable data dir degrades to in-memory counting.

### Registered in v1

| Name | Where | Patches |
| --- | --- | --- |
| `compaction.bare_model_provider_prefix` | `omnigent/runtime/workflow.py` | Bare `claude-*` / `databricks-*` ids sent to the generic client. |
| `llm.context_overflow_text_sniff` | `omnigent/runtime/llm_retry.py` | Providers reporting overflow only as prose in a 400/413 body. |
| `inbox.empty_completion_phrasing` | `omnigent/runner/tool_dispatch.py` | A parent model reading `…returned: ` with no output as a truncated hand-off. |

## Reading `GET /v1/shims`

```
GET /v1/shims?idle_days=30
```

`idle_days` defaults to 30 and accepts 1..3650. The response lists every
registered shim in registration order:

```json
{
  "idle_days": 30,
  "shims": [
    {
      "name": "compaction.bare_model_provider_prefix",
      "reason": "Generic LLM client routes prefix-less ids to OpenAI.",
      "model_gap": "Model (or caller) emits a bare provider model id.",
      "added_in": "0.19.0",
      "review_by": "0.24.0",
      "owner": "runtime",
      "fire_count": 12,
      "last_fired_at": "2026-10-20T14:03:11+00:00",
      "by_model": {"claude-haiku-4-5-20251001": 12},
      "retirable": false,
      "first_seen_at": "2026-09-01T09:00:00+00:00"
    }
  ]
}
```

The report merges the server process's counters with the JSONL firing log, so
a shim that only fires inside the runner on the same machine is still counted.
`retirable` is `true` once the deployment has observed the shim for at least
`idle_days` and it has not fired in that window. Observation starts at
`first_seen_at`, recorded the first time the report sees the shim registered
(`<data dir>/shims/first_seen.json`), or at an earlier logged firing. A fresh
install therefore reports nothing as retirable until the window has passed. Firings logged for a shim that is no longer registered are
ignored: deleting the registration is how a shim leaves the report.

In multi-user deployments the route is admin-only (401 without an identity,
403 for non-admins), because firing counts span every workspace on the server.

## The janitor automation

[`examples/shim-janitor`](../examples/shim-janitor) is an agent that calls
`GET /v1/shims?idle_days=30` on its own server and, for each `retirable` shim,
opens a pull request deleting the call site and the registration, quoting the
shim's `reason` and last-fired date. Its README shows how to schedule it
weekly with `POST /v1/scheduled-tasks`. The reviewer of that pull request makes
the final call; the janitor never merges.

## Retiring by hand

Delete the `register_shim(...)` block, every `shim_fired(...)` for it, the tests
that assert it fires, and its row in the table above. Decide separately whether
the patched code path itself goes with it: retiring the bookkeeping is not the
same as deleting the behaviour it measured.
