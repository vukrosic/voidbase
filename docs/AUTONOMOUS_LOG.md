# Autonomous build loop — running log

A self-maintained ledger for the unattended 5-min build loop. Each entry: what
shipped, a self-critique, and the next concrete move. The newest entry is at the
top. The next loop iteration should read this first for continuity (git history +
this file are the only memory across fires).

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
