-- 0006_box_health.sql — box heartbeat + health tracking for graceful recovery.
--
-- Today a GPU box that drops mid-run strands its job forever: nobody requeues
-- it and the loop silently starves. This adds the per-box health signal the
-- reaper sweep (scripts/reaper.py) uses to tell a live box from a dead one, and
-- that the cockpit shows at a glance:
--
--   last_heartbeat   when a worker last pinged for this box (POST box_heartbeat)
--   status           'healthy' (pinging) | 'offline' (reaper saw it go dark)
--                    | 'unknown' (never pinged yet)
--   failed_run_count how many runs the reaper has requeued off this box — the
--                    flap/quarantine signal.
--
-- All nullable / defaulted so existing box rows stay valid and nothing breaks.
-- Idempotent. Run via: python3 db/apply.py db/migrations/0006_box_health.sql

alter table boxes add column if not exists last_heartbeat   timestamptz;
alter table boxes add column if not exists status           text not null default 'unknown';
alter table boxes add column if not exists failed_run_count integer not null default 0;

-- Constrain status to the known vocab. Added separately (not inline) so a re-run
-- is a no-op: `add column if not exists` skips the column AND its inline check on
-- the second pass, so the constraint would never land if it lived on the column.
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'boxes_status_check') then
        alter table boxes add constraint boxes_status_check
            check (status in ('healthy', 'offline', 'unknown'));
    end if;
end $$;

-- The reaper scans boxes by heartbeat age every 60s; index it.
create index if not exists idx_boxes_heartbeat on boxes (last_heartbeat);
