-- voidbase 0002_rls
-- Row-level security = the trust layer, expressed as policy instead of GitHub CI.
--
-- Model (public platform, no reputation yet):
--   * Anyone (even anon) can READ everything — the registry is public.
--   * A signed-in contributor can WRITE rows they own (contributor_id = auth.uid()).
--   * A contributor can never overwrite someone else's results.
--   * The trusted edges (champions, decisions) are MAINTAINER-ONLY: a public
--     submission can never move the champion. That is the whole defense — public
--     writes land 'unverified'; only the confirm path (run by a maintainer)
--     promotes. Reputation/auto-trust is a later optimization on top of this.

-- Helper: is the caller a maintainer?
create or replace function is_maintainer()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from contributors
        where id = auth.uid() and role = 'maintainer'
    );
$$;

-- Enable RLS on every table.
alter table contributors  enable row level security;
alter table boxes         enable row level security;
alter table threads       enable row level security;
alter table ideas         enable row level security;
alter table queue_items   enable row level security;
alter table runs          enable row level security;
alter table eval_points   enable row level security;
alter table comparisons   enable row level security;
alter table confirmations enable row level security;
alter table champions     enable row level security;
alter table decisions     enable row level security;

-- Public read on everything.
create policy read_all on contributors  for select using (true);
create policy read_all on boxes         for select using (true);
create policy read_all on threads       for select using (true);
create policy read_all on ideas         for select using (true);
create policy read_all on queue_items   for select using (true);
create policy read_all on runs          for select using (true);
create policy read_all on eval_points   for select using (true);
create policy read_all on comparisons   for select using (true);
create policy read_all on confirmations for select using (true);
create policy read_all on champions     for select using (true);
create policy read_all on decisions     for select using (true);

-- contributors: you may create and edit only your own profile row.
create policy own_insert on contributors for insert with check (id = auth.uid());
create policy own_update on contributors for update using (id = auth.uid());

-- boxes: you own your boxes.
create policy own_insert on boxes for insert with check (contributor_id = auth.uid());
create policy own_update on boxes for update using (contributor_id = auth.uid());
create policy own_delete on boxes for delete using (contributor_id = auth.uid());

-- runs: a contributor inserts/updates only runs tied to their own id.
create policy own_insert on runs for insert with check (contributor_id = auth.uid());
create policy own_update on runs for update using (contributor_id = auth.uid());

-- eval_points / comparisons / confirmations: writable only when the parent run
-- belongs to the caller (or, for confirmations, the reproducing contributor).
create policy own_insert on eval_points for insert with check (
    exists (select 1 from runs r where r.id = run_id and r.contributor_id = auth.uid())
);
create policy own_insert on comparisons for insert with check (
    exists (select 1 from runs r where r.id = run_id and r.contributor_id = auth.uid())
);
create policy own_insert on confirmations for insert with check (
    reproduced_by = auth.uid()
);

-- queue_items: any contributor may claim work (update needs-run -> claimed) and
-- report progress; only maintainers create or delete queue items.
create policy maintainer_insert on queue_items for insert with check (is_maintainer());
create policy contributor_claim on queue_items for update using (auth.uid() is not null);
create policy maintainer_delete on queue_items for delete using (is_maintainer());

-- ideas: any contributor may propose; edit only your own.
create policy any_propose on ideas for insert with check (proposed_by = auth.uid());
create policy own_update  on ideas for update using (proposed_by = auth.uid() or is_maintainer());

-- threads: maintainer-managed.
create policy maintainer_write on threads for insert with check (is_maintainer());
create policy maintainer_edit  on threads for update using (is_maintainer());

-- TRUSTED EDGES — maintainer-only. This is what stops a public submission from
-- moving the champion. Promotion happens through the confirm path only.
create policy maintainer_only on champions for insert with check (is_maintainer());
create policy maintainer_only_upd on champions for update using (is_maintainer());
create policy maintainer_only on decisions for insert with check (is_maintainer());
