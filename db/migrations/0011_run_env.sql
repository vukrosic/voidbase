-- 0011_run_env.sql — capture the runtime fingerprint so a champion is re-runnable.
--
-- A confirmed champion is only a trustworthy result if someone else can RE-RUN it
-- and land on the same number. The runs table already carries the config, seed,
-- command, content_hash and the git_commit/git_branch/git_dirty columns — but the
-- library/CUDA/GPU stack the run trained on was never recorded, and that stack
-- moves numerics. This adds the one missing piece of the reproducibility bundle:
--
--   env   jsonb  the run's runtime fingerprint, best-effort, captured by the
--                client at report time. Shape (all optional):
--                  {"python": "3.11.9", "platform": "Linux-...-x86_64",
--                   "torch": "2.3.1", "cuda": "12.1", "gpu": "NVIDIA RTX 3060"}
--                Null on legacy rows and whenever the client couldn't probe it —
--                voidcheck.repro_bundle() reads a null env as "stack unknown" and
--                downgrades the bundle's `reproducible` verdict accordingly.
--
-- Nullable, no default: existing runs stay valid and simply read back env=null.
-- Idempotent. Run via: python3 db/apply.py db/migrations/0011_run_env.sql

alter table runs add column if not exists env jsonb;
