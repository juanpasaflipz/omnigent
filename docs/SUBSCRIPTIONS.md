# Subscriptions (event wakes)

A subscription lets a running session wake up when something happens in the world: CI going red on a pull request, an issue being opened, a deploy finishing. Today an agent can wake on *time* (timers, automations) and on *its own sub-agents* (the inbox). Subscriptions add *external events* to that list, through one primitive rather than one tool per event source.

The flow is: an agent (or a client) creates a subscription naming a `source`, an event `kind`, and an optional field filter. When a matching event reaches the server, the server posts a hidden system message into the subscribed session, exactly like a timer firing, and the model handles it on its normal turn path. If the session is mid-turn the message queues like any other user input.

Everything described here is behind the `subscriptions` release feature flag and ships off by default.

## Enabling the flag

Set `OMNIGENT_FEATURES=subscriptions` on the server and restart it. The server then:

- mounts `POST /v1/events/github` and `POST /v1/events/webhook` (the ingest surface),
- mounts the `/v1/subscriptions` CRUD routes,
- builds an in-process event bus backed by a `subscriptions` table on the same database as automations.

The flag is server-only: it does not appear under `features` in `GET /v1/info`, and nothing changes in the web UI. See `designs/FEATURE_FLAGS.md` for the inventory and review release.

Separately, an agent only gets the `sys_subscribe` tools when its spec opts in with a top-level `subscriptions: true`, the same way `timers: true` gates the timer tools. Both switches must be on for an agent to subscribe itself.

## GitHub webhook setup

1. Pick a secret and set it on the server as `OMNIGENT_GITHUB_WEBHOOK_SECRET`. It is read on every delivery, so rotating it needs no restart. With the variable unset the endpoint answers **503** and accepts nothing.
2. In the repository (or organization) settings, add a webhook:
   - **Payload URL:** `https://<your-server>/v1/events/github`
   - **Content type:** `application/json`
   - **Secret:** the value from step 1
   - **Events:** choose *Let me select individual events* and tick **Check runs**, **Check suites**, **Pull requests**, **Issues**, **Issue comments**, and **Pushes**. Any other event is acknowledged with `202` and `matched: 0` but never published, so ticking more is harmless.
3. Send a test delivery. A `202` with `{"matched": 0, "delivered": 0, "suppressed": 0}` means the signature verified and there was simply nothing subscribed yet; a `401` means the secret does not match.

Each delivery is normalized into a `kind` and a flat `fields` dict:

| GitHub event / action | `kind` |
| --- | --- |
| `check_run` / `completed` | `check_run.completed` |
| `check_suite` / `completed` | `check_suite.completed` |
| `pull_request` / `opened`, `synchronize`, `closed` | `pull_request.opened` … |
| `issues` / `opened`, `closed` | `issues.opened`, `issues.closed` |
| `issue_comment` / `created` | `issue_comment.created` |
| `push` | `push` |

`fields` carries whichever of these the payload has: `repo` (`owner/name`), `action`, `conclusion`, `status`, `name` (the check's name), `pull_request` (number; the first when a check lists several), `issue` (number), `sender` (login), `ref`, and `url` (the subject's `html_url`). Nothing else from the payload is kept, so a filter can only name these keys.

## The generic webhook

Anything that is not GitHub posts to `POST /v1/events/webhook` under normal bearer authentication:

```jsonc
{
  "kind": "deploy.finished",
  "fields": {"env": "prod", "service": "api", "ok": true}
}
```

`kind` is a lowercase identifier with at most one dot (`deploy`, `deploy.finished`); `fields` is a flat object of up to 64 scalar values. The source is fixed to `webhook`. This is the path a Slack bot, a CI script, or a cron job uses to wake sessions on things the server does not natively ingest.

Both ingest routes answer `202` with `{"matched": n, "delivered": n, "suppressed": n}`: how many active subscriptions the event's filter matched, how many received a wake notice, and how many were skipped by the cooldown (below).

## Subscriptions

`POST /v1/subscriptions` takes `conversation_id`, `source` (`github` or `webhook`), `kind`, and optionally `match` (up to 16 scalar values) and `note` (up to 512 characters). The caller must have **edit** access to the conversation, because a wake notice starts a turn there. Rows are owner-scoped like automations: `GET /v1/subscriptions` lists only your own, and another user's id answers `404`. `PATCH /v1/subscriptions/{id}` changes `state` (`active` / `paused`), `note`, or `match`; `DELETE` soft-deletes.

Matching is a subset test: every key in `match` must be present in the event's `fields` with an equal value. Numbers and strings compare as strings, so `"pull_request": 412` matches a webhook that sends `"412"`. An empty filter matches every event of that `source`/`kind`.

## Agent tools

With `subscriptions: true` in the agent spec, the agent gets three tools, dispatched by the runner to the routes above:

- `sys_subscribe(source, kind, match?, note?)` — the runner fills in the calling session; the model never names one. It is **not** approval-gated, unlike `sys_scheduled_task_create`, because it can only wake the caller's own session rather than launch unattended work elsewhere.
- `sys_unsubscribe(subscription_id)`
- `sys_subscription_list()`

A typical use is waiting for CI instead of polling:

```jsonc
// sys_subscribe
{
  "source": "github",
  "kind": "check_run.completed",
  "match": {"repo": "acme/api", "pull_request": 412, "conclusion": "failure"},
  "note": "CI on my PR"
}
```

The agent then ends its turn. When a check on PR 412 fails, the session wakes with:

```
[System: event github/check_run.completed matched subscription 3f9c… ('CI on my PR')]
{"action":"completed","conclusion":"failure","name":"unit","pull_request":412,"repo":"acme/api","status":"completed","url":"https://github.com/acme/api/runs/…"}
```

The notice is a hidden meta user message (the user sees the agent act, not the plumbing). The composed system prompt for a subscribing agent tells the model to treat it as a routine runtime status message rather than instructions typed by a person, and to act on the fields in line with its task and the note. The JSON line is truncated at 4,000 characters.

Each subscription has a 10-second cooldown: a flapping job that completes several times in a row produces one wake, and the rest count as `suppressed`. `last_fired_at` and `fire_count` on the row record deliveries.

## Limits in v1

- **Existing sessions only.** A wake notice is posted into the subscribed session; an event never creates a new session. (Launching a session from an event can reuse the automation fire path later.)
- **No replay.** An event that arrives while the server is down, or that matches nothing, is gone. GitHub's own redelivery can resend a webhook, which is then matched afresh.
- **One process.** The bus matches and delivers inside the server that received the event. There is no cross-replica leasing, so with several replicas each delivery is handled by whichever one GitHub reached.
- **Slack is not native.** The Slack integration keeps its own routing; a Slack-driven wake should forward to `/v1/events/webhook`.
- **Delivery is best-effort.** If the session's runner is unreachable the failure is logged, the fire is still stamped (so the cooldown holds), and the ingest request still succeeds.
