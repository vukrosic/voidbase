# Autonomous build loop — running log

A self-maintained ledger for the unattended 5-min build loop. Each entry: what
shipped, a self-critique, and the next concrete move. The newest entry is at the
top. The next loop iteration should read this first for continuity (git history +
this file are the only memory across fires).

---

## 2026-06-19 · Freshness badge + UNTRIED-flag discovery (search not exhausted) + paired sweep

**Two things this fire: a small UI ship, and a research correction.**

**Shipped (UI)** — voidspark `f6e7bc3`. Freshness badge on `/voidbase` surfacing
the `stale`/`age_s` the SWR cache already returns: green "live"/"Ns old" when
fresh, amber "Ns old · refreshing" when serving a stale snapshot mid-refresh.
Makes last fire's invisible caching honest. Tested in Chrome (showed "17s old ·
refreshing" amber), zero console errors.

**Research correction — the search is NOT exhausted.** The standing belief
(memory + logs) was that singles plateaued and the space is mined out. False:
`llm_config.py` defines **177 `use_*` flags; only 37 have ever been tried**.
Excluding optimizer/LR variants (rules: structural only) leaves **115 UNTRIED
structural mechanisms** — many literature-strong and obviously worth trying:
`use_swiglu_ffn`, `use_value_residual`, `use_qk_layernorm`, `use_parallel_block`,
`use_sub_ln`, `use_mla`, `use_sliding_window`, `use_short_conv`, `use_xpos`,
`use_softpick`, `use_nope`, etc. The "plateau" was a plateau of the 37 tried, not
of the space. This reframes the whole "search needs a better idea" conclusion:
there ARE untested ideas, sitting right in the config.

**In progress (GPU)** — launched a faithful paired sweep on the box (still up,
RTX 3060): baseline (champion) + 7 untried structural flags (champion + each),
seed 42, same session so within-session deltas are clean. All 7 dry-validated
(DRY_OK). Running unattended in tmux (`/root/sweep.log`, ~40 min for 8 runs);
results land one per run. NEXT fire should read `/root/sweep.log` for the deltas
— any candidate beating baseline by > 0.01 (the band) is a real new lead.

**Self-critique**
- *I asserted "search plateaued" for several fires without ever enumerating the
  untried space.* A 30-second `grep use_ | sort -u` vs the tried set would have
  shown 115 untried mechanisms days ago. I trusted the inherited conclusion instead
  of checking it — the exact failure the outcome-aware proposer was meant to fix,
  committed by me at the meta level. Lesson: re-derive "we're stuck" claims from
  data before accepting them.
- *Same drift caveat as last fire:* these are hand-built configs (champion base +
  one flag), so absolute numbers won't match the registry and I'm NOT writing them
  to Neon. Only the within-session baseline-vs-candidate deltas are trustworthy.
  The proper path is still the worker/queue pipeline (logged below).
- *The sweep script captures each run's full output and only logs the final*, so I
  can't watch step-level progress — a `tee` to a per-run file would let me catch a
  diverging run early instead of waiting ~5 min per result. Minor.
- *7 flags is a thin slice of 115.* Picked by literature priors, not systematically.
  A real campaign would batch many more (the box is free), but I bounded it to keep
  the fire reviewable and the GPU spend sane.

**Next moves (priority order)**
1. **Read `/root/sweep.log`** and record the 7 deltas; promote any >band lead into
   a proper registry confirm (worker pipeline).
2. **Wire the untried-flag sweep into the real loop** — `feeder.py` already enqueues
   untried `use_*` singles (deduped); point it at the 115 and let `worker.py` drain
   them so results are registry-faithful, not hand-run. THIS is how to mine the
   space properly.
3. **Run the frontier candidate through the real pipeline** (last fire's #1, still
   valid) to settle its sub-band result registry-cleanly.
4. **Revive the box heartbeat**; **psycopg_pool**.

---

## 2026-06-19 · GPU box revived → REAL paired research run (frontier candidate likely noise)

