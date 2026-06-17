-- voidbase 0001_init
-- The central experiment registry, rewritten for Postgres/Supabase.
--
-- Design goals that the old SQLite schema (registry/schema.sql) could not meet:
--   1. Multi-writer: many contributors' machines write results concurrently.
--   2. Identity: every row is owned by a contributor (Supabase Auth) and a box.
--   3. Result integrity: a comparison's delta is only trustworthy when the
--      treatment and baseline share the SAME seed on the SAME box. The old
--      schema let seed/box drift leak into delta_val_loss (the exact source of
--      the lucky-seed and wrong-branch fake-NULL bugs).
--   4. Trust without reputation: public submissions land 'unverified'; the
--      champion only ever moves through the confirm gate (see champions + RLS).
--
-- Conventions: timestamptz everywhere, identity columns for surrogate keys,
-- text PKs kept where the loop already addresses rows by human name.

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

-- One row per human, 1:1 with Supabase auth.users. Created on first sign-in
-- (handle this in an auth trigger or the API; the FK lets RLS key off auth.uid()).
create table contributors (
    id              uuid primary key references auth.users (id) on delete cascade,
    handle          text unique,
    github_login    text,
    role            text not null default 'contributor'
                        check (role in ('contributor', 'maintainer')),
    -- contribution counters (donate-compute / donate-tokens accounting; future)
    compute_seconds bigint not null default 0,
    tokens_donated  bigint not null default 0,
    created_at      timestamptz not null default now()
);

-- A physical/virtual machine that runs experiments. Comparability is per-box:
-- deltas are only paired within one box. fingerprint = stable hardware hash so
-- the same box is recognized across sessions.
create table boxes (
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

-- A hypothesis / line of investigation.
create table threads (
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

-- Backlog of proposed levers (the idea queue the AI/token-donors fill).
create table ideas (
    id              text primary key,
    title           text not null,
    explanation     text,
    status          text not null default 'proposed'
                        check (status in ('proposed', 'needs-run', 'running', 'done', 'rejected')),
    proposed_by     uuid references contributors (id) on delete set null,
    notes           text,
    created_at      timestamptz not null default now()
);

-- The job queue. Compute-donors claim items via a lease (claimed_by_box +
-- lease_expires_at) so two boxes never run the same item.
create table queue_items (
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

-- A single training run. Two orthogonal state axes:
--   status       : lifecycle (queued -> running -> done/failed)
--   verification : trust    (unverified -> confirmed | rejected)
-- Public submissions are always born 'unverified'. content_hash = hash of
-- (git_commit + config + flags) so identical work is dedupable and a
-- reproduction can be matched back to the claim it reproduces.
create table runs (
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

-- Per-step learning curve.
create table eval_points (
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

-- A paired treatment-vs-baseline delta. The integrity fix lives here:
-- same_seed / same_box are GENERATED, and is_paired (both true) is the only
-- flag under which delta_val_loss may be treated as signal rather than noise.
-- The loop must read is_paired, not delta_val_loss alone.
create table comparisons (
    id                  bigint generated always as identity primary key,
    run_id              text not null references runs (id) on delete cascade,
    baseline_run_id     text references runs (id) on delete set null,
    baseline_name       text,
    seed                integer,
    baseline_seed       integer,
    box_id              uuid references boxes (id) on delete set null,
    baseline_box_id     uuid references boxes (id) on delete set null,
    -- NOTE: require non-null. `null is not distinct from null` is TRUE in SQL,
    -- so without the IS NOT NULL guard a legacy/untracked row (null seed, null
    -- box) would be falsely marked paired — the exact integrity leak we prevent.
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

-- Reproduce-to-confirm protocol (salvaged from token2science, minus GitHub).
-- An independent box re-runs a claimed run; 'agrees' if within the noise band.
-- A run flips verification='confirmed' after K independent agreeing rows
-- (enforced in the API/confirm path, not here).
create table confirmations (
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
-- Gated edges (maintainer-only via RLS)
-- ---------------------------------------------------------------------------

-- Append-only champion history. Promotion is THE trusted edge: a champion only
-- changes through the confirm gate, and RLS restricts inserts to maintainers.
-- The current champion of a scope = the row with superseded_at is null.
create table champions (
    id              bigint generated always as identity primary key,
    scope           text not null,            -- e.g. 'tiny1m3m'
    run_id          text not null references runs (id) on delete restrict,
    val_loss        double precision not null,
    promoted_by     uuid references contributors (id) on delete set null,
    promoted_at     timestamptz not null default now(),
    superseded_at   timestamptz,
    reason          text
);

-- Approval / verdict ledger.
create table decisions (
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

create index idx_ideas_status              on ideas (status);
create index idx_queue_items_thread_status on queue_items (thread_name, status);
create index idx_queue_items_claimable     on queue_items (status, priority desc) where status = 'needs-run';
create index idx_runs_thread_status        on runs (thread_name, status);
create index idx_runs_verification         on runs (verification);
create index idx_runs_content_hash         on runs (content_hash);
create index idx_eval_points_run_step      on eval_points (run_id, step);
create index idx_comparisons_run           on comparisons (run_id);
create index idx_comparisons_paired        on comparisons (is_paired) where is_paired;
create index idx_confirmations_run         on confirmations (run_id);
create index idx_champions_scope_current   on champions (scope) where superseded_at is null;
create index idx_decisions_thread          on decisions (thread_name);

-- Exactly one current champion per scope.
create unique index idx_champions_one_current on champions (scope) where superseded_at is null;
