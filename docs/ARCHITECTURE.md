# voidbase architecture & consolidation plan

Captured 2026-06-17. This is the durable record of the decision to turn the
single-laptop research loop into a distributed, multi-contributor platform.

## The problem this solves

Three parallel coordination systems had grown up side by side:

- `autoresearch/` flat files + `closed.md` ledger (local loop state)
- `llm-research-kit-scaling/token2science/` (GitHub-issues/PRs protocol)
- `experiment-registry/` SQLite store (now **voidbase**)

That fragmentation is itself the mess. The plan: **collapse onto voidbase as the
single spine**, fold token2science's good epistemics into it as DB + API, and
retire the GitHub transport and eventually the `closed.md` file-ledger.

## Why these choices

- **Real DB over GitHub-as-backend.** GitHub's only real advantage was a free
  identity + verification surface for untrusted contributors. Everything else
  (speed, scale, query) the DB wins. We keep the good protocol, drop the
  minutes-long PR round-trips.
- **Supabase, not Neon or Convex.** Supabase = Postgres (the old `schema.sql`
  ports almost as-is) **+ Auth built-in + auto API + row-level security**. Neon
  is just Postgres (auth/API are your job). Convex isn't SQL (rewrite the schema,
  lose SQL leaderboard analytics, more lock-in). Supabase answers identity *and*
  storage in one.
- **voidspark stays the front-end.** Same Next.js UI; only its data layer swaps
  from local files/SQLite to `fetch(API)`.
- **No desktop app.** Devs/contributors get voidspark over the API (`npm i`).
- **Public, no reputation.** Ship public; reputation/abuse-handling is a later
  optimization. The non-deferrable piece is *result integrity*, not security.

## The trust gap that must NOT be deferred

Going multi-writer on heterogeneous GPUs makes seed-noise worse, not better.
This week's bugs — lucky-seed-42, the wrong-branch fake-NULLs, the confirm
control-arm bug — were all integrity failures. So the schema enforces:

- Every run is owned by a `contributor` + `box`, carries its `seed` and a
  `content_hash` of (commit + config + flags).
- `comparisons` has generated `same_seed`, `same_box`, `is_paired`. **Only
  `is_paired` deltas are signal.** Per-box baselines de-drift the screen.
- `confirmations` = reproduce-to-confirm. A run flips to `verification =
  'confirmed'` only after K independent agreeing reproductions.
- `champions` is append-only and **maintainer-only** (RLS). A public submission
  can never move the champion; promotion goes through the confirm gate
  (the existing `confirm_paired.py` paired 3-seed protocol).

## Staged rollout (de-risked order)

1. **API + managed Postgres** *(small, do first)*. Apply `migrations/` to a
   Supabase project; stand up the thin write-API (`api/`). Migrate the local
   loop to write through it. No outside contributors yet — this just
   de-fragments and proves the API against our own traffic.
2. **Trust layer** *(the real blocker, before any outsider)*. Enforce same-seed
   pairing (done in schema), add per-box baselines, wire reproduce-to-confirm.
3. **Compute-donor client** *(highest EV — kills GPU starvation)*. CLI / `npm i`:
   claim a `queue_item` → run `_arq_*.py` → push `runs` + `eval_points`.
4. **Token-donor + researcher modes.**
5. ~~Consumer app~~ — cut. voidspark-over-API covers devs for free.

The trap to avoid: building the shiny UI before the trust layer. Distributed
results without enforced pairing isn't a platform — it's a louder lucky-seed bug.

## Cutover from the old SQLite system

The old `registry/` (SQLite + `store.py` + Streamlit `dashboard.py`) is **still
live**: the running loop and `open-superintelligence-lab-github-io/scripts/
sync-lab-data.py` read `registry/experiments.sqlite`. Do not delete it before:

1. Supabase project stood up, migrations applied.
2. A one-time importer copies the current SQLite rows into Postgres.
3. The loop's writers (`run_job_queue.py`, the daemon's harvest) point at the API.
4. `sync-lab-data.py` reads from the API/Postgres instead of the local file.
5. voidspark's data layer switched to the API.

Only then remove `registry/`, `store.py`, `dashboard.py`, and retire
`token2science/` + the `closed.md` file-ledger.

## Open question for the operator (on return)

Near-term goal — **friends-only** (a few trusted boxes, shared secret, light
auth) or **fully public** (open sign-up)? Decision is "public" per 2026-06-17,
so RLS is wired seriously from day one (`0002_rls.sql`). Revisit only if the
goal narrows back to friends-only.
