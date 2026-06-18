# Autonomous build loop — running log

A self-maintained ledger for the unattended 5-min build loop. Each entry: what
shipped, a self-critique, and the next concrete move. The newest entry is at the
top. The next loop iteration should read this first for continuity (git history +
this file are the only memory across fires).

---

## 2026-06-19 · Confirm gate on /voidbase + polling fix (voidspark)

**Shipped** — voidspark `f7f8713`, `82690cc`.
1. Surfaced the existing `<GateStatus>` on `/voidbase` directly under the
   champion lineage, so the dashboard reads top-to-bottom: champion → its
   history → what's challenging it right now. Verified live — it shows
   `use_canon_conv+use_cross_block_score_share` mid-confirm (4/6) at margin
   +0.0176, the first real >band candidate from stack mode.
2. Fixed a real bug found while testing: the page hung on "Loading…". The
   Neon API takes 10–30s/query but the page polled every 3s and toggled the
   spinner each poll, so requests piled up and Refresh never recovered. Unified
   the fetches into one `load({background})`, slowed the poll 3s→10s, added an
   in-flight guard so polls never overlap. Verified: button settles to
   "Refresh", page renders fully.

**Self-critique**
- *The 10–30s query latency is the deeper issue.* The poll fix treats the
  symptom; the cause is slow `/runs`, `/comparisons`, `/activity`, `/gate`
  queries against Neon (no caching, full-table reads, serial HTTP). The right
  fix is server-side: add a short TTL cache or a single composite `/dashboard`
  endpoint so one round-trip replaces five. **This is the next move.**
- *GateStatus + the page now poll on separate clocks* (20s vs 10s) hitting the
  same slow API independently. A composite endpoint would also de-duplicate this.
- *Didn't wire the per-run lineage (`/lineage?run=`)* — deferred again in favor
  of the gate pairing, which had higher narrative payoff. Still worth doing.

**Next moves (priority order)**
1. **Composite `/dashboard?scope=` endpoint** (server-side) — one query/round-trip
   returning health + champion + gate + recent runs, with a short in-process TTL
   cache. Kills the latency + the five-pollers problem at the source. High value.
2. **Split `api/server.py` (1196 lines)** — the god-file; do it alongside #1 since
   adding an endpoint touches the same file (extract a router first, then add).
3. **Per-run lineage breadcrumb** in the runs expand row (`/lineage?run=`).
4. **Voidmind idea engine** — the real research ceiling (search is flat).

---

## 2026-06-19 · Champion lineage timeline (voidspark)

**Shipped** — `873b3d3` in voidspark. New `<ChampionLineage>` component on
`/voidbase`: reads `/champions` through the existing proxy, renders the champion
promotion arc as a vertical timeline (val_loss per promotion, paired-delta badge,
CURRENT marker, click-to-expand full confirm record). Own component so the
512-line page doesn't bloat. Typecheck clean, tested through Chrome (render +
expand + zoom verified the green ▾ / red ▴ badges), zero console errors.

**Self-critique**
- *Baseline understates the gain.* `totalGain` anchors on the first record
  (6.2403), which was itself a lucky single seed later honestly re-pinned UP to
  6.2539. Measuring from the honest baseline gives 0.0819, not 0.0683. Defensible
  (conservative) but the "honest" anchor is arguably the re-pin. Low priority.
- *Fetch once, no poll.* The rest of the page polls every 3s; this fetches on
  mount only. Promotions are rare + manual, so a new champion won't appear until
  refresh. Acceptable, but could hang the fetch on the page's load cycle.
- *Red badge reads as regression at a glance.* The honest re-pin shows red ▴
  because the raw number rose. The reason text clarifies, but there's no schema
  field distinguishing "re-baseline" from "true regression" to style them apart.
- *Pre-existing, not mine:* the page's "Refresh" button sits on "Loading…"
  perpetually because the 3s `load()` poll keeps `loading` true. Out of scope for
  this change; flag for a later fix.

**Next moves (priority order)**
1. **Lineage DAG / per-run ancestry** — `/lineage?run=` exists but is unused in
   the UI. A run's ancestry (which champion it stacked on, which levers) would
   make the "stacked super-additively" story explorable, not just narrated.
2. **Split `api/server.py` (1196 lines)** — the god-file the standing rules warn
   against. Route modules behind a thin router, zero behavior change. Cheap
   hygiene win, directly serves the "no god files" constraint.
3. **Voidmind idea engine** — the real research bottleneck: the search is flat
   (best single lever +0.0136 = noise at the 0.01 band). An LLM proposer that
   reads lineage and emits *novel* mechanisms (not recombinations of the 4 flags)
   is the only thing that raises the ceiling. Biggest payoff, biggest effort.
4. **Gate-status panel on the dashboard** — `/gate?scope=` already returns the
   live confirm field (clears, near-miss, recent verdicts). Surfacing it next to
   the lineage closes the loop: "here's the champion, here's what's challenging
   it right now." (`use_canon_conv+use_cross_block_score_share` is mid-confirm at
   margin 0.0176 — a real >band candidate from stack mode.)

**System state observed this fire**
- API up (Neon): 89 runs, 167 ideas, 244 queue items, 14 comparisons, 6 decisions.
- Champion: `champ-05-323-mom0p90-lr2x` @ 6.172 (tiny1m3m).
- GPU box OFFLINE — last heartbeat 2026-06-18 13:34, 6 failed runs. Compute is
  down; software work doesn't need it, but training/sweeps are blocked until a
  box is back. Queue: 0 in flight, 0 needs-run, 175 done, 15 failed.
