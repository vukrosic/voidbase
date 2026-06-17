# voidbase API — the write protocol

A thin server in front of Supabase. Reads can go straight to Supabase's
auto-generated REST (public, RLS-guarded). **Writes go through here** so the
*protocol* — pairing rules, content-hash dedup, the confirm gate — lives
server-side, not in every client. Implement as Supabase Edge Functions
(Deno/TS) or a small FastAPI service; either fronts the same Postgres.

Auth: clients send a Supabase JWT (human session) or a scoped API key (machine).
RLS does the row-ownership enforcement underneath; the API enforces the
*protocol* invariants RLS can't express.

## Endpoints (sketch — not yet implemented)

### Compute donor
- `POST /queue/claim` — atomically take the highest-priority `needs-run` item,
  set `status='claimed'`, `claimed_by_box`, `lease_expires_at`. Returns the
  command + arq stub ref. Lease so two boxes never run the same item.
- `POST /runs` — create a run (born `verification='unverified'`). Server stamps
  `content_hash` from (git_commit + config + flags) and rejects a duplicate hash
  from the same box.
- `POST /runs/:id/eval` — append `eval_points` (batched curve).
- `POST /runs/:id/finish` — set final losses, `status='done'`, `finished_at`.

### Confirm gate (the trusted edge)
- `POST /runs/:id/confirm` — an independent box reports a reproduction →
  `confirmations` row. Server computes `delta_from_original`, sets `agrees`
  against the noise band. When K independent agreeing confirmations exist,
  flips the run to `verification='confirmed'`.
- `POST /champions` — **maintainer only.** Promote a confirmed run: close the
  current champion (`superseded_at=now()`), insert the new one. Server refuses
  unless the run is `confirmed` and beats the current champion by > band on a
  paired (`is_paired`) comparison.

### Token donor / researcher
- `POST /ideas` — propose a lever (`proposed_by = caller`).
- `POST /queue` — maintainer promotes an idea to a queue item.

## Invariants the server enforces (that RLS cannot)

1. A `comparison` written for a verdict must be `is_paired` (same seed + box).
2. A champion promotion requires `verification='confirmed'` + paired margin.
3. `content_hash` dedup: identical (commit, config, flags) from one box is
   rejected — stops accidental and dishonest double-submits.
4. Queue claims are atomic + leased (no double-run).
