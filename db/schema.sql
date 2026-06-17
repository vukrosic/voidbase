-- voidbase canonical Postgres schema (host-agnostic: Neon / any Postgres 14+).
--
-- This is the production schema. It is the Supabase migration (supabase/
-- migrations/0001_init.sql) with the Supabase coupling removed:
--   * contributors.id is a plain generated uuid (no FK to auth.users).
--   * No row-level security here — auth is enforced at the voidbase API layer
--     (the API holds the DB connection and decides who may write). RLS can be
--     layered back on later if we adopt an in-DB auth provider.
--
-- The integrity model is unchanged and is the whole point: comparisons.is_paired
-- is GENERATED (same seed AND same box, both non-null) so only paired deltas are
-- trustworthy signal — never read delta_val_loss alone.

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

create table if not exists contributors (
    id              uuid primary key default gen_random_uuid(),
    handle          text unique,
    github_login    text,
    role            text not null default 'contributor'
                        check (role in ('contributor', 'maintainer')),
    compute_seconds bigint not null default 0,
    tokens_donated  bigint not null default 0,
    created_at      timestamptz not null default now()
);

create table if not exists boxes (
    id              uuid primary key default gen_random_uuid(),
    contributor_id  uuid not null references contributors (id) on delete cascade,
    label           text,
    gpu_class       text,
    fingerprint     text,
    created_at      timestamptz not null default now(),
    unique (contributor_id, fingerprint)
);

-- ---------------------------------------------------------------------------
-- Research structure
-- ---------------------------------------------------------------------------

create table if not exists threads (
    name            text primary key,
    hypothesis      text,
    status          text not null default 'active'
                        check (status in ('active', 'paused', 'closed')),
    priority        integer not null default 0,
    owner_id        uuid references contributors (id) on delete set null,
    notes_path      text,
    summary         text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists ideas (
    id              text primary key,
    title           text not null,
    explanation     text,
    status          text not null default 'proposed'
                        check (status in ('proposed', 'needs-run', 'running', 'done', 'rejected')),
    proposed_by     uuid references contributors (id) on delete set null,
    notes           text,
    created_at      timestamptz not null default now()
);

create table if not exists queue_items (
    id                  text primary key,
    thread_name         text not null references threads (name) on delete cascade,
    name                text not null,
    command             text not null,
    status              text not null
                            check (status in ('needs-run', 'claimed', 'running', 'done', 'failed', 'cancelled')),
    priority            integer not null default 0,
    gpu_class           text,
    claimed_by_box      uuid references boxes (id) on delete set null,
    claimed_at          timestamptz,
    lease_expires_at    timestamptz,
    created_at          timestamptz not null default now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    log_path            text,
    output_dir          text,
    decision            text,
    unique (thread_name, name)
);

-- ---------------------------------------------------------------------------
-- Results
-- ---------------------------------------------------------------------------

create table if not exists runs (
    id                  text primary key,
    queue_item_id       text references queue_items (id) on delete set null,
    thread_name         text not null references threads (name) on delete cascade,
    name                text not null,
    contributor_id      uuid references contributors (id) on delete set null,
    box_id              uuid references boxes (id) on delete set null,
    command             text,
    config              jsonb,
    content_hash        text,
    seed                integer,
    tokens_target       bigint,
    tokens_seen         bigint,
    actual_steps        integer,
    status              text not null
                            check (status in ('queued', 'running', 'done', 'failed')),
    verification        text not null default 'unverified'
                            check (verification in ('unverified', 'confirmed', 'rejected')),
    verdict             text,
    output_dir          text,
    metrics_path        text,
    checkpoint_path     text,
    final_val_loss      double precision,
    final_val_accuracy  double precision,
    final_train_loss    double precision,
    git_commit          text,
    git_branch          text,
    git_dirty           boolean,
    created_at          timestamptz not null default now(),
    finished_at         timestamptz
);

create table if not exists eval_points (
    id              bigint generated always as identity primary key,
    run_id          text not null references runs (id) on delete cascade,
    step            integer not null,
    tokens          bigint,
    val_loss        double precision,
    val_accuracy    double precision,
    val_perplexity  double precision,
    learning_rate   double precision,
    elapsed_seconds double precision,
    created_at      timestamptz not null default now()
);

-- The integrity fix: same_seed / same_box / is_paired are GENERATED and require
-- non-null, so a legacy/untracked row (null seed, null box) is never paired.
create table if not exists comparisons (
    id                  bigint generated always as identity primary key,
    run_id              text not null references runs (id) on delete cascade,
    baseline_run_id     text references runs (id) on delete set null,
    baseline_name       text,
    seed                integer,
    baseline_seed       integer,
    box_id              uuid references boxes (id) on delete set null,
    baseline_box_id     uuid references boxes (id) on delete set null,
    same_seed           boolean generated always as
                            (seed is not null
                             and seed is not distinct from baseline_seed) stored,
    same_box            boolean generated always as
                            (box_id is not null
                             and box_id is not distinct from baseline_box_id) stored,
    is_paired           boolean generated always as
                            (seed is not null and box_id is not null
                             and seed is not distinct from baseline_seed
                             and box_id is not distinct from baseline_box_id) stored,
    matched_step        integer,
    matched_tokens      bigint,
    baseline_val_loss   double precision,
    run_val_loss        double precision,
    delta_val_loss      double precision,
    n_seeds             integer,
    verdict             text,
    created_at          timestamptz not null default now()
);

create table if not exists confirmations (
    id                  bigint generated always as identity primary key,
    run_id              text not null references runs (id) on delete cascade,
    reproduced_by_box   uuid references boxes (id) on delete set null,
    reproduced_by       uuid references contributors (id) on delete set null,
    reproduced_val_loss double precision,
    delta_from_original double precision,
    agrees              boolean,
    notes               text,
    created_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Gated edges (maintainer-only — enforced by the API, not RLS)
-- ---------------------------------------------------------------------------

create table if not exists champions (
    id              bigint generated always as identity primary key,
    scope           text not null,
    run_id          text not null references runs (id) on delete restrict,
    val_loss        double precision not null,
    promoted_by     uuid references contributors (id) on delete set null,
    promoted_at     timestamptz not null default now(),
    superseded_at   timestamptz,
    reason          text
);

create table if not exists decisions (
    id              bigint generated always as identity primary key,
    thread_name     text references threads (name) on delete set null,
    run_id          text references runs (id) on delete set null,
    decision        text not null,
    reason          text,
    decided_by      uuid references contributors (id) on delete set null,
    decided_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

create index if not exists idx_ideas_status              on ideas (status);
create index if not exists idx_queue_items_thread_status on queue_items (thread_name, status);
create index if not exists idx_queue_items_claimable     on queue_items (status, priority desc) where status = 'needs-run';
create index if not exists idx_runs_thread_status        on runs (thread_name, status);
create index if not exists idx_runs_verification         on runs (verification);
create index if not exists idx_runs_content_hash         on runs (content_hash);
create index if not exists idx_eval_points_run_step      on eval_points (run_id, step);
create index if not exists idx_comparisons_run           on comparisons (run_id);
create index if not exists idx_comparisons_paired        on comparisons (is_paired) where is_paired;
create index if not exists idx_confirmations_run         on confirmations (run_id);
create index if not exists idx_decisions_thread          on decisions (thread_name);
create unique index if not exists idx_champions_one_current on champions (scope) where superseded_at is null;
