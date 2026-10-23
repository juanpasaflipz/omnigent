# WakeVane: the harness that deletes itself

*Wake agents. Steer outcomes. Launch notes for WakeVane, a distribution of Omnigent.*

At AI Engineer 2026, Jediah Katz, who spent years on Cursor's agent harness
before moving to SpaceXAI, asked whether the agent harness is dead. His answer
was a careful yes and no. Recent studies found that for the newest models, the
choice of harness barely moves coding benchmark scores. The tricks a harness
used to supply, such as how to explore a codebase, how to keep going on a long
task, and how to check your work, have been absorbed by the models. He called
the loop that does this the data flywheel: the harness makes a hard task
reachable, people attempt it, the labs learn from it, the model learns the
skill natively, and the harness deletes the scaffolding.

What the model cannot absorb is everything around the model. Agents that run
for days need to run somewhere other than a laptop. Agents that send email or
push code need permissions and someone to ask. Two harnesses with the same
benchmark score can differ several times over in cost per task. And the
harness is what reaches for the next frontier, by turning a chat into a
long-lived colleague that wakes up when the world changes.

Omnigent already had most of that: hosts and managed sandboxes, a policy
engine with approvals and spend caps, coordinator and worker sessions across
nine harnesses. This distribution adds the three pieces it was missing.

## Wake on the world

Agents could already wake on a timer or when a sub-agent finished. Now they
can wake when something happens. One tool, `sys_subscribe`, takes a source,
an event kind, and a field filter:

```json
{"source": "github", "kind": "check_run.completed",
 "match": {"repo": "acme/api", "conclusion": "failure"}}
```

The server verifies GitHub's webhook signature, normalizes the event, and
posts a short notice into the subscribed session. A generic webhook endpoint
takes anything else: a deploy pipeline, a cron job, the Slack bot. New event
sources add a normalizer, not a new tool. That is the primitive the talk
described, built as one framework instead of a pile of integrations.

## Track the hacks you plan to delete

Every harness carries small deterministic patches over model gaps. They
usually live as comments, and nobody remembers to remove them when the next
model no longer needs them. Here each one is registered with a reason, the
model gap it covers, and a release by which it must be reviewed. Every firing
is counted per model. `GET /v1/shims` lists them with a `retirable` flag when
one has gone quiet, a test fails the build when a review release arrives, and
an example agent opens a pull request to delete each retirable shim.

## Measure cost, not just score

`omnigent usage --efficiency` reports cache hit rate, output share, cost per
session, and model mix, per harness and per model, with the worst sessions
first. A long session with a low cache hit rate usually means the prompt
prefix keeps changing. A coding agent with a high output share is usually
rewriting files instead of editing them. The harness bench gains a probe that
reports cost per solved task, so efficiency becomes part of each harness's
conformance matrix rather than a number someone finds on an invoice.

## Put it together

`examples/factory` is a software-factory foreman built from these pieces. It
subscribes to new issues and failing checks, triages each one on a small
model, sends real bugs to a sandboxed fixer that has to reproduce first and
show tests and a recording, asks a human before anything merges, and hands
the merge to a watcher that wakes when the deploy finishes. Nothing in it
polls.

## Try it

```bash
uv tool install --python 3.12 "git+https://github.com/juanpasaflipz/omnigent.git@dist"
omnigent server
omnigent run examples/factory
```

The design is in `designs/HARNESS_FLYWHEEL.md` and the maintenance plan is in
`FORK_PLAN.md`. Each primitive is headed upstream once it has run in real use.
Numbers from that use, cache hit rates and cost per solved task across
harnesses, will be the next post.
