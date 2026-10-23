# The harness flywheel: subscriptions, shims, and efficiency

**Status:** In progress (fork branch `flywheel/harness-primitives`)
**Motivation:** Jediah Katz, "Is the Agent Harness Dead?" (AI Engineer, 2026)

## 1. Thesis

Frontier models have absorbed the coding tricks a harness used to supply
(explore the codebase, run the tests, keep going). Two recent studies found that
harness choice no longer moves SWE-bench Lite or MLE-bench scores for the newest
models. What a harness still owns, and what a model cannot absorb:

1. **The next frontier of tasks.** Long-lived coordinator agents that wake on
   events, farm work out to workers, and report back without flooding the user.
2. **Runtime and infrastructure.** Work that runs for days has to run off the
   laptop.
3. **Trust and security.** Scoped permissions, approval gates, spend caps.
4. **Efficiency.** Same eval score, very different cost per task. Cache hit
   rate, token efficiency, and model mix are harness decisions.

The talk calls the loop that drives this the **data flywheel**: the harness
makes a hard task reachable, users attempt it, labs get the data, the model
learns it natively, the harness deletes the scaffolding, and the harness moves
to the next frontier. A harness that wants to survive this loop needs three
things it does not usually have: a way to wake on events, a way to track the
hacks it will later delete, and a way to measure whether it is getting cheaper.

Omnigent already has the runtime (sandboxes, hosts, resume), the trust layer
(policies, approvals, cost budgets), coordinator/worker sessions (`sys_session_send`,
async inbox), timers, and RRULE scheduled tasks. This design adds the three
missing primitives.

## 2. Primitive 1: subscriptions

**Problem.** Today an agent can wake on *time* (timers, scheduled tasks) and on
*its own sub-agents* (inbox). It cannot wake on *the world*: a Slack post, CI
going red on a PR it cares about, an issue being opened. The Slack integration
maps threads to sessions, but it is a separate daemon with its own routing, and
GitHub events have no ingestion path at all.

**Shape.** One framework and one tool. An agent calls `sys_subscribe` with a
source, an event kind, and a field filter. When a matching event arrives, the
server posts a wake notice into the subscribed session, exactly like a timer
firing, so the model handles it on the normal ingest path.

```jsonc
// sys_subscribe
{
  "source": "github",
  "kind": "check_run.completed",
  "match": {"repo": "acme/api", "conclusion": "failure", "pull_request": 412},
  "note": "CI on my PR"
}
```

Wake notice shape (posted as a hidden meta user message, mirroring
`[System: timer X fired]`):

```
[System: event github/check_run.completed matched subscription sub_… ("CI on my PR")]
{"repo": "acme/api", "conclusion": "failure", "pull_request": 412, "name": "unit", "url": "…"}
```

**Components.**

| Piece | Location | Notes |
| --- | --- | --- |
| Spec flag | `omnigent/spec/{types,parser,omnigent}.py` | Top-level `subscriptions: true`, default `False`, modelled on `timers:`. |
| Entity | `omnigent/entities/subscription.py` | `Subscription`: id, user_id, workspace_id, conversation_id, source, kind, match (JSON), note, state (active/paused/deleted), created_at, updated_at, last_fired_at, fire_count. |
| Store | `omnigent/stores/subscription_store/` | Interface + SQLAlchemy store, modelled on `scheduled_task_store`. `list_active(source, kind)` is the hot path. |
| DB | `omnigent/db/db_models.py`, Alembic migration | `subscriptions` table. Index on `(state, source, kind)`. |
| Event bus | `omnigent/server/events/bus.py` | `CanonicalEvent(source, kind, fields, received_at)`; `publish()` loads active subscriptions for `(source, kind)`, applies `match` as a subset test against `fields`, delivers wake notices, stamps `last_fired_at` / `fire_count`. Per-subscription cooldown (default 10 s) so a flapping CI job does not spam a session. |
| Ingest | `omnigent/server/routes/events.py` | `POST /v1/events/github` verifies `X-Hub-Signature-256` with `OMNIGENT_GITHUB_WEBHOOK_SECRET` and normalizes `check_run`, `check_suite`, `pull_request`, `issues`, `issue_comment`, `push`. `POST /v1/events/webhook` takes `{kind, fields}` under normal bearer auth for anything else (Slack bot, CI scripts, cron). |
| REST | `omnigent/server/routes/subscriptions.py` | CRUD under `/v1/subscriptions`, owner-scoped like scheduled tasks. |
| Agent tools | `omnigent/tools/builtins/subscriptions.py` | `sys_subscribe`, `sys_unsubscribe`, `sys_subscription_list`. Runner-dispatched to REST, mirroring `sys_scheduled_task_*`. |
| Runner dispatch | `omnigent/runner/tool_dispatch.py` | `_SUBSCRIPTION_TOOLS` + `_execute_subscription_tool`. |
| Prompt | `omnigent/runtime/prompt.py` | `EVENT_WAKE_NOTICE_INSTRUCTION`, appended only when `spec.subscriptions`. |
| Feature flag | `omnigent/server/feature_flags.py` | `subscriptions`, default off, review by 0.24.0. Routes register only when on. |

