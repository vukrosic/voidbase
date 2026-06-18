-- 0009_contributor_token.sql — bearer-token auth for write endpoints (Voidrunner).
--
-- Until now the API was localhost / single-operator / no auth. Voidrunner is the
-- first WRITE client that runs on a machine the operator doesn't control (a
-- compute donor's box), so it needs an identity that isn't "whoever can reach
-- the DB". This adds the one column that makes a bearer token resolvable to a
-- contributor.
--
--   token_hash  sha256(token) hex digest. The plaintext token is shown to the
--               donor ONCE at /register and never stored — only this hash is, so
--               a DB leak can't impersonate a contributor. Nullable: existing
--               contributors (incl. the localhost 'automation' identity) have no
--               token and authenticate via the localhost dev-bypass instead.
--
-- Idempotent. Run via: python3 db/apply.py db/migrations/0009_contributor_token.sql

alter table contributors add column if not exists token_hash text;

-- A token must map to exactly one contributor. Partial unique index so the many
-- token-less rows (null) don't collide — only real tokens are constrained unique.
create unique index if not exists contributors_token_hash_key
    on contributors (token_hash)
    where token_hash is not null;
