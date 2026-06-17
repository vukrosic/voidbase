-- 0003_idea_lifecycle.sql — widen ideas.status to the real research lifecycle.
--
-- The original constraint (proposed/needs-run/running/done/rejected) was the
-- distributed-platform's minimal vocab. But the live single-operator loop runs a
-- richer pipeline in flat files (idea.md `status:`), and for Neon to be the
-- AUTHORITATIVE mirror of that loop it has to faithfully store every real state —
-- not collapse `needs-confirm` / `needs-recode` / `needs-taste` into `proposed`
-- and lose the signal the cockpit needs. The queue_items vocab stays narrow
-- (that table is the GPU job queue); only `ideas` (the backlog/record) widens.
--
-- Idempotent: drop-if-exists then add. Run via: python3 db/apply.py db/migrations/0003_idea_lifecycle.sql

alter table ideas drop constraint if exists ideas_status_check;

alter table ideas add constraint ideas_status_check check (
    status in (
        -- platform-canonical
        'proposed', 'needs-run', 'running', 'done', 'rejected',
        -- live-loop pipeline states (mirrored from autoresearch/ideas/*/idea.md)
        'draft', 'needs-plan', 'planning', 'needs-implement',
        'needs-recode', 'needs-taste', 'tasting', 'needs-review',
        'needs-confirm', 'superseded'
    )
);
