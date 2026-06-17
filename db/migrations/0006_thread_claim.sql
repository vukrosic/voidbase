-- 0006_thread_claim.sql
-- A thread can be CLAIMED so two contributors (or two agents) don't pick the
-- same one and burn GPU-hours on duplicates. The claim is an async "queue of
-- attention" — soft, time-boxed, no auth (localhost single-operator model).
--
--   claimed_by        free-text handle of whoever is working it (null = open).
--   claimed_at        when the claim was taken.
--   claim_expires_at  now() + 48h at claim time. A claim past this is treated as
--                     unclaimed on read (lazy auto-release), so an abandoned
--                     claim never permanently parks a thread.
--
-- All nullable so existing rows stay valid and the API never breaks.

alter table threads add column if not exists claimed_by       text;
alter table threads add column if not exists claimed_at       timestamptz;
alter table threads add column if not exists claim_expires_at timestamptz;