**First compute fire in days — and it produced a real research finding, not code.**

**The box is ALIVE.** The heartbeat had been stale ~9h so the dashboard showed it
offline, but the box itself (RTX 3060 12GB, `1.208.108.242:52646`, via the
`connect-to-gpu` skill) is up and research-capable — only the heartbeat *loop*
died, not the box. Verified: repo present, dataset `pretrain_1B` present, GPU idle.

**Ran real experiments** (`run_experiment.py`, `Tiny1M3MAlibiConfig`, seed 42):
1. First attempt built on a BARE config → 6.2556. Discovered the box has NO
   `autoresearch/champion.json`, so `run_experiment.py` had no champion base to
   merge onto — it trained bare-alibi + the 2 flags, ≈ early-champion level. A real
   config-drift bug: the champion config lives only in Neon, not on the box.
2. Fetched the true champion config from Neon (`muon_lr 0.048, muon_momentum 0.9,
   use_poly_alibi, use_deepnet_alpha` + alibi env) and ran a FAITHFUL PAIRED run,
   same seed/box, back-to-back:
     - **Baseline (champion):              6.1778**  (registry champion 6.172 — reproduced within noise ✓)
     - **Candidate (champion+canon_conv+cross_block_score_share): 6.1734**
     - **Paired delta = +0.0044** — INSIDE the 0.01 screen band.

**The finding:** the registry's leading "frontier" candidate, which showed +0.0176
and read as the search's first real >band winner, gives only **+0.0044 on a
faithful paired seed — i.e. sub-band noise.** The +0.0176 was an UNPAIRED
comparison (the candidate's lucky single seed 6.1544 vs the champion's recorded
6.172). The paired baseline+candidate in one session collapses it to +0.0044. This
is exactly the paired-vs-unpaired trust gap voidbase is built to catch — and it
says this candidate would likely NOT survive a real confirm. The gate's skepticism
was right; the search is still genuinely plateaued.

**Honest caveats (do not over-claim):**
- *One seed, not a 3-seed confirm.* +0.0044 is suggestive, not a verdict.
- *Reconstruction drift:* my candidate seed-42 (6.1734) differs from the registry's
  recorded cand-s42 (6.1544) by 0.019 — so my rebuild of the candidate config is
  NOT a byte-perfect replica of the registry's confirm runs (torch/cuda/env drift,
  or those runs carried extra fields). Therefore absolute numbers are NOT
  comparable to the registry; only THIS session's own baseline-vs-candidate delta
  (+0.0044, identical conditions) is trustworthy. That delta is the real signal.
- *Not injected into the registry.* These runs bypassed the worker/confirm
  pipeline and carry the caveat above, so writing them to Neon would pollute it.
  Kept as an out-of-band finding.

**Self-critique**
- *I rebuilt the champion config by hand from the runs row instead of using the
  real pipeline.* The 0.019 same-seed gap is the price — a faithful experiment must
  come from the SAME config artifact the registry used, not a hand-reassembly. The
  right fix is to run through `worker.py` (claim a real queue job whose `config`
  carries the exact champion base), not `run_experiment.py` with a reconstructed
  EXPERIMENT_CONFIG.
- *The box has no champion.json* — `sync_champions.py` exists; it should be run on
  the box (or the worker should pull the champion) so `run_experiment.py` is
  faithful by default. This is the config-drift root cause.
- *I didn't revive the heartbeat* (needs the worker + the box reaching the Mac API
  over a reverse tunnel). So the dashboard still shows the box offline even though
  it's up — a real gap between displayed and actual state.
- *Burned ~20 min of GPU on three tiny runs.* Cheap (cents) and authorized, but the
  first (bare) run was wasted on the champion.json bug I could have checked first.

**Next moves (priority order)**
1. **Run the candidate through the REAL pipeline** — `sync_champions.py` on the box
   to write champion.json, then `worker.py once` against a properly-queued confirm
   job, so the config is the registry's exact artifact (kills the 0.019 drift).
   Only then is a paired delta registry-trustworthy.
