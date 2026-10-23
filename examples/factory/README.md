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
# 1. Subscriptions are behind a release flag.
OMNIGENT_FEATURES=subscriptions omnigent server

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

- Subscriptions v1 wake an existing session. The foreman must already be
  running to receive the first event. Launching a fresh session from an event
  is the next phase (see the fork plan).
- `check_run.completed` fires for every check on every PR in the repo. The
  foreman is told to ignore PRs it did not open; a `match` on `pull_request`
  number per fixer PR would be tighter and is a good first contribution.
