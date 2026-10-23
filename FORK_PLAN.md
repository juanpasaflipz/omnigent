# WakeVane plan: a harness built for the flywheel

WakeVane is the name of this distribution of Omnigent. The sections below
still say "the fork" where they describe repository mechanics.

This is the operating plan for forking `omnigent-ai/omnigent` into a harness we
can share publicly, with the ideas from Jediah Katz's "Is the Agent Harness
Dead?" talk built in. The technical design lives in the repo at
`designs/HARNESS_FLYWHEEL.md`. This file is the plan around it: why fork, what
to keep, what to add, how to run it, and how to tell the story.

## Status (2026-10-23)

| Phase | State | What exists | What is left |
| --- | --- | --- | --- |
| 0 | Done | Subscriptions, shim registry, efficiency scorecard, factory and shim-janitor examples, design doc | Nothing |
| 1 | Done | Fork published, `dist` integration branch, first tag, install from `@dist` verified on a clean tool dir, NOTICE line, Flywheel CI workflow, launch post in `docs/blog/` | Enable Actions on the fork, then run `scripts/fork_trim_workflows.sh juanpasaflipz/omnigent` |
| 2 | Built, not proven live | Subscriptions launch new sessions through the scheduled-task fire path, with an approval gate for agent-created launches; Slack forwards messages as `slack.message` events; GitHub App webhook setup documented | One live run: a GitHub App webhook on a test repo opening an issue that starts a triage session |
| 3 | Built, janitor not scheduled | Efficiency tab on the Usage page (`usage_page` flag), Shims page (`shims_page` flag), retirement only after a full observation window | Deploy a server and create the weekly janitor automation from `examples/shim-janitor/README.md` |
| 4 | Built, no live numbers | `cost_per_solved_task` bench probe with measurement rendering; offline runs report SKIPPED | One live bench run with gateway credentials for claude-native and codex-native |
| 5 | Prepared | Upstream-ready branches `upstream/shims` and `upstream/usage-efficiency`, each passing its tests against upstream `main`, with PR descriptions drafted | Run a week on our own use, file the feature issues upstream requires, then open the PRs. Subscriptions goes upstream after its live run |

**Weekly upstream sync is not automated.** A scheduled workflow that pushes
to this repository with a write token needs the owner's explicit decision.
Until then, sync by hand:

```bash
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout dist && git merge main && git push origin dist
```

## 1. Why Omnigent, and why fork rather than start fresh

The talk's claim is that a harness no longer adds coding intelligence. What it
adds is (a) the next frontier of tasks, (b) runtime that runs off the laptop,
(c) trust and security, and (d) efficiency. Omnigent already has three of the
four as mature, tested code:

| Talk's requirement | Omnigent today |
| --- | --- |
| Off-laptop runtime, resumable state | Hosts, managed sandboxes on 12 providers, session resume, server-side persistence |
| Trust and security | Policy engine (ALLOW/DENY/ASK), cost budgets, scoped grants, OS sandboxing, secretless credential proxy |
| Coordinator and worker agents | `sys_session_send`, async inbox, steerable sub-agents, per-role harness and model mix |
| Harness-agnostic | Claude Code, Codex, Cursor, OpenCode, Hermes, Pi, Devin, Kiro, custom ACP agents behind one API |
| Ship and unship | Release feature flags with review-by releases |
| Measurable | OpenTelemetry spans with cache token attributes, per-model cost rollups, a conformance bench per harness |

Starting fresh would mean rebuilding half a million lines to get back here. The
gaps are narrow and specific, which is exactly what a fork is for.

## 2. What the fork adds (the three primitives)

Implemented on branch `flywheel/harness-primitives`:

1. **Subscriptions.** One tool, `sys_subscribe`, that lets an agent wake when
   the world changes: CI goes red on its PR, an issue opens, a Slack thread
   gets a reply, a generic webhook fires. One framework, new sources add a
   normalizer rather than a tool. Behind the `subscriptions` feature flag.
2. **Shim registry.** Every deterministic patch over a model gap is registered
   with a reason and a review-by release, logged on each firing, and listed at
   `GET /v1/shims` with a `retirable` flag. The `shim-janitor` example agent
   opens PRs to delete shims that stopped firing. This is how the harness
   deletes itself on purpose instead of by archaeology.
3. **Efficiency scorecard.** `GET /v1/usage/efficiency` and
   `omnigent usage --efficiency`: cache hit rate, output share, cost per
   session, model mix, per harness and per model, worst sessions first.

Plus the **software factory** example (`examples/factory`): a foreman that
subscribes to issue and CI events, triages with a cheap model, dispatches a
fixer into a fresh sandbox, demands a recording as proof, and hands the merged
change to a watcher that subscribes to the deploy.

## 3. Positioning for the world

The pitch, in one line: *the harness that measures itself and deletes its own
scaffolding.* Every other harness claims to make agents smarter. This one
claims to make them cheaper, safer, and more awake, and shows the numbers.