2. **Revive the box heartbeat** so the dashboard reflects reality (worker loop +
   reverse tunnel from box → Mac API).
3. **Freshness badge on /voidbase** — surface `stale`/`age_s` (small UI win).
4. **Status filter on the Idea backlog**; **psycopg_pool** (root-cause DB fix).

**System state:** box UP (RTX 3060, idle, both research tmux sessions exited
clean). Queue empty (0 needs-run). Champion 6.172 stands; no real challenger after
the frontier candidate collapsed to sub-band on paired test.

---

## 2026-06-19 · /dashboard stale-while-revalidate + startup warm-up

**Shipped** — voidbase `62dba4e`. Killed the cold-MISS hang that bit twice (the
BrokenPipe two fires ago, the "Loading…" wait last fire). The composite query
still takes ~10-13s against Neon, but now it almost never blocks a request:
- **< 12s (FRESH):** serve cache as-is.
- **12-90s (STALE):** serve the cached snapshot INSTANTLY + spawn exactly ONE
  background refresh (`_DASH_REFRESHING` guard → N stale reads = 1 recompute).
- **> 90s / cold:** block & recompute inline (the only path that ever waits).
Since the page polls every 10s, once warm the cache always lands in FRESH..STALE
and never blocks again. Added a startup `warm_dashboard()` (off-thread) so even the
FIRST request after a restart is cache-served. Payload carries `cached/stale/age_s`.

**Tested** — deterministic isolated-scope run: COLD 6.5s (blocks once) →
FRESH 2ms → after 14s a STALE read returns 2ms with `stale:true` + spawns a
refresh → 13s later age dropped 14.1s→8.9s (bg refresh republished, nothing
blocked). Startup warm-up: first `/dashboard` post-restart served in 2.7ms. Also
watched the live tiny1m3m scope stay warm purely from the open Chrome tab's 10s
poll. Suite 79 green; Chrome renders fast, zero console errors.

**Self-critique**
- *Background refreshes still take `_pg_lock`.* The HTTP response no longer waits,
  but the refresh thread holds the single Neon connection for ~10-13s, so a
  concurrent live write (heartbeat, run report) queues behind it. Tolerable on a
  single-operator localhost; a real fix is a connection pool (the `_pg_lock`
  comment already flags psycopg_pool). The whole stale-cache design is a patch
  over that one-connection bottleneck, not a cure.
- *`_DASH_STALE_TTL_S = 90s` is a guess.* If a tab sits idle >90s the next click
  pays the full cold wait again. Could raise it (serve arbitrarily stale + always
  refresh) but then a long-idle tab shows very old data for one frame. 90s ≈ 9
  poll cycles felt safe; unvalidated against real idle patterns.
- *The client ignores `stale`/`age_s`.* I plumbed them through but voidspark
  doesn't surface "showing data from 14s ago" — a tiny freshness badge would make
  the staleness honest to the operator. Left for a UI fire.
- *Startup warm-up races the first request:* if a user hits `/dashboard` in the
  ~10s before warm-up lands, they still block (the cold path), and BOTH may compute
  (warm-up holds the flag, so the user's request would actually find it refreshing
  and... no — a cold request with no cache entry ignores the flag and computes
  inline, so a true cold race double-computes once). Harmless (idempotent) but not
  free. Acceptable for a one-time boot window.

