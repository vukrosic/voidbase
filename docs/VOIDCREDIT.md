# Voidcredit — attribution & leaderboard (pure-library spoke)

> Status: **planned, recommended next.** Captured 2026-06-18. A pure policy
> library like [VOIDCHECK](../voidcheck/), imported by new read endpoints in
> `api/server.py`. Integration rationale: [SPOKES.md](SPOKES.md).

## One line

Voidcredit turns the rows the platform already has — `runs` (with
`contributor_id`/`box_id`), `confirmations`, `champions`, `ideas` — into **credit
and lineage**: a leaderboard, a per-contributor page, and the idea→run→champion
chain that shows whose work led to a confirmed win.

## Why a pure library (not a service)

It only **reads and derives**. So, exactly like Voidcheck, the *policy* — what
counts as credit, how to rank, how lineage is walked — is a set of pure functions
with property tests, and the API edge does the SQL and calls them. Two
consequences that matter:

- **Credit is derived on read, never stored.** No `credit` table to drift out of
  sync; the source of truth stays `runs`/`confirmations`/`champions`. (A
  materialized cache is a later *optimization*, not the design.)
- **The growth flywheel is swappable.** Re-weight or re-skin credit without
  touching the integrity core or the write path.

## The pure surface (`voidcredit/`, no I/O)

```python
# given rows the API gathered, shape them — no DB, no network
leaderboard(contributor_stats)      -> ranked rows         # who contributed most
contributor_card(handle, runs, confirmations)  -> profile  # one donor's story
lineage(run, ideas, queue_items, champions)    -> chain    # idea → run → champion
credit_events(run, confirmations, champions)   -> [event]  # "promoted champion X"
```

What "credit" counts (v0 policy, all derivable):
- **compute** — runs reported by a contributor's boxes (and their `box`/GPU time).
- **tokens** — ideas/queue_items proposed (the Voidmind side, once it exists).
- **impact** — runs that became `confirmed`, and runs a champion points at
  (the high-signal credit: *your* run is the current best).

## API integration (new read endpoints)

All read-only, public (credit is public by design), portable null-safe like the
existing endpoints:

| Endpoint | Returns |
|---|---|
| `GET /leaderboard` | contributors ranked by the credit policy |
| `GET /contributor?handle=` | one contributor's card: totals, recent runs, confirmed wins |
| `GET /lineage?run=` | the idea→run→champion chain for a run |

These do the aggregation SQL, pass rows to `voidcredit`, and return JSON —
mirroring how `confirm_daemon` passes rows to `voidcheck`. voidspark's existing
`/leaderboard` and `/contributor` pages switch their data source to these.

## Zero schema change

Everything is already on the tables (`runs.contributor_id`/`box_id` landed in #16,
`confirmations`, `champions`, `ideas.proposed_by`). v0 is pure read.

## Build phases

1. `voidcredit/` pure library + property tests (ranking is stable, lineage walks
   idea→queue→run→champion, credit_events fire only on confirmed/championed runs).
2. `/leaderboard`, `/contributor`, `/lineage` read endpoints in `api/server.py`.
3. Point voidspark's leaderboard/contributor pages at them.
4. (later) a materialized cache if read latency ever matters.

## Open questions (decide when real)

- **Credit weighting** — how to combine compute vs. tokens vs. impact into one
  rank. v0: keep them as separate columns; don't collapse to a single score until
  there's a reason to.
- **Sybil / gaming** — a public leaderboard invites fake contributors. Same
  deferral as the rest of the trust model: worst case is junk rows we ignore, not
  a corrupted champion. Rate-limit `register` when it matters.
