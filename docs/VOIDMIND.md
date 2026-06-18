# Voidmind — the token-donor / idea loop (idea capture)

> Status: **idea, not started.** Captured 2026-06-18. This is a standalone spoke,
> built after Voidrunner. It is written down here so the design isn't lost.

## One line

Voidmind is the **"donate AI tokens"** client: a standalone agent loop that reads
open research threads from voidbase, generates candidate ideas, and enqueues them
as runnable jobs — using the *donor's own* LLM API keys, never voidbase's.

It is the design/idea half of the platform; **Voidrunner** is the compute half.
Voidmind fills the queue, Voidrunner drains it.

```
Voidmind (tokens) ──ideas + queue_items──▶ voidbase ──jobs──▶ Voidrunner (compute)
```

## Why it's a separate spoke

- **Different resource donated.** A contributor with Claude/OpenAI credits but no
  GPU can still move the platform forward. Voidmind is how their tokens turn into
  research direction.
- **Different trust surface.** Voidmind only *proposes* (writes `ideas` +
  `queue_items`, both low-trust). It can never move the champion — that still goes
  through the confirm gate. So an open Voidmind is safe: worst case is junk ideas
  that never get claimed or that lose their paired comparison.
- **Different failure mode.** A bad runner wastes GPU; a bad idea-loop wastes
  queue space. Keeping them separate means each can be rate-limited / quality-gated
  independently.

## The plug boundary (HTTP only — same rule as Voidrunner)

Voidmind speaks only the voidbase API. It never touches the DB.

- `GET /threads/public?status=active&unclaimed=…` — read open research threads
  (already exists; built for exactly this in #7).
- `GET /threads/goal?name=…` — pull the full goal prompt for a thread (exists).
- `GET /runs`, `GET /comparisons` — read what's already been tried, so the loop
  doesn't re-propose a dead end.
- `POST /ideas` — write a candidate idea (**needs building**).
- `POST /queue_items` — enqueue the idea as a runnable job, carrying a
  self-contained `config` + `content_hash` (**needs building**; the config shape
  must match what Voidrunner/`run_experiment.py` consumes).

## The loop (sketch)

```
1. pull open threads (+ their goal prompt)
2. pull recent runs/comparisons for each thread  → "what's been tried"
3. ask the donor's LLM: given the goal + history, propose N next configs
4. dedup against existing ideas/queue (content_hash)
5. POST /ideas  +  POST /queue_items   (born low-trust, unclaimed)
6. sleep / repeat
```

## Open design questions (defer until Voidrunner ships)

- **Idea quality gate.** Open token-donation invites spam/low-value ideas. Options:
  maintainer-approves-before-enqueue, a cheap LLM-judge pre-filter, or
  per-contributor enqueue rate limits. Decide when it's real, not now.
- **Config schema ownership.** Voidmind must emit configs in the exact shape
  `run_experiment.py` consumes. That schema should be pinned/versioned once
  (shared by Voidrunner) so the idea-loop and the runner can't drift.
- **Auth.** Same bearer-token scheme as Voidrunner (see VOIDRUNNER plan). A
  Voidmind token grants `ideas`/`queue_items` write, nothing else.

## Relationship to existing code

The local design loop already does this against files/SQLite (`feeder.py`,
`generate-ideas` paths). Voidmind is that loop **re-pointed at the API** and
packaged so a stranger can run it on their own keys — exactly the same extraction
Voidrunner is to `worker.py`.