Three audiences, three entry points:

- **Builders of autonomous colleagues** want subscriptions and coordinator
  patterns. Entry: the factory example.
- **Platform teams paying the bill** want the efficiency scorecard and policies.
  Entry: `omnigent usage --efficiency` on their existing sessions.
- **Harness engineers** want the shim registry as a discipline. Entry:
  `docs/SHIMS.md`.

Keep the Omnigent name in attribution (Apache 2.0, keep `LICENSE` and `NOTICE`
intact, add our own line to `NOTICE`). Give the fork its own name only once it
has diverged enough that "Omnigent plus three features" undersells it. Until
then, present it as a downstream distribution and send the primitives upstream.

## 4. Repo mechanics

### Fork and remotes

```bash
cd omnigent
gh repo fork omnigent-ai/omnigent --remote=true --remote-name=origin
git remote rename origin upstream   # only if gh left upstream as "origin"
git remote -v                       # expect: origin = your fork, upstream = omnigent-ai
git push -u origin flywheel/harness-primitives
```

### Branch strategy

- `main` tracks `upstream/main`. Never commit directly to it.
- `flywheel/*` branches hold our primitives, one per primitive, rebased on
  `main` weekly (`git fetch upstream && git rebase upstream/main`).
- `dist` is the integration branch that merges every `flywheel/*` branch and is
  what we tag and share. Releases are `dist` tags: `v0.20.0-flywheel.1`.

### Upstream policy

Send each primitive upstream as its own PR once it has a week of our own use
behind it. Upstream requires the DCO sign-off (`git commit -s`), the PR
template with Summary, Test Plan, Demo, Type of change, Test coverage, and
Release notes, and `pre-commit run --all-files` clean. Features ship behind a
flag with a review-by release, which matches their `designs/FEATURE_FLAGS.md`
rules. If upstream declines, the fork keeps the feature and the rebase cost is
ours; that is the price of a fork and it is fine.

### CI to keep in the fork

Upstream runs over a hundred workflows, most of them repo-management bots. On
the fork, keep `ci.yml`, `lint.yml`, `web-tests.yml`, `server-compat.yml`,
`security-scan.yml`, and `docker-build.yml`. Disable the rest in the fork's
Actions settings rather than deleting the files, so rebases stay clean.

## 5. Roadmap

| Phase | Weeks | Deliverable | Done when |
| --- | --- | --- | --- |
| 0 | now | Branch `flywheel/harness-primitives` with the three primitives, docs, examples, tests | All new tests pass, `pre-commit` clean, this file and the design doc merged |
| 1 | 1 to 2 | Fork published, `dist` branch, first tag | A stranger can install from the fork and run `examples/factory` end to end |
| 2 | 2 to 4 | Subscriptions v2: fire a *new* session from an event (reuse `server/scheduled/fire.py`), Slack daemon forwards to `/v1/events/webhook`, GitHub App webhook registered by `docs/GITHUB_APP_SETUP.md` | Issue opened in a test repo produces a triage session with no human action |
| 3 | 4 to 6 | Efficiency in the web Usage page (`web/src/lib/usageApi.ts`), shims dashboard in the web UI, janitor automation running weekly on our own fork | Both pages exist behind flags and the janitor has opened at least one real PR |
| 4 | 6 to 8 | Harness bench extension: a "cost per solved task" probe so efficiency is part of the conformance matrix (`docs/harness-bench-design.md`) | Bench reports cost per task for claude-native and codex-native |
| 5 | 8 to 12 | Upstream PRs for each primitive, blog post with real numbers from our usage | At least one primitive merged upstream or explicitly declined |

## 6. What we deliberately do not do

- No new harness adapters. Upstream maintains nine; adding a tenth is their
  business, not our differentiation.
- No UI work in phase 0. The primitives are API and CLI first so they can be
  tested and upstreamed without the Playwright lanes.
- No rewriting of existing prompts. The talk's point is that model skill is
  absorbed; we add measurement and wakeups, not more instructions.

## 7. How to verify phase 0 yourself

```bash
cd omnigent
uv run --no-sync pytest tests/runtime/test_shims.py tests/server/test_shims_route.py \
  tests/server/events tests/server/test_subscriptions_routes.py \
  tests/runner/test_subscription_tool_dispatch.py tests/server/test_usage_efficiency.py \
  --reruns 0 -q
```

Then start a server with the flag on and poke the new endpoints:

```bash
uv run --no-sync omnigent server
curl -s localhost:8000/v1/shims | jq .
curl -s "localhost:8000/v1/usage/efficiency?days=30" | jq .totals
curl -s -X POST localhost:8000/v1/events/webhook \
  -H 'content-type: application/json' \
  -d '{"kind":"deploy.finished","fields":{"service":"api","status":"ok"}}'
```

The last call returns `{"matched":0,...}` until a session has subscribed. Run
`examples/factory` and ask the foreman to subscribe to `deploy.finished`, then
repeat the call and watch the session wake.
