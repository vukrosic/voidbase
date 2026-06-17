-- 0004_queue_config.sql — carry the config-row payload on the queue.
--
-- The scalable experiment model: an experiment is a JSON of overrides on top of
-- the champion, not a bespoke _arq_*.py file. The worker runs ONE generic
-- entrypoint (run_experiment.py) and feeds it this `config`. `content_hash` is
-- the dedup key — the hash of the resolved (env+fields) config, so "has anyone
-- tried this?" is a single indexed lookup instead of diffing opaque code.
--
-- Idempotent. Run via: python3 db/apply.py db/migrations/0004_queue_config.sql

alter table queue_items add column if not exists config jsonb;
alter table queue_items add column if not exists content_hash text;

create index if not exists queue_items_content_hash_idx on queue_items (content_hash);
create index if not exists queue_items_status_priority_idx
    on queue_items (status, priority desc, created_at);

-- runs already has content_hash; index it so dedup against past results is fast.
create index if not exists runs_content_hash_idx on runs (content_hash);