**Delivery.** Wake notices go through the same server-side path a timer uses:
append a meta user message to the session and let the normal turn dispatch
start a turn if the session is idle. If the session is mid-turn the message
queues like any other user input.

**Not in v1.** Launching a *new* session from an event (reuse
`server/scheduled/fire.py` later), Slack-native subscriptions (the Slack daemon
should forward to `/v1/events/webhook`), event replay, multi-replica leasing.

**Delivered in v2.**

- *Launch a new session from an event — done.* A subscription targets either a
  `conversation_id` (v1 wake) or an `agent_id` + `prompt_template` with the
  scheduled-task launch fields (`execution_target`, `host_id`, `workspace`,
  `max_cost_usd`, `model_override`); a check constraint keeps exactly one
  target. A match is handed to `fire.launch_session` with a `LaunchRequest`,
  the entry point the scheduled-task fire path now exposes, so target
  resolution, session creation, the owner grant, the cost cap and the launch
  are shared with automations rather than duplicated. Launched sessions are
  labelled with the subscription and event. An agent-created launch
  subscription parks on the same approval gate as
  `sys_scheduled_task_create`.
- *Slack forwards to the bus — done.* With `OMNIGENT_SLACK_FORWARD_EVENTS=true`
  the Slack bot posts channel messages and thread replies to
  `/v1/events/webhook` as `slack.message`, using the poster's delegated token.
  There is still no `slack` source on the server; Slack rides the generic
  webhook.
- *GitHub App webhook — documented.* `docs/GITHUB_APP_SETUP.md` covers pointing
  the App's webhook at `/v1/events/github` so one App covers every installed
  repository.

Still open: event replay, multi-replica leasing, run history for launch
subscriptions (failures are only logged), and editing launch fields in place.

## 3. Primitive 2: the shim registry

**Problem.** Every harness carries deterministic patches over model gaps: a
nudge when the model does not reply fast enough, a provider prefix the model
forgot, an overflow detector because a provider does not report it
structurally. The talk's rule: *log every firing centrally, and let a bot open a
PR to delete any shim that stopped firing.* Omnigent has these shims scattered
as comments.

**Shape.** `omnigent/runtime/shims.py`:

```python
COMPACTION_BARE_MODEL = register_shim(
    "compaction.bare_model_provider_prefix",
    reason="Generic LLM client routes prefix-less ids to OpenAI.",
    model_gap="Model (or caller) emits a bare provider model id.",
    added_in="0.19.0",
    review_by="0.24.0",
    owner="runtime",
)
...
shim_fired(COMPACTION_BARE_MODEL, model=model)
```

Each firing increments an in-process counter, emits an OpenTelemetry counter
`omnigent.shim.fired` with `shim`, `model`, `harness` attributes, and appends a
JSONL line under `<data dir>/shims/firings.jsonl` so the record survives a
restart and crosses the runner/server process boundary on one machine.

`GET /v1/shims` returns every registered shim with lifetime fire count, last
fired time, per-model counts, and `retirable: true` when the shim has not fired
for `idle_days` (default 30) on any model that is still in use. The
`examples/shim-janitor` agent is a weekly automation that reads that endpoint
and opens a PR removing retirable shims.

Three existing shims are wired in v1 so the registry is not empty:
`_route_bare_model_for_compaction` (workflow.py), the context-overflow text
sniffing in `llm_retry.py`, and one more the implementer judges clearest.

## 4. Primitive 3: the efficiency scorecard

**Problem.** `GET /v1/usage` reports cost. It does not report *why*. The
levers the talk names, cache hit rate, token efficiency, and model mix, are all
derivable from the per-model token counters Omnigent already records
(`cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`,
`output_tokens`, `total_cost_usd` in `by_model`).

**Shape.** `GET /v1/usage/efficiency?days=30` returns:

```jsonc
{
  "window_days": 30,
  "totals": {
    "cost_usd": 142.10,
    "sessions": 61,
    "cache_hit_rate": 0.71,      // cache_read / (input + cache_read + cache_creation)
    "output_share": 0.09,        // output / (input + cache_read + cache_creation + output)
    "cost_per_session_usd": 2.33
  },
  "by_harness": {"claude-native": {...}, "codex-native": {...}},
  "by_model": {"claude-opus-5-5": {"cost_share": 0.82, "cache_hit_rate": 0.74, ...}},
  "sessions": [{"id": "...", "harness": "...", "cache_hit_rate": 0.4, "cost_usd": 9.1, ...}]
}
```

Sessions are sorted by cost descending so the worst offenders are on top. The
CLI gets `omnigent usage --efficiency`. A session whose cache hit rate is far
below its harness average is the first thing to investigate.

## 5. Principles carried from the talk

- **Primitives over integrations.** One `sys_subscribe`, not one tool per
  event source. New sources add a normalizer, not a tool.
- **Ship and unship.** Everything new sits behind a release feature flag so it
  can be removed without a refactor.
- **Expose less.** Wake notices are hidden meta messages; the user sees the
  agent act, not the plumbing.
- **Log the hacks.** Shims are first-class, logged, and expected to die.
- **Measure cost, not just score.** Efficiency is a product surface.
