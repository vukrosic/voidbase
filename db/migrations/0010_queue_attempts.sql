-- 0010_queue_attempts.sql — bound infra-failure retries on a queue item.
--
-- worker.py re-queues a job (status -> needs-run) when a run fails on an SSH/box
-- DROP rather than a real training crash, so a transient blip doesn't poison the
-- search with a spurious `failed`. But an UNBOUNDED re-queue means a genuinely
-- dead box (accepts SSH, then drops every run) would cycle a job
-- needs-run -> claimed -> drop -> needs-run forever, burning the slot. This adds
-- the one counter that lets the worker give up after N infra failures and record
-- the job `failed` for real, so a dead box can't wedge the queue.
--
--   attempts  count of infra-failure re-queues for this item (NOT normal claims).
--             The worker increments it on each connection-drop re-queue and stops
--             re-queuing once it hits the cap (see MAX_INFRA_ATTEMPTS in worker.py).
--             Defaults 0; existing rows backfill to 0.
--
-- Idempotent. Run via: python3 db/apply.py db/migrations/0010_queue_attempts.sql

alter table queue_items add column if not exists attempts int not null default 0;
