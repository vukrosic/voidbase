-- 0005_thread_goal_prompt.sql
-- A research thread becomes self-executing: it carries the FULL prompt an AI
-- agent runs to do the research end-to-end, plus how results come back.
--
--   goal_prompt  the complete /goal brief — a contributor hands this straight to
--                their AI, which writes one experiment and opens a PR.
--   kind         'question' (open-ended direction) vs 'sweep' (enumerate configs).
--   submit_via   'pr' (default, GitHub is the gate, no auth) | 'neon' (direct,
--                authenticated API, born unverified — reserved for later).
--   repo_url     where the PR goes (defaults to the universe-lm repo).
--
-- All nullable / defaulted so existing rows stay valid and the API never breaks.

alter table threads add column if not exists goal_prompt text;
alter table threads add column if not exists kind        text not null default 'question';
alter table threads add column if not exists submit_via  text not null default 'pr';
alter table threads add column if not exists repo_url    text;
