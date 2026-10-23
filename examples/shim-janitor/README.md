# Shim janitor

A weekly automation that reads `GET /v1/shims` and opens one pull request per
shim that has stopped firing. Background: [docs/SHIMS.md](../../docs/SHIMS.md).

## Run it once

```bash
cd /path/to/omnigent            # the checkout the janitor edits
export OMNIGENT_SERVER_URL=http://127.0.0.1:8000
omnigent run examples/shim-janitor
```

The agent needs a Claude provider (`omnigent setup`), the `gh` CLI logged in
for the repository, and a server whose firing log reflects real traffic (the
server and runner share `<data dir>/shims/firings.jsonl`, so point the janitor
at the server your sessions actually run under).

## Schedule it weekly

Register the agent on the server, then create a scheduled task
([docs/AUTOMATIONS.md](../../docs/AUTOMATIONS.md)). The task fires every
Monday at 09:00 UTC and runs the agent on the connected host that holds the
checkout:

```bash
omnigent agent add examples/shim-janitor       # prints the agent id, ag_...

curl -s -X POST "$OMNIGENT_SERVER_URL/v1/scheduled-tasks" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OMNIGENT_API_TOKEN" \
  -d '{
    "name": "shim janitor",
    "prompt": "Retire every shim that GET /v1/shims?idle_days=30 reports as retirable, one pull request each.",
    "rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0",
    "agent_id": "ag_...",
    "timezone": "UTC",
    "host_id": "host_...",
    "workspace": "/path/to/omnigent",
    "max_cost_usd": 5.00
  }'
```

`POST /v1/scheduled-tasks/{id}/run` fires it immediately to test the setup
without waiting for Monday. Pause it with `PATCH /v1/scheduled-tasks/{id}` and
`{"status": "paused"}`.

## What a run produces

For each retirable shim: a branch `janitor/retire-<shim-name>`, a commit
`Retire shim <name>`, and a pull request whose summary quotes the shim's
`reason`, `model_gap`, `last_fired_at`, `fire_count` and `owner`. Shims that
never fired but were added in the current `VERSION` are skipped and listed in
the run summary.
