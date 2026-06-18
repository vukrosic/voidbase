# Voidmind — the token-donor / idea loop

> Status: **BUILT** (2026-06-18), `voidmind/`. A **write-client spoke** parallel to
> `runner/`: runs on a donor's box, HTTP + bearer token, stdlib + `voidconfig`
> only, zero DB imports. The LLM call lives behind a swappable **proposer seam**
> (`voidmind/propose.py`) so the donor's keys stay theirs and the core carries no
> vendor SDK. Integration map: [SPOKES.md](SPOKES.md). Client usage:
> [`voidmind/README.md`](../../voidmind/README.md).
>
> **What shipped:** the package (`core` protocol + `propose` seam + `cli`), the two
> new API endpoints `POST /ideas` and `POST /queue_items` (bearer-token, born
> low-trust), and **`voidconfig`** — a new pure lib that owns the config-row shape
> and the dedup `content_hash`, imported by the API and re-exported by
> `scripts/feeder` so a Voidmind row lands in the EXACT same dedup space as an
> auto-fed one (this resolved the "config schema ownership" open question below).

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

## Design questions

- **Config schema ownership.** ✓ **RESOLVED** — the config-row shape and the
  dedup `content_hash` now live in the pure **`voidconfig`** lib, the single owner
  imported by the API's `POST /queue_items` (authoritative hash + validation) and
  re-exported by `scripts/feeder`. A test pins `voidconfig.content_hash` ==
  `feeder.content_hash` so the idea-loop and the auto-feeder can't drift.
- **Auth.** ✓ Same bearer-token scheme as Voidrunner. A Voidmind token grants
  `ideas`/`queue_items` write, nothing else; the localhost dev-bypass still works
  for the operator, and `VOIDBASE_REQUIRE_AUTH=1` closes it for public deploys.
- **Idea quality gate.** *(still open — decide when spam is real.)* Open
  token-donation invites low-value ideas. The trust floor already holds (a
  proposal is born unclaimed/unverified and can never move the champion, so the
  worst case is junk that loses its paired comparison). When volume warrants it,
  add one of: maintainer-approve-before-enqueue, a cheap LLM-judge pre-filter, or
  per-contributor enqueue rate limits. Not built yet — the gate belongs where the
  spam appears, and there's none to measure against today.

## Relationship to existing code

The local design loop already does this against files/SQLite (`feeder.py`,
`generate-ideas` paths). Voidmind is that loop **re-pointed at the API** and
packaged so a stranger can run it on their own keys — exactly the same extraction
Voidrunner is to `worker.py`.
