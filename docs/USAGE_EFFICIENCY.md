# Usage reports

Omnigent records LLM usage per session: cumulative token counters and cost,
rolled up across every sub-agent the session spawned, and broken down per
model id. Two read-only surfaces expose it for the calling user:

| Surface | API | CLI | Answers |
| --- | --- | --- | --- |
| Cost report | `GET /v1/usage` | `omnigent usage` | *What* did I spend, and when? |
| Efficiency scorecard | `GET /v1/usage/efficiency` | `omnigent usage --efficiency` | *Why* did it cost that much? |

Both are scoped to the caller (401 in multi-user mode without an identity)
and are independent of the `usage_page` release flag, which gates only the
web Usage page.

## Efficiency scorecard

The scorecard derives the levers behind spend — cache hit rate, token
efficiency and model mix — from the per-model counters every harness already
reports (`input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `output_tokens`, `total_cost_usd`). Nothing
new is recorded.

### Formulas

For any set of summed counters (one session, one model, one harness, or the
whole window):

```text
prompt_tokens   = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
cache_hit_rate  = cache_read_input_tokens / prompt_tokens
output_share    = output_tokens / (prompt_tokens + output_tokens)
cost_per_session_usd = cost_usd / sessions
cost_share (per model) = model cost_usd / sum of all per-model cost_usd
```

A rate is `null` when its denominator is zero, so "nothing measured" is
distinguishable from "measured as 0". Group rates (harness, model, totals)
are computed from the group's **summed** counters, not averaged across
sessions, so a long expensive session weighs more than a short one.

Counters are read defensively: a missing or malformed counter counts as 0,
and when a session has no top-level counters they are summed from its
`by_model` buckets. Cost is only ever the session's authoritative
`total_cost_usd`; per-model costs are snapshots that are not guaranteed to
sum to it (native harnesses attribute one cumulative figure to the active
model), which is why `cost_share` divides by the per-model sum rather than
the window total — the shares then always form a mix that adds up to 1.

### Endpoint

```http
GET /v1/usage/efficiency?days=30&limit=50
```

| Query | Default | Range | Meaning |
| --- | --- | --- | --- |
| `days` | 30 | 1–366 | Window in UTC calendar days, including today |
| `limit` | 50 | 1–1000 | Maximum session rows returned |

The window uses the same inclusive calendar-day semantics as the cost
report's `cost_last_30d`: today plus the `days - 1` prior UTC days. A session
is in the window when its **last activity** falls inside it, and its whole
cumulative usage is counted (session usage is cumulative, not per-day), so
the scorecard totals are not expected to tie out to the daily-rollup summary
in `GET /v1/usage`.

Response shape (`EfficiencyReport` in `omnigent/server/schemas.py`):

```jsonc
{
  "object": "usage_efficiency_report",
  "window_days": 30,
  "totals": {
    "cost_usd": 142.10, "sessions": 61, "cost_per_session_usd": 2.33,
    "prompt_tokens": 1000000, "output_tokens": 90000,
    "cache_hit_rate": 0.71, "output_share": 0.09
  },
  "by_harness": { "claude-native": { /* same shape as totals */ } },
  "by_model":   { "claude-opus-5-5": { "cost_share": 0.82, "cache_hit_rate": 0.74, /* ... */ } },
  "sessions": [
    { "id": "conv_…", "title": "…", "harness": "claude-native",
      "model_ids": ["claude-opus-5-5"], "cost_usd": 9.1,
      "cache_hit_rate": 0.4, "output_share": 0.3,
      "prompt_tokens": 50000, "output_tokens": 20000, "created_at": 1700000000 }
  ]
}
```

`totals`, `by_harness` and `by_model` cover every session in the window;
`sessions` is the same set sorted by `cost_usd` descending and truncated to
`limit`, so the worst offenders are on top. Sessions whose harness cannot be
resolved group under `"unknown"`.

### CLI

```sh
omnigent usage --efficiency                 # last 30 days, top 50 sessions
omnigent usage --efficiency --days 7 --limit 20
omnigent usage --efficiency --json          # raw report
```

The table prints a totals line (cost, sessions, cache hit %, output share %,
cost per session), a per-harness table, a per-model table with cost share,
then the top sessions. A session row is marked `!` when its cache hit rate
is more than 25 percentage points below its harness average (computed
client-side from `by_harness`). Rates the server could not compute print as
`—`.

### How to read it

- **Low cache hit rate on a long session** usually means prompt churn: the
  tools, system instructions or attached context are changing between turns,
  so each turn re-reads the prompt instead of hitting the cache. Check what
  the harness sends at the top of every request and keep it stable.
- **High output share on a coding agent** usually means it is rewriting
  files instead of editing them (or narrating at length). Prefer targeted
  edit tools and shorter responses; output tokens are the expensive ones.
- **A model with a large `cost_share` but a low `cache_hit_rate`** is the
  first place to look for savings: it is both the dominant spend and the one
  getting the least help from the cache.
- **Compare harnesses, not just models.** The same model behind two harnesses
  can show very different cache hit rates; the harness decides what is
  cacheable.
