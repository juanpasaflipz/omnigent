# Factory: a software-factory meta-harness

A worked example of the three flywheel primitives in `designs/HARNESS_FLYWHEEL.md`.
One foreman owns a bug from report to verified deploy. It never polls: every
step is woken by an event or by a worker finishing.

```
issue opened ──► foreman wakes ──► triager (low effort, read-only)
                                       │
                          severity ≥ medium + repro
                                       ▼
                                   fixer (sandbox, writes, tests, recording, PR)
                                       │
                 CI red on that PR ──► foreman wakes ──► fixer again
                                       │
                            human merges (policy ASKs)
                                       ▼
                                   watcher subscribes to deploy.finished
                                       │
                 deploy.finished ──► watcher wakes ──► health checks ──► verdict
```

## Run it

```bash
# 1. Start the server. Subscriptions are on by default in this distribution.
omnigent server

# 2. Point GitHub at the server (Settings → Webhooks on the repo):
#    Payload URL  https://<your-server>/v1/events/github
#    Secret       the value of OMNIGENT_GITHUB_WEBHOOK_SECRET on the server
#    Events       Issues, Check runs, Pull requests

# 3. Have your deploy pipeline announce itself:
curl -X POST https://<your-server>/v1/events/webhook \
  -H "authorization: Bearer $OMNIGENT_TOKEN" -H "content-type: application/json" \
  -d '{"kind":"deploy.finished","fields":{"service":"api","status":"ok","version":"1.4.2"}}'

# 4. Start the foreman and name the repo.
omnigent run examples/factory
> Watch acme/api. Subscribe to new issues and failed check runs on open PRs.
```

The foreman subscribes and ends its turn. From then on it only speaks when
something happens.

### Without a running foreman

A launch subscription names an agent and a prompt template instead of a
session; every matching event starts a fresh session for you on the server
(see [`docs/SUBSCRIPTIONS.md`](../../docs/SUBSCRIPTIONS.md#launch-subscriptions)).
Find the triager's agent id with `GET /v1/agents`, then:

```bash
curl -X POST https://<your-server>/v1/subscriptions \
  -H "authorization: Bearer $OMNIGENT_TOKEN" -H "content-type: application/json" \
  -d '{
        "agent_id": "<triager agent id>",
        "source": "github",
        "kind": "issues.opened",
        "match": {"repo": "acme/api"},
        "note": "triage",
        "prompt_template": "A new issue was opened in {fields_json}. Reproduce it, label it, and comment with what you found.",
        "execution_target": "managed_sandbox",
        "max_cost_usd": 1
      }'
```

Each new issue in `acme/api` now opens a session titled `issues.opened: triage`,
labelled with the subscription id, with no human action. If the foreman creates
this subscription itself (`sys_subscribe` with `agent_id`), the call waits for
your approval first, like creating an automation.

## What each piece demonstrates

| Piece | Flywheel idea |
| --- | --- |
| `subscriptions: true` on the foreman and watcher | One `sys_subscribe` primitive instead of a tool per event source. No timers, no polling. |
| `triager` and `watcher` at low reasoning effort, `fixer` at full effort | Effort and model mix. Pin a smaller model on the cheap workers with `model:` if your provider has one, then check `omnigent usage --efficiency` after a day: the per-model table shows where the money goes. |
| `blast_radius` with `gate_pushes: true` on the foreman | Acting versus asking. The agent acts freely inside the sandbox and asks at the shared-state boundary. |
| `cost_budget` on every agent | Trust. A runaway loop stops at a number you chose. |
| The fixer's recording requirement | Proof, not claims. The PR carries a video the human can watch before merging. |
| Everything runs server-side | Off the laptop. Close the lid; the foreman still wakes. |

## Limits of this example

- The foreman's own `sys_subscribe` calls wake the foreman's session, so the
  foreman must be running to receive them. With launch subscriptions (v2) you
  do not need it running for intake: register a subscription that starts a
  triage session per new issue, and nothing has to be up when the issue
  arrives. See "Without a running foreman" below.
- `check_run.completed` fires for every check on every PR in the repo. The
  foreman is told to ignore PRs it did not open; a `match` on `pull_request`
  number per fixer PR would be tighter and is a good first contribution.