**Next moves (priority order)**
1. **Freshness badge on /voidbase** — surface `stale`/`age_s` (critique #3); tiny,
   makes the new caching visible + honest. Good Chrome-testable UI fire.
2. **Run the outcome-aware Voidmind proposer for real** — needs an LLM key.
3. **Status filter on the Idea backlog** — cheap, high-utility.
4. **Connection pool (psycopg_pool)** — the real fix for the `_pg_lock`
   serialization that all the caching works around (critique #1). Bigger lift.
5. **Get a GPU box back** — `.env` coords empty; all research payoff is blocked.

---

## 2026-06-19 · Per-run lineage breadcrumb (deferred 5 fires — done)

**Shipped** — voidspark `3255b53`. The `/lineage?run=` chain (thread → queue_item
→ run → champion, voidcredit-derived) existed but rendered nowhere — deferred
behind higher-narrative work for five straight fires. Now every run row on
`/voidbase` is expandable (was: only `has_eval` rows); expanding fetches the
lineage for ANY run plus the learning curve when present, each cached. New
`<LineageBreadcrumb>` renders the chain as a horizontal breadcrumb with per-kind
icons; the champion link is gold (trophy).

**Tested** — Chrome: a normal run (`use_canon_conv+use_av_output_carry`) shows
thread → queued-lever → run; the confirmed champion (`323-mom0p90-lr2x`) shows
thread → run → CURRENT CHAMPION (gold trophy). Zero console errors, typecheck clean.

**Found a latent bug (didn't ship a fix — the client is already correct):** run
ids contain `+`. A raw query string (`/lineage?run=...+...`) is decoded by the
server's `parse_qs` as `+`→space, so the lookup 404s — I hit this with curl. The
voidspark proxy uses `encodeURIComponent` (`+`→`%2B`), so the React client is
safe, which the live test confirmed (the `+`-containing run resolved correctly).
The server behavior is spec-correct (form-encoding), so the fix belongs at the
caller, not the server. *But:* any non-encoding consumer (a manual curl, a naive
script hitting `/lineage` or `/eval`) silently 404s on `+`/space ids. Worth a
one-line note in the API docstring; logged below.

**Self-critique**
- *The breadcrumb is read-only — no navigation.* Clicking the `champion` node could
  jump to that champion in the lineage timeline, or the thread node could filter
  the runs table. Right now it's informational only. Fine as a first cut.
- *Two fetches per expand (lineage + eval) and they serialize at the DB layer*
  (one Neon connection under `_pg_lock`). For a run with eval that's two ~0.3–1s
  round-trips back-to-back. Acceptable (only on explicit expand, both cached), but
  a combined `/lineage` that also returns eval would halve it. Low priority.
- *I made EVERY row expandable, including the `confirm-*` paired-seed rows* whose
  lineage is shallow (often just thread → run). Not wrong, but those 16 rows add
  little; they could collapse into their parent candidate. A later grouping pass.
- *No unit test* (consistent with the repo's testless voidspark components), so the
  breadcrumb's correctness rests on the manual Chrome pass alone.

**Next moves (priority order)**
1. **One-line API docstring note** on the `+`/space query-encoding gotcha for
   `/lineage` + `/eval` (cheap, prevents the next curl-debugging detour).
2. **Run the outcome-aware Voidmind proposer for real** — needs an LLM key; would
   replace the "(fed from Neon queue)" idea placeholders with real proposals.
3. **Status filter on the Idea backlog** — cheap, high-utility once volume is browsed.
4. **Background-refresh /dashboard** — hides the cold-MISS latency (the BrokenPipe
   from last fire + the "Loading…" wait this fire both came from it).
5. **Get a GPU box back** — `.env` box coords are now EMPTY (box torn down), so
   compute is fully blocked; every research payoff above waits on this.

---

## 2026-06-19 · Idea backlog panel — surface the proposal stream

**Shipped** — voidbase `4b8c9d9`, voidspark `1e0244f`. The `/ideas` backlog (167
candidate experiments, fed by Voidmind + manual) rendered nowhere, so "what is the
search considering next" was invisible — the exact follow-up the last fire logged
after building the outcome-aware proposer. Now:
1. `/dashboard` folds in the recent-24 idea slice (one cached round-trip, no extra
   client call). Ideas aren't scope-keyed → a global recent slice; full backlog
   stays at `/ideas`.
2. New `<IdeasBacklog>` on `/voidbase`, placed between the champion/gate (proven
   story) and the operational views. Status-tagged list + a done/active/dead
   distribution header + click-to-expand explanations. Self-contained like
   ChampionLineage (optional `data` prop; self-fetches standalone). Status vocab
   (12 distinct values) buckets into 3 colors so it stays scannable.

**Tested** — Chrome: heading + 24 badged items render; header shows
9 done/12 active/3 dead; click-to-expand revealed the full proposal text on
"223 — Per-Block Learnable RoPE Base"; zero console errors. Typecheck clean.
Backend: MISS populates 24 ideas, HIT serves in ~2ms.

**Self-critique**
- *The panel is a recent-24 window with no filter/search.* With 167 ideas and a
  12-value status vocab, an operator can't yet ask "show me only needs-taste" or
  page back. Fine at this size; a status filter is the obvious next iteration.
- *Many ideas have explanation "(fed from Neon queue)"* — a placeholder, not a real
  proposal. I correctly suppress the expand affordance for those (no ▸), but it
  means the BACKLOG's information value is uneven: the manually-authored ideas are
  rich, the auto-fed ones are just titles. The outcome-aware Voidmind proposer
  (last fire) will fix this at the source once it runs — its proposals carry a real
  `explanation` (the landscape signal that motivated them).
- *Restart hit a BrokenPipe on the cold MISS:* my 20s curl timed out while the
  first uncached composite query ran (>20s cold), and the server logged a broken
  pipe writing back to the dead socket. Harmless (the payload still cached, next
  call was 2ms), but it confirms the cold-MISS latency is real and user-visible on
  a fresh boot. The logged "background-refresh /dashboard" move (serve stale while
  revalidating) would hide it.
- *No automated test for the new panel.* Consistent with the other voidspark
  components (none have tests; the repo has no component test harness). The
  backend addition is covered by the existing /dashboard shape only implicitly.

**Next moves (priority order)**
1. **Run the outcome-aware Voidmind proposer for real** — needs an LLM key + the
   champion base config; would replace the "(fed from Neon queue)" placeholders
   with landscape-motivated proposals. Gated on a key being available to the loop.
2. **Status filter on the Idea backlog** (critique #1) — cheap, high-utility once
   volume is browsed.
3. **Per-run lineage breadcrumb** in the runs expand row (`/lineage?run=`) — the
   long-deferred cheap UI win.
4. **Background-refresh /dashboard** (serve stale while revalidating) — hides the
   cold-MISS latency that the BrokenPipe exposed.
5. **Get a GPU box back** — the research payoff of everything above is still
   compute-blocked (box offline since 2026-06-18 13:34).

---

## 2026-06-19 · Voidmind outcome-aware proposer (the ceiling-raiser)

**Shipped** — voidbase `fb1ad6f`. The idea engine was proposing BLIND: its
`build_context` fed the donor's LLM only the goal + a flat list of lever *names*
tried — never which won, lost, or by how much. That's the flat-search ceiling in
software form (random recombination of the same flags). Fixed by assembling the
**outcome signal** from data already in the API and rendering it in the prompt:
- `champion` (GET /gate) — the val_loss every margin is measured against.
- `lineage` (GET /champions) — the confirmed promotion arc + mechanism `reason`s;
  the compounding story to EXTEND.
- `frontier` — gate-cleared candidates already past the band, to build on.
- `contenders` — best runs ranked by val_loss + signed margins (`rank_contenders`,
  pure/unit-tested); the near-misses worth COMBINING.
- `verdicts` — recent confirmed/rejected paired outcomes (proven / dead).
The system prompt now tells the LLM to extend the arc / combine the strongest
contenders / open a new direction when near-misses plateau, and never re-propose a
rejected lever.

**Tested** — `rank_contenders` + enriched `build_context` + landscape prompt are
unit-tested with a fake transport (+6 tests; file 19, suite 79, all green; the
original 13 unchanged since the new context keys are additive). Then exercised
against the LIVE registry (champion 6.172): the rendered prompt correctly shows
the 6-step arc, the real frontier `canon_conv+cross_block_score_share` (+0.0176),
the ranked contenders with margins, and the rejected `gmlp_sgu`.

**Self-critique**
- *No UI surface, so no Chrome test this fire.* This is backend/CLI; the rigorous
  equivalent (live `build_context` → prompt render on real data) was done. A real
  follow-up: surface the *proposals* on voidspark so a human can watch the idea
  engine reason — that WOULD be Chrome-testable and closes the research loop
  visually.
- *Live testing caught a real defect:* the tried-levers list was bloated with 16
  `confirm-*` paired-seed machinery rows — fixed (filtered). Exactly why testing
  on real data, not just fakes, matters. But it also means the fake-transport
  tests alone would NOT have caught it; I should add a fixture with confirm-* rows
  to lock the filter in. (Did add the filter test to `rank_contenders`; the
  `tried_levers` filter is only covered live — a gap.)
- *The proposer is still only as good as the donor's LLM + the flags it knows.*
  The landscape tells it WHAT compounded, but the config schema (`fields`) is the
  vocabulary ceiling — it can only propose flags `run_experiment.py` understands.
  A genuinely novel *mechanism* (new code path, not a flag toggle) still needs a
  human or a code-writing agent. This raises the recombination ceiling, not the
  invention one.
- *Unverifiable end-to-end right now:* the GPU box is offline, so even a brilliant
  proposal can't be run to confirm the landscape-reasoning actually finds a >band
  winner. The logic is tested; the *research payoff* is blocked on compute.

**Next moves (priority order)**
1. **Surface proposals/ideas on voidspark** — the idea engine now reasons well but
   is invisible. A panel showing recent proposals (lever + the landscape signal
   that motivated them) makes the research loop watchable AND Chrome-testable.
2. **Lock the `confirm-*` tried-levers filter in a unit test** (critique #2 gap).
3. **Per-run lineage breadcrumb** in the runs expand row (`/lineage?run=`) — the
   five-fires-deferred cheap UI win.
4. **Get a GPU box back** so the proposer's output can actually be confirmed —
   the research payoff is compute-blocked (box offline since 2026-06-18 13:34).

---

## 2026-06-19 · Split api/server.py god-file (4 modules)

**Shipped** — voidbase `e4acd44`. The 1252-line `api/server.py` (DB plumbing +
every read + every write + auth + the HTTP dispatcher in one file) is now four
modules by responsibility, zero behavior change:
- `api/backend.py` (132) — backend-agnostic helpers (rows/_pg_rows/_pg_exec) +
  resolved config (PG_URL, BACKEND, REQUIRE_AUTH, LEASE_SECONDS).
- `api/reads.py` (502) — every GET builder + the composite /dashboard cache.
- `api/writes.py` (476) — every mutating endpoint + bearer-token auth.
- `api/server.py` (227) — now ONLY the Handler, ROUTES, and main().

Sibling imports work because `python3 api/server.py` puts `api/` on sys.path[0];
each module self-inserts the repo root for db.conn/voidcheck/voidcredit/voidconfig
so it stays independently importable (tests, REPL).

**Tested** — server boots on neon; `/health`, `/gate` (champion 6.172, 1 clears,
gate live), `/dashboard` (MISS 5.7s → HIT 2.3ms, cache survived the split), and
POST dispatch (register→400, unknown-resource→404 writable list) all return
identical shapes. Drove Chrome to `/voidbase`: full render (lineage −0.0683 over
6 promotions, gate live with 1 candidate confirming, 89 runs, 14 comparisons),
**zero console errors**.

**Self-critique**
- *The split is pure hygiene, not capability.* It buys readability + a place to
  add endpoints without growing one file — real, but it doesn't move the search
  or the latency. The honest priority remains the idea engine (next).
- *Each module re-does `sys.path.insert(0, repo_root)`* (3 copies). Defensible —
  it keeps every module importable standalone — but it's duplicated boot logic.
  A tiny `api/_bootstrap.py` could centralize it; not worth a file yet.
- *No automated test imports these modules*, so the "byte-identical contract"
  claim rests on the manual curl + Chrome pass, not a regression guard. A cheap
  win: a smoke test that imports server and asserts ROUTES + the GET handler map.
  The existing api tests are HTTP-integration (skip when no server) — they'd have
  caught a contract break only if run against the live box.
- *backend.py imports psycopg lazily inside the helpers* (unchanged from before).
  Good for the SQLite-only path, but means a missing psycopg surfaces per-request,
  not at boot. Pre-existing; left as-is to keep the refactor behavior-preserving.

**Next moves (priority order)**
1. **Voidmind idea engine** — the real research ceiling. The search is flat (best
   single lever +0.0136 = noise at the 0.01 band); stack mode found ONE real
   >band combo (canon_conv+cross_block_score_share, +0.0176, mid-confirm). An LLM
   proposer that reads lineage and emits *novel* mechanisms is the only lever that
   raises the ceiling. Biggest payoff. Now unblocked by the clean writes module.
2. **Per-run lineage breadcrumb** in the runs expand row (`/lineage?run=`) —
   deferred four fires running; still the cheapest UI win.
3. **Smoke test for the api split** — import server, assert the route maps, so a
   future edit can't silently drop an endpoint (see critique #3).
4. **Background-refresh /dashboard** — serve stale while revalidating so even a
   MISS (still 5–13s) is hidden from the client.

---

## 2026-06-19 · Composite /dashboard endpoint + client migration

**Shipped** — voidbase `3919168`, voidspark `8c65cc4`.
1. `GET /dashboard?scope=` (api/server.py): composes health + champions + gate
   + runs + comparisons + activity into ONE payload, memoized behind a 12s TTL.
   The backend shares one Neon connection behind `_pg_lock`, so six separate
   queries serialize and pollers pile up — this collapses them and a cache hit
   never takes the lock. Verified: MISS 8.2s, HIT 1.9ms (~4000×).
2. Migrated the /voidbase page to call `/dashboard` once and feed champions +
   gate to the components as props (both kept an optional `data` prop; they
   self-fetch when standalone, e.g. GateStatus on /research). Verified through
   Chrome: **one Refresh = 1 POST, down from 6**; zero console errors clean.

**Self-critique**
- *Caught my own cache bug in testing:* v1 stamped the TTL with the timestamp
  from the START of the request, so a 13s query published an already-expired
  entry — it never cache-hit. Fixed by stamping after the work completes. Lesson:
  for slow producers, the TTL clock must start when the value is READY.
- *Two React warnings appeared* ("useEffect dep array changed size") — confirmed
  Fast-Refresh false positives from live-editing the dep arrays (Previous len 1 →
  Incoming len 3); a clean mount shows none. Not shipped breakage, but noted.
- *The cache is per-process and unbounded* (one entry per scope — currently 1).
  Fine now; if scopes proliferate it should get a max-size/LRU. Low priority.
- *Still 8–13s on a cache MISS.* The composite doesn't make Neon faster, it just
  stops the pile-up. A genuinely fast dashboard would need either a closer
  read-replica or precomputed snapshots. Acceptable — misses are now rare.

**Next moves (priority order)**
1. **Split `api/server.py` (now ~1240 lines)** — the god-file; extract route
   groups (reads / gate+dashboard / voidrunner-write / threads) into modules
   behind a thin dispatcher. Pure refactor, directly serves the "no god files"
   rule. Do this next while the structure is fresh in mind.
2. **Per-run lineage breadcrumb** in the runs expand row (`/lineage?run=`).
3. **Voidmind idea engine** — the real research ceiling (search is flat).
4. **Background-refresh /dashboard** so even a MISS is hidden: serve stale while
   revalidating, so the client never waits 13s.

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
