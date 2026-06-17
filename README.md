# voidbase

The central experiment registry for the voidspark research platform — the
database + API that every contributor pulls from and pushes to.

> Renamed from `experiment-registry` / `autoresearch-db`. This folder is being
> **completely rewritten** from a single-laptop SQLite store into a multi-writer
> Postgres backend (Supabase) so that distributed contributors can take part.

## The shape

```
contributors' machines  →  voidbase API  →  Supabase (Postgres + Auth + RLS)
   (never touch the DB)       (the protocol)      (the data + trust policy)
            ▲
            └── voidspark (Next.js) is the front-end, reading the same API
```

Three ways to contribute, all the same client + API, just different permissions:

| Mode             | Client does                                  | Writes        |
| ---------------- | -------------------------------------------- | ------------- |
| Donate compute   | claims a `queue_item`, runs it, reports back | `runs`, `eval_points` |
| Donate AI tokens | runs the design/idea loop                    | `ideas`, `queue_items` |
| Do research      | browse, submit manually                      | `ideas`, `runs` |

## Why the rewrite (what the old SQLite schema could not do)

1. **Multi-writer.** SQLite is single-writer/local. Postgres lets many boxes
   write concurrently.
2. **Identity.** Every row is now owned by a `contributor` (Supabase Auth, via
   GitHub OAuth) and a `box`.
3. **Result integrity — the important one.** A comparison's delta is only
   trustworthy when treatment and baseline share the **same seed on the same
   box**. The old `comparisons` table did not enforce this, which is exactly how
   the lucky-seed-42 and wrong-branch fake-NULL bugs leaked in. The new
   `comparisons` table has generated `same_seed`, `same_box`, and `is_paired`
   columns — **read `is_paired`, never `delta_val_loss` alone.**
4. **Trust without reputation.** Public submissions are born `verification =
   'unverified'`. The champion only ever moves through the confirm path, and RLS
   restricts champion/decision writes to maintainers. No reputation system yet —
   that is a later optimization on top of this gate.

## Trust model (public, no reputation — by design)

- Anyone can sign in (GitHub OAuth) and submit. Public read on everything.
- A contributor can only write rows they own; they can never overwrite another
  contributor's results (enforced by RLS, `0002_rls.sql`).
- A raw submission lands in the **unverified pool**. It is visible and useful as
  a lead, but it does **not** move the champion.
- The champion changes only through the **confirm gate**: reproduce-to-confirm
  (`confirmations`) + paired 3-seed verdict, run by a maintainer. This is the
  same gate the local loop already uses (`confirm_paired.py`), kept as the one
  trusted edge.
- Security/abuse handling (rate limits, sybil, auto-trust) is deferred — worst
  case from skipping it is junk rows we ignore, not corrupted conclusions.

## Layout

```
voidbase/
├─ supabase/
│  ├─ config.toml              # local stack + GitHub OAuth
│  └─ migrations/
│     ├─ 0001_init.sql         # the schema (Postgres rewrite)
│     └─ 0002_rls.sql          # row-level security = the trust layer
├─ api/                        # the thin write-protocol server (see api/README.md)
├─ docs/
│  └─ ARCHITECTURE.md          # consolidation plan + cutover from the old system
└─ registry/                   # OLD SQLite system — still live, removed at cutover
```

## Confirm daemon

Confirming a screen WIN used to be a manual step — the bottleneck at 50+
experiments/week. `scripts/confirm_daemon.py` automates the *queueing* of
confirms (a human still promotes):

1. Polls Neon for `done`, still-`unverified` runs whose `final_val_loss` beats
   the **current champion** by more than the screen band (`0.02`).
2. For each fresh candidate it enqueues a **paired 3-seed confirm** — 3 candidate
   runs + 3 champion-baseline runs at matched seeds (42 / 123 / 7) — as
   `queue_items` the worker drains. It never double-enqueues.
3. When all 6 finish it computes the **paired delta** (candidate mean − champion
   mean), writes one `confirmations` row, and flips the run's `verification` to
   `confirmed` / `rejected`.

The baseline arm is rebuilt from the **current champion config** (champions table
→ its run's `config`), re-run fresh in the same batch — never the bare base and
never a stale log, the bug that used to over-credit promotions.

It does **not** promote. Promotion to champion stays a maintainer action — a
human in the loop even at scale. `--auto-promote` exists but is a no-op stub.

```bash
python3 scripts/confirm_daemon.py --once          # one poll cycle, then exit
python3 scripts/confirm_daemon.py --interval 60   # poll loop (default 60s)
python3 scripts/confirm_daemon.py --once --scope tiny1m3m --screen-band 0.02
```

Needs `DATABASE_URL` (Neon) configured — see `db/conn.py`.

## Status / cutover

The old `registry/` SQLite system is **still the live store** for the running
research loop (`sync-lab-data.py` reads `registry/experiments.sqlite`). It stays
until the Supabase backend is stood up and the loop is migrated to write through
the API. Do not delete `registry/` before the cutover — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the staged plan.

## Running locally (once Supabase CLI is installed)

```bash
supabase start          # local Postgres + Auth + Studio
supabase db reset       # applies migrations/ from scratch
# or against the hosted project:
supabase link --project-ref <ref>
supabase db push
```
