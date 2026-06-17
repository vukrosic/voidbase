-- 0008_thread_tags.sql — persist research-thread tags.
--
-- The /research board already sends a `tags` array when authoring a thread, but
-- upsert_thread() writes a fixed column list and silently drops unknown keys, so
-- the tag filter on the board filters on nothing. This adds the backing column.
--
--   tags   jsonb array of free-text strings, e.g. ["attention","positional"].
--          The board derives its filter pills + 🔥-trending from these.
--
-- Defaulted + not-null so every existing thread reads back an empty array
-- instead of null (the UI can map() over it without a guard). Idempotent.
-- Run via: python3 db/apply.py db/migrations/0008_thread_tags.sql

alter table threads add column if not exists tags jsonb not null default '[]'::jsonb;
