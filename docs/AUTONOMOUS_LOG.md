# Autonomous build loop — running log

A self-maintained ledger for the unattended 5-min build loop. Each entry: what
shipped, a self-critique, and the next concrete move. The newest entry is at the
top. The next loop iteration should read this first for continuity (git history +
this file are the only memory across fires).

---

## 2026-06-19 · Research fire: first compound result + qk_layernorm resolved (no UI)

**Stayed on research per my own last-fire note.** Two real findings, no new views.

- **First compounding result is in — and it does NOT compound.**
  `swiglu_ffn+cross_block_score_share` = 6.1609, **+0.0111 single-seed**: clears the
  band, but is slightly WORSE than swiglu alone (+0.0139 single / −0.0118 confirmed).
  So stacking SwiGLU + cross_block_score_share is NOT super-additive — adding the
  second mechanism doesn't help, maybe marginally hurts. Honest signal: the champion
  lineage compounded for SOME mechanisms, but not every pair does. It still clears
  the band, so confirm_daemon will paired-confirm it (likely lands ~champion-level).
  swiglu+canon_conv (the two CONFIRMED singles) is queued pri 8, runs next — the
  better test of whether confirmed mechanisms compound.
- **qk_layernorm RESOLVED — the SSH fix works.** It failed once (the connection drop)
  then **completed on retry: 6.1775** (−0.0055, HURTS — a real negative result, not
  the flag incompatibility I'd guessed). One retry, succeeded, NO loop — so the
  unbounded-infra-retry risk didn't bite, and the keepalive + re-queue fix from two
  fires ago is validated end-to-end.
- Prioritized the strongest compounds (swiglu+canon_conv → pri 8) so the
  highest-signal experiments run before single-flag breadth.

**Self-critique**
- *Single-seed compound deltas are noisy* — swiglu+cross_block at +0.0111 vs swiglu
  alone +0.0139 is within run-to-run variance, so "doesn't compound" is a LEAD, not a
  verdict. The paired confirm will settle it. I should state it as "no evidence of
  super-additivity yet," not a hard "doesn't compound."
- *I spent most of the fire waiting* (~8 min across polls for one run). The result was
  worth seeing live, but at ~7-8 min/run I can't watch every experiment — I should
  check results opportunistically (next fire) rather than block. The loop produces
  these autonomously regardless.
- *No attempts-cap still* — qk_layernorm happened to succeed on retry 2, but a truly
  dead box would still loop. Logged again; lower urgency now that one real case
  self-healed, but not zero.

**Next moves (priority order)**
1. **swiglu+canon_conv result** (the two-confirmed compound) — the real test of
   super-additivity. Check opportunistically next fire.
2. **Bound infra retries** (attempts cap) — the last open robustness gap.
3. Let the search keep draining; promotion is the human's call (3 confirmed waiting).

---

## 2026-06-19 · 🏆 SwiGLU CONFIRMED (2nd challenger) + Findings panel on the dashboard

**SwiGLU passed its paired confirm: Δ−0.0118** — the SECOND confirmed champion
challenger, and the BEST paired delta yet (edging canon_conv combo's −0.0114). The
search now has **3 confirmed winners** awaiting promotion (swiglu_ffn, canon_conv+
cross_block_score_share, canon_conv) — all surfaced on the dashboard's ready-to-
promote panel. The `swiglu+canon_conv` compounding experiment was already queued
(last fire's beats-stack), so `--seed-from confirmed` correctly found nothing new to
add — the sharp combo is already on its way.

**Shipped the Findings surface** (voidbase `3ea9ecb`, voidspark `c24d92d`): the
research CONCLUSION is now on the dashboard, not just a CLI. `/findings` exposes
`findings.py`'s buckets over HTTP (reusing the pure `bucket_for` — one classifier
source); a compact `<FindingsSummary>` panel under the live gate shows the counts
header + the confirmed/lead/rejected lists (the long marginal/neutral tails collapse
to counts). Tested through Chrome: **58 tested · 3 confirmed · 1 rejected · 15
marginal · 31 neutral · 7 failed**, zero console errors. The page now tells the whole
story: champion → gate (live + ready-to-promote) → **findings (what works)** → ideas.

**Self-critique**
- *The dashboard is getting LONG.* champion-lineage, gate, findings, idea-backlog,
  live-activity, runs, comparisons — 7 stacked panels. findings + gate's
  confirmed_pending + recent-verdicts overlap (all show the confirmed set). It reads
  as a coherent narrative top-to-bottom, but a tabbed or collapsible layout would
  scale better than infinite vertical stacking. Deferred — it works, just dense.
- *3 confirmed challengers, champion still unmoved.* The guardrail is right (promotion
  is the human's), but the longer it sits, the more the compounding experiments stack
  on a STALE baseline (6.172) instead of the best confirmed (6.1581). The search is
  effectively running one champion-generation behind until a promotion. That's the
  cost of the manual gate while I'm the only one awake — acceptable, but real.
- *I keep choosing "surface it on the dashboard" as the fire.* Three fires of UI now.
  Justified individually (each surfaced real new data), but the marginal value of
  another panel is dropping. Next fire should bias to the RESEARCH (let confirmed
  stacks run, analyze results) or a robustness fix, not more views.

**Next moves (priority order)**
1. **Watch the confirmed-stack results** — swiglu+canon_conv, swiglu+cross_block,
   etc. are queued; do any compound PAST −0.0118? That's the live research question
   and the real payoff, not another surface.
2. **Bound infra retries** (attempts cap) — the one robustness gap still open.
3. Dashboard layout (tabs/collapse) only if it gets unwieldy; psycopg_pool.

---

## 2026-06-19 · findings.py — the research OUTPUT (what 58 mechanisms taught us)

**Built the research conclusion view.** `loop_status` says if the loop runs;
`scripts/findings.py` says what it FOUND — every tested structural mechanism binned
by EVIDENCE strength (paired-confirmed = real; single-seed = suggestive only, the
paired-vs-unpaired trap this platform exists to avoid). One registry read, no GPU.

The search so far (58 mechanisms): **2 CONFIRMED** (canon_conv+cross_block_score_share
Δ-0.0114, canon_conv Δ-0.0101), **1 LEAD** (swiglu_ffn +0.0139, mid-confirm), **1
REJECTED** (gmlp_sgu, paired Δ-0.0020), 15 marginal (beat champ but in-band noise),
31 neutral/worse, 1 implausible (broken metric), 7 failed. That's the honest picture:
a 58-wide search has yielded ~2-3 real >band mechanisms — rare, but REAL, which is the
whole point of the paired gate. `bucket_for()` is pure + unit-tested (+7; 96 suite).

**Testing caught a real bug:** `use_conv_ffn` (val 0.4388 — a broken/forged metric)
showed as the TOP "LEAD" with a +5.73 margin until I applied the same
`voidcheck.is_implausible_win` screen the confirm daemon uses. Without it the findings
view would headline garbage as the best result. Running the tool on real data (not
just unit fakes) is what surfaced it.

**Self-critique**
- *findings + the /research "Recent search" list + the gate's confirmed_pending now
  overlap* — three surfaces describing the same result set at different cuts
  (research-conclusion CLI vs operational list vs actionable-promote panel). Coherent
  but somewhat redundant; a single source (have the dashboard read findings' buckets)
  would DRY it. Didn't unify — they answer subtly different questions.
- *findings reads the DB directly* (like feeder/confirm), so it needs Mac creds — it's
  an operator CLI, not something the dashboard can call. To surface buckets in the UI
  I'd add a `/findings` API endpoint (the API has the DB connection). Logged.
- *"15 marginal" is the real story and I under-emphasize it.* Most mechanisms that
  "beat the champion" do so by <band — i.e. noise. The search's signal is thin; the
  confirmed wins are the exception. findings makes this visible, which is honest but
  sobering: the untried space has winners, but they're sparse.

**Next moves (priority order)**
1. **SwiGLU verdict** (4/6) — moves it confirmed or rejected in findings.
2. **`/findings` API endpoint + a dashboard "Findings" surface** (critique #2) so the
   research conclusion is visible, not just a CLI.
3. Bound infra retries; psycopg_pool.

---

## 2026-06-19 · Dashboard was BROKEN (stale dev-server mess) — cleaned to one server

**Caught the visibility layer down by actually clicking around (the mandate).** I'd
been verifying only `/voidbase` for many fires; this fire I checked the other pages
and found `/research` rendering UNSTYLED (raw serif HTML, no Tailwind) with data
stuck on "Loading…" and a console `Unexpected token '<' … not valid JSON` (a fetch
getting an HTML error page instead of JSON). The whole voidspark app was effectively
broken.

**Root cause:** the Next dev server was a pile of STALE processes — many
`next-server` instances accumulated (from restart attempts across fires + my own this
fire), fighting over port 3000, so requests hit a half-dead/old-build instance.
`lsof -ti:3000`, `ps | grep next` showed ~11 next processes. **Fix:** killed them ALL,
freed ports 3000/3001/3002, started exactly ONE fresh `npm run dev`. Verified in
Chrome: `/research` now renders fully styled — Confirm gate (with the
CONFIRMED-READY-TO-PROMOTE panel), a "Recent search" list of every tested mechanism
with deltas — zero console errors. The API proxy returns valid JSON again.

Live research seen on the restored page: SwiGLU confirm now **4/6** (advancing);
the 2 confirmed challengers still surfaced as ready-to-promote.

**Self-critique**
- *I made it worse before better.* The trigger was a single `/` → 000 (a transient
  dev-compile timeout — `/` actually serves 200 in 4.4s once compiled). I overreacted
  and spawned 2-3 redundant `npm run dev` servers, which landed on 3001/3002 and
  thickened the mess. I should have FIRST checked `lsof -ti:3000` / `ps | grep next`
  before ever starting a server. Diagnose the process table before acting on it —
  same lesson as the duplicate-daemons fire, not learned.
- *No supervision for the dev server either.* Like the daemons, it's a bare
  `nohup npm run dev` with no restart policy, so it dies/duplicates silently between
  fires and the dashboard goes dark unnoticed. `loop_status.py` checks the API + box
  but NOT the voidspark dev server — a real gap, since that's the human's window.
- *I burned a fire on ops, not building.* Legitimate (the dashboard being down is a
  real outage), but the avoidable-overreaction part cost time.

**Operational note for future fires (cross-fire memory):** there should be EXACTLY
ONE `npm run dev` on :3000. Before starting one, run `lsof -ti:3000` — if it's taken,
the server is already up; do NOT spawn another. If the dashboard renders unstyled or
"Loading…" forever, it's the stale-multi-server bug: `ps aux | grep next | awk '{print $2}' | xargs kill -9`, free the port, start one.

**Next moves (priority order)**
1. **Add a voidspark check to `loop_status.py`** — GET localhost:3000/voidbase and
   flag if down or duplicated, so the dashboard outage is caught automatically.
2. **SwiGLU verdict** (4/6 → 6/6) → confirmed-seed stack round.
3. Bound infra retries; psycopg_pool.

---

## 2026-06-19 · feeder --seed-from confirmed (sharper stacking) + slowness diagnosed

**Built the logged stack-quality fix and ruled out a feared bug.**

- **`feeder --mode stack --seed-from confirmed`** (`ee145fa`): stack now optionally
  seeds its C(winners,2) pairs ONLY from singles that passed a paired 3-seed confirm,
  not from any single that screened below the champion once. The latter let
  `gmlp_sgu` (screened in, paired-REJECTED) seed pairs — wasted GPU. Default stays
  `beats` (back-compat). +3 unit tests (fake conn). Verified live: `beats` = 8 seeds
  incl gmlp_sgu; `confirmed` = 1 (use_canon_conv) so it correctly REFUSES to stack
  yet (need ≥2) — once SwiGLU's confirm lands there'll be 2 proven singles and it'll
  pair canon_conv+swiglu, the highest-quality compounding experiment. 89 tests pass.
- **Ruled out two scares:** SwiGLU's confirm looked "stuck at 2/6 for 3 fires" with
  cand-s7 perpetually running — but the worker log shows ONE claim (no requeue loop),
  the box is genuinely training it (GPU 99%, step 400), so it's just a slow single
  run, not the infinite-infra-retry I logged as a risk. And `qk_layernorm`'s "failed
  again" was the STALE old run-log (the SSH-drop one); its queue item is requeued at
  pri 9, waiting behind the pri-100 confirm jobs — it hasn't actually re-run.
- **GPU is NOT throttling** (67°C, 1935/2130 MHz, throttle-reasons 0x0). The ~7-8
  min/run is inherent, not thermal — so throughput isn't a lever I can pull without
  touching research code. Accepted.

**Self-critique**
- *`--seed-from confirmed` is built but I left the default `beats`* and the already-
  queued stack pairs used `beats` (so a gmlp_sgu pair may be in flight). Switching the
  default once a couple of confirms exist would be the real win; I made the tool but
  didn't change behavior. Deliberate (back-compat + only 1 confirmed single today),
  but it means the sharper mode won't bite until I explicitly use it next round.
- *Three fires of "is the confirm stuck?" diagnosis* — I keep re-checking the same
  slow-confirm and re-deriving "it's just slow." loop_status now shows confirm X/6;
  I should TRUST it advances ~1 job/8min and stop re-investigating unless X/6 is
  flat across two fires with the same running job-id (the actual stuck signature).
- *No live exercise of confirmed-mode* — it correctly no-ops at 1 confirmed single, so
  I couldn't see it pair for real yet. The unit test covers the ≥2 case; live proof
  waits on SwiGLU's verdict.

**Next moves (priority order)**
1. **SwiGLU verdict** → then run `feeder --mode stack --seed-from confirmed` to pair
   the 2 proven winners (canon_conv + swiglu) — the sharp compounding round.
2. Let the already-queued beats-stack pairs + exploration drain.
3. Bound infra retries (attempts cap); psycopg_pool.

---

## 2026-06-19 · Stack-mode compounding search — combine the proven winners

**Used the confirmed findings to DIRECT the next search round** — the compounding
strategy that built the champion lineage (each champion stacked a new mechanism on
the last). Instead of more single-flag exploration, `feeder.py --mode stack` pairs
the singles that beat the champion: it found 5 winners (swiglu_ffn, canon_conv,
cross_block_score_share, gmlp_sgu, av_output_carry) and enqueued 4 NEW combos at
priority 6 (above single-flag exploration, below confirms):
- `swiglu_ffn+canon_conv`, `swiglu_ffn+cross_block_score_share` (the two strongest
  mechanisms combined), `swiglu_ffn+gmlp_sgu`, `swiglu_ffn+av_output_carry`.
Dry-validated the two key ones on the box (DRY_OK) — a 3-mechanism stack (champion +
2 flags) can hit incompatibilities (qk_layernorm did), so I checked before spending
GPU. If `swiglu+cross_block_score_share` compounds past the band, it's a bigger lead
than either alone — exactly the super-additive stacking the lineage shows.

Queue order now: SwiGLU confirm (pri 100) → qk_layernorm (9) → stack pairs (6) →
single-flag exploration (5). Confirms finish first, then compounding, then breadth.

**Self-critique**
- *Stack's "winners" = beats-champion-on-val (unpaired), not CONFIRMED.* `gmlp_sgu`
  is in the winners list but was REJECTED on paired confirm. So a stack pair seeded
  by gmlp_sgu spends GPU on a mechanism that's probably noise. Defensible (stack is
  exploratory; the confirm gate filters the pairs later) but I could tighten stack to
  seed only from CONFIRMED singles now that confirmations exist — a real feeder
  improvement (`--seed-from confirmed`).
- *I'm stacking on the OLD champion (6.172), not the confirmed canon_conv combo.*
  Because promotion is pending (guardrail), the champion base is still the old one. So
  `swiglu+canon_conv` is champion+swiglu+canon_conv, which is close to but not exactly
  "best-confirmed + swiglu". Once the human promotes canon_conv combo, re-running
  stack would compound from the new, higher baseline. Noted for post-promotion.
- *Only 4 pairs from C(5,2)=10* — `--limit 4` + 6 already-tried. The swiglu pairs are
  the freshest signal so that's the right 4, but I didn't enqueue the non-swiglu novel
  combos (e.g. canon_conv+av_output_carry). Bounded deliberately to keep GPU focused.

**Next moves (priority order)**
1. **SwiGLU verdict** (confirm draining) — then its stack pairs run.
2. **`feeder --mode stack --seed-from confirmed`** (critique #1) — seed compounding
   only from paired-CONFIRMED singles, not unpaired-beats-champion, once enough
   confirmations exist. Sharper GPU spend.
3. **Re-stack after a promotion** from the new champion baseline (critique #2).
4. Keep feeding; bound infra retries; psycopg_pool.

---

## 2026-06-19 · "Confirmed — ready to promote" surfaced on the dashboard

**Closed the visibility gap from last fire: the human can now SEE the confirmed
challengers waiting to be promoted.** The gate showed clears + verdicts, but a
CONFIRMED-but-unpromoted run was only inferable from the verdict list — no
actionable "this is ready, promote it" signal. Now there is one.

- **API** (`98102cf`): `/gate` returns `confirmed_pending` — runs that PASSED their
  paired confirm AND still beat the live champion AND aren't champion yet (promotion
  is the manual maintainer step the daemon never automates), best-first with each
  one's paired delta. Returns `canon_conv+cross_block_score_share` (Δ-0.0114) +
  `canon_conv` (Δ-0.0101).
- **UI** (`4165182`): an emerald "CONFIRMED — READY TO PROMOTE (N)" panel in the
  confirm gate, directly under the champion, each row showing val + paired Δ, with an
  inline note that promotion is manual. Tested through Chrome (renders both, zero
  console errors, typecheck clean).

Loop: SwiGLU confirm now 2/6 and climbing — a likely second confirmed challenger.

**Self-critique**
- *`confirmed_pending` overlaps the champion-lineage's job a bit.* A promoted
  confirmed run becomes a champion (shown in the lineage); an unpromoted one shows
  here. The boundary is "is it the current champion?" — clean, but two surfaces now
  describe the confirmed set. Acceptable (they answer different questions: "what's the
  history" vs "what's waiting"), but worth keeping coherent.
- *There's no promote BUTTON* — by design (promotion is a guardrailed maintainer
  action, and a one-click promote from a read-only dashboard would be the exact
  auto-promotion the system forbids). But the operator still has to promote via CLI/
  SQL; a guided "here's the command to promote this" hint would bridge read-only
  visibility to action without breaking the guardrail. Didn't build it.
- *Two confirmed challengers both stem from `canon_conv`* — the combo and the single.
  Promoting the combo (better, -0.0114) likely subsumes the single. The panel lists
  both equally; it could note "best" or that they're related. Minor.

**Next moves (priority order)**
1. **SwiGLU verdict** (2/6 → 6/6) — second confirmed challenger likely.
2. **A "how to promote" hint** by the confirmed_pending panel (critique #2) — the
   exact `sync_champions`/SQL command, so the human can act without guessing, while
   promotion stays their explicit step.
3. Keep feeding the untried space; bound infra retries; psycopg_pool.

---

## 2026-06-19 · 🏆 FIRST CONFIRMED CHALLENGER — canon_conv+cross_block_score_share

**The autonomous loop produced its first paired-confirmed improvement over the
champion, judged end-to-end with no human in the path.**

`confirm_daemon` judged it this fire:
> `use_canon_conv+use_cross_block_score_share` → **CONFIRMED** — paired 3/3 seeds,
> cand mean **6.1605** vs champ 6.1720, **Δ −0.0114** (band 0.001), sign 3/3 favour
> candidate. Per seed: s42 6.1544 vs 6.1762 (−0.0218); s123 6.1581 vs 6.1669
> (−0.0088); s7 6.1691 vs 6.1728 (−0.0037).

The candidate run is now `verification='confirmed'`; the gate dropped it from clears
(SwiGLU remains, 1/6); **the champion stays 6.172 — the manual-promotion guardrail
correctly did NOT auto-swap.** It's confirmation-ready for the human to promote
(would lower the champion ~6.172 → ~6.16).

**This CORRECTS my own earlier error.** Three fires ago I hand-ran a paired test of
this exact combo and got +0.0044 (sub-band), concluding it was noise. WRONG — that
was the ~0.019 config-drift from hand-rebuilding the champion+flag config. The
*faithful* registry confirm (the real queue config, on the box, judged by the daemon)
shows it's a genuine −0.0114 winner across all 3 seeds. Lesson, now burned in: trust
ONLY the worker/queue pipeline's numbers, never a hand-reconstructed config.

**Also this fire** (`d1e396b`, prior commit): SSH-drop resilience landed — keepalive +
re-queue-on-infra-drop, so qk_layernorm-style losses retry instead of poisoning the
search. 86 tests green.

**Self-critique**
- *I dismissed canon_conv combo as noise for two fires on bad (hand-config) data.*
  The faithful pipeline existed the whole time; I should have trusted it over my
  ad-hoc test the moment they disagreed, instead of asserting "sub-band." The
  config-drift caveat was even in my own notes — I under-weighted it.
- *"Search plateaued" was wrong for even longer.* The whole project inherited that
  framing; it took enumerating the 177 flags (115 untried) + actually running them to
  break it. Two confirmed/near-confirmed winners (canon_conv combo, SwiGLU) came out
  of the "exhausted" space within hours of feeding it. Inherited conclusions deserve
  a data check before they steer strategy.
- *I'm respecting the promotion guardrail* (not swapping the champion), which is
  correct — but the human has no signal yet that a confirmed challenger is WAITING.
  A "confirmed, awaiting promotion" surface on the dashboard would close that gap
  (the gate shows clears + verdicts but not "ready to promote"). Good next build.

**Next moves (priority order)**
1. **Surface "confirmed, awaiting promotion"** on the dashboard — the human needs to
   see canon_conv combo is ready to become champion (guardrail = their call).
2. **SwiGLU verdict** (1/6 → 6/6) — a SECOND confirmed challenger likely incoming.
3. Keep feeding the untried space (the strategy is now proven to yield winners).
4. Bound infra retries; psycopg_pool.

---

## 2026-06-19 · Root-caused a spurious failure → SSH-drop resilience (keepalive + retry)

**Read the log instead of guessing, found the real bug, fixed it.** Last two fires I
assumed `use_qk_layernorm` failed because the flag was incompatible with the alibi
champion. WRONG. Its saved box log (`run-logs/auto-use_qk_layernorm-7c046c33.log`)
ends at **"Connection to 1.208.108.242 closed by remote host"** — the SSH connection
dropped mid-training (a vast.ai/network blip), so a multi-minute run was lost and
recorded as a `failed` EXPERIMENT. That poisons the search signal and dedup-blocks a
real retry.

**Fix** — voidbase `d1e396b`, two parts:
1. **SSH keepalive** in the worker (`ServerAliveInterval=30`, `ServerAliveCountMax=10`)
   so a long run isn't dropped by an idle NAT/firewall or a brief blip (tolerates
   ~5 min of silence).
2. **`is_transient_infra_failure(rc, out)`** — a non-OK run that parsed NO val_loss
   and looks like a drop (rc 255 / "closed by remote host" / "Broken pipe" / …) is
   **RE-QUEUED to retry**, not reported failed. A genuine training crash (traceback,
   val parsed) still records as `failed`. +5 unit tests; **86 pass** (whole suite).

Applied via a clean worker restart (now `python -u`, unbuffered): requeued the
orphaned in-flight job + qk_layernorm (the drop victim). Worker re-claimed instantly;
`failed` count 16→14. Loop healthy, one of each daemon.

**Live research:** canon_conv+cross_block_score_share confirm now **5/6** (base-s42
running → 6/6 → judged imminently); SwiGLU 1/6. The canon_conv verdict lands next.

**Self-critique**
- *I guessed "incompatible flag" twice without reading the saved log.* The worker
  ALREADY persists full box output per run (`run-logs/<job>.log`) — exactly for this
  — and I ignored it for two fires. The data to root-cause was one `tail` away. When
  a run fails, READ ITS LOG before theorizing.
- *The infra-failure retry has no attempt cap.* A box that's genuinely down would
  re-queue → drop → re-queue forever. The reaper + the box going `offline` (no
  heartbeat) is a partial backstop, but a real fix bounds retries (e.g. a
  `attempts` counter on the queue row, fail after N). Logged.
- *I requeued `like '%qk_layernorm%' and failed`, which also matched an OLD
  `282-deepnet-rope-base-qk-layernorm`* — harmless (it just re-runs), but sloppy
  targeting; I should have matched the exact id.
- *`fail_reason` capture is now LESS urgent* — the real lesson was "read the existing
  run-log," not "add a column." The log already has the reason; the gap is that I
  didn't look. A UI link to the run-log would close it better than a parsed column.

**Next moves (priority order)**
1. **canon_conv verdict** (imminent, 5/6) then **SwiGLU verdict** — the research
   payoff. canon_conv likely rejects (~+0.0044 paired last test); SwiGLU is the hope.
2. **Bound infra retries** (critique #2): an `attempts` cap so a dead box can't
   spin a job forever.
3. **Surface the run-log in the UI** (critique #4) — a link from a failed run to its
   saved box output, so failures are one click to diagnose.
4. Profile per-run throughput; psycopg_pool.

---

## 2026-06-19 · Live research verified on the dashboard; SwiGLU confirm progressing

**A verify + tooling fire — the loop is doing the research autonomously, so this
confirmed it's working and sharpened the monitoring.**

- **Verified the full live research story renders** (`/voidbase`, Chrome, zero
  console errors): champion lineage → Confirm gate showing **CLEARS BAND (2)** with
  `canon_conv+cross_block_score_share` confirming 4/6 (+0.0176) and **`use_swiglu_ffn`
  confirming 1/6 (+0.0139)** → "Gate live — 2 candidates in paired confirm" → recent
  verdicts. The autonomously-discovered SwiGLU lead is visibly being paired-confirmed.
- **SwiGLU confirm IS progressing** (1/6 — cand-s42 landed), just slowly: ~8 min/job
  on the 3060, so the full 6-job verdict is ~40 min out. Diagnosed the apparent
  "stuck" as just slow: box healthy (1 GPU app, clean memory, load ~1.0; the 7
  run_experiment procs are one run's dataloader workers). Reaper had one transient
  Neon timeout — skipped that sweep gracefully, no harm.
- **Enhanced `loop_status.py`** (commit `be485d4`): GATE CLEARS now shows each lead's
  margin + live confirm X/6 (e.g. `use_swiglu_ffn +0.0139 confirm 1/6`), so the
  research frontier is one command.

**Self-critique**
- *~8 min/job is the real throughput ceiling now.* The search does 6 jobs/confirm +
  exploration, serially, one box. A confirm takes ~50 min; mining 97 untried flags is
  ~13h. The lever isn't more monitoring — it's faster runs (is the per-run dataset
  load / model build re-paid every run? worth profiling) or a second box (a human
  rental decision). I keep verifying the loop; the bottleneck is throughput.
- *This fire shipped little NEW capability* — a tooling tweak + verification. Justified
  (the loop is autonomous and needed confirming, the mandate says test through Chrome),
  but I should bias toward the throughput question or the `fail_reason` build next,
  not another monitoring pass.
- *qk_layernorm's failure is still unexplained* (logged last fire). Two pri-9 leads
  queued, one cleared (SwiGLU), one failed (qk_layernorm) — a 50% construct-failure
  rate on hand-picked flags suggests some untried flags are incompatible with the
  alibi base; `fail_reason` capture would turn that from a guess into data.

**Next moves (priority order)**
1. **SwiGLU verdict** (~40 min out) — does +0.0139 hold paired across 3 seeds?
2. **`fail_reason` capture** — turn opaque `failed` runs into diagnosable ones
   (why qk_layernorm died); small worker + schema change, real signal.
3. **Profile per-run overhead** — if dataset load/compile is re-paid each run, caching
   it could ~2x search throughput (the actual bottleneck).
4. psycopg_pool.

---

## 2026-06-19 · Fix confirm-daemon cancelled-job deadlock (code, not just ops)

**Shipped** — voidbase `06ed1b9`. Last fire I hand-fixed the canon_conv confirm that
was frozen at 4/6 (2 cancelled jobs). This fire fixed the underlying BUG so it can't
recur: confirm_daemon Phase B now detects cancelled jobs in a candidate's confirm set
and re-queues them (new `requeue_confirm_jobs`: status→needs-run, claim cleared,
guarded on `status='cancelled'`), skipping the judge that cycle so the worker
completes the set. Without it, any confirm job cancelled mid-flight (box outage,
reaper sweep, manual) freezes the candidate at <6/6 forever — it never becomes
done/failed, the only states the terminal check counts. +2 unit tests (fake conn);
10 pass. Restarted the daemon with `python -u` so its logs are unbuffered (the buffer
fooled me two fires ago).

Loop progress this fire (via the new `loop_status.py`):
- **SwiGLU paired confirm DRAINING** — 1/6 running (cand-s42), 5 queued. The
  autonomous confirm of the first real challenger is underway.
- canon_conv confirm: 4 done + 2 needs-run (last fire's re-queue) → will reach 6/6
  and finally get a verdict.
- `use_qk_layernorm` **FAILED** (null val) — likely incompatible with the alibi
  champion base (QK-LayerNorm vs the alibi/poly-alibi path). The loop recorded it
  failed and moved on (graceful degradation working as designed). A negative/no-data
  result, not a loop bug.

**Self-critique**
- *The fix re-queues but doesn't bound retries.* If a confirm job fails for a real
  reason (a genuinely broken flag like qk_layernorm), re-queuing a CANCELLED one is
  fine, but I should make sure I'm not creating a requeue→fail→requeue loop. I'm not
  — the heal only touches `cancelled`, and a failed job is terminal (counts toward
  judging), so a broken arm resolves as a rejected confirm, not an infinite loop.
  Verified the logic, but it's worth a test for the failed-arm path too (didn't add).
- *qk_layernorm failing silently-ish is a gap.* It's recorded `failed` with null val,
  but I don't capture WHY (the box stderr isn't surfaced to the registry). A run that
  fails to construct vs one that OOMs vs one that diverges are different stories; the
  registry flattens them to "failed". Worth a `fail_reason` column someday.
- *Restarted only confirm_daemon with -u, not worker/reaper* (worker is mid-run;
  killing it loses SwiGLU's confirm job). They'll get -u on their next natural
  restart. Inconsistent for now.

**Next moves (priority order)**
1. **SwiGLU verdict** — once its 6 confirm jobs are terminal, confirm_daemon judges
   the paired 3-seed delta. THE research question: does +0.0139 hold paired? Watch it.
2. **canon_conv verdict** — its confirm will also complete now; likely rejected
   (my paired test said ~+0.0044, sub-band).
3. **fail_reason capture** for failed runs (critique #2) — small schema + worker change.
4. Keep feeding the untried space; psycopg_pool.

---

## 2026-06-19 · 🎯 SwiGLU CLEARS THE BAND faithfully → autonomous paired confirm running

**The autonomous research loop found and is now confirming a genuine new champion
challenger — end-to-end, no human in the path.**

- **SwiGLU faithful result: `use_swiglu_ffn` = 6.1581, +0.0139 vs champion 6.172 —
  CLEARS the 0.01 band.** The hand-sweep's +0.0128 holds registry-clean (even a hair
  stronger). The gate now lists it as a band-clearing candidate (blocker: null, gate
  live). This is the FIRST real champion challenger of this search, pulled from the
  115-untried-mechanism space discovered two fires ago — the untried-space thesis is
  now empirically validated, not just argued.
- **confirm_daemon auto-enqueued SwiGLU's 3-seed paired confirm (6 jobs)** the cycle
  after it landed (Phase A). The worker will drain them; once all 6 are terminal the
  daemon judges the paired delta — and if it holds, SwiGLU is confirmation-ready (the
  manual-promotion guardrail still gates the actual champion swap, by design).
- **Unstuck the stale `canon_conv+cross_block_score_share` confirm.** It was frozen
  at 4/6 forever — 2 of its paired jobs (base-s42, cand-s7) were CANCELLED when the
  box went dark days ago, and "terminal" only counts done/failed, so it could never
  reach 6/6 or be judged. Re-queued the 2 cancelled jobs (they carry their config) so
  it completes and gets a real verdict. (My paired test two fires ago suggested its
  +0.0176 was unpaired-inflated to ~+0.0044 — the confirm will settle it honestly.)
- mla finished: 6.1966 (−0.0246, HURTS — clear negative result).

**Also shipped** — `scripts/loop_status.py` (commit `ca14fa4`): one-shot loop health
(daemons / box heartbeat / queue / in-flight / recent deltas) from authoritative
state, not the block-buffered daemon logs that fooled me last fire. Exit non-zero on
an unhealthy loop. Dogfooded it this fire to catch SwiGLU starting + mla's result.

**Self-critique**
- *The cancelled-job confirm deadlock is a latent bug, not just stale data.* ANY
  candidate whose confirm has a cancelled job (box outage, reaper cancel, manual)
  freezes at <6/6 forever — confirm_daemon's terminal check is `done/failed` only.
  I hand-fixed THIS instance; the real fix is the daemon treating a cancelled
  confirm job as needing re-queue (or counting it terminal and judging on what
  landed). Logged as a code fix, not just an op.
- *Single-seed "clears band" is still a LEAD, not a verdict.* SwiGLU at +0.0139 on
  one seed is promising; the 3-seed paired confirm now running is what makes it real.
  I'm stating it as a challenger, not a new champion — the system's whole point.
- *I re-queued canon_conv's jobs, adding GPU load* that competes with SwiGLU's
  confirm. Fine (both drain), but it means SwiGLU's verdict is a few runs further
  out. Acceptable — resolving the old lead honestly is worth it.

**Next moves (priority order)**
1. **Watch SwiGLU's paired confirm complete + get judged** — the live research
   question. If confirmed, surface it for the human to promote (guardrail).
2. **Fix the confirm-daemon cancelled-job deadlock in code** (critique #1): a
   cancelled confirm job must be re-queued or counted, so a box outage can't freeze
   a candidate forever.
3. **Keep feeding** the 115-untried space (`feeder.py --limit N`).
4. **`python -u`** daemons; **psycopg_pool**.

---

## 2026-06-19 · Loop validated end-to-end + SwiGLU lead queued for faithful confirm

**The revived loop's full cycle is confirmed working, and the real lead is teed up.**

1. **Full claim→train→report cycle VALIDATED.** The first job the worker drained,
   `auto-use_mix_norm`, completed on the box and **reported to Neon: val_loss 6.1725,
   queue item → done.** (The worker's stdout log is block-buffered to its file so it
   looked stuck — the registry is the truth; the run row + queue status confirm it.)
   Result itself: 6.1725 = −0.0005 vs champion 6.172, sub-band noise — a valid
   NEGATIVE result, mix_norm doesn't help.
2. **SwiGLU lead queued for a faithful run.** The hand-sweep found `use_swiglu_ffn`
   +0.0128 (the one >band lead) but that was a hand-built config. Enqueued it +
   `use_qk_layernorm` on thread `tiny1m3m` (gate-visible) at **priority 9** (above the
   16 feeder jobs at 5), via the feeder's own `make_experiment`/`champion_base` so the
   row is registry-identical. The worker runs these NEXT; if SwiGLU's +0.0128 holds
   registry-faithfully and clears the band, confirm_daemon auto-enqueues its 3-seed
   paired confirm → the path to the first new champion mechanism in this search.
   (`use_parallel_block` skipped — already in the dedup space.)

Loop state: worker draining (GPU 100%), 16 needs-run (14 feeder @ pri 5 + swiglu/
qk_layernorm @ pri 9), box healthy/heartbeating, reaper + confirm_daemon up.

**Self-critique**
- *I almost misdiagnosed the buffered worker log as a hang.* Spent two checks
  chasing "why no report" before querying the registry directly — which instantly
  showed mix_norm done @ 6.1725. Lesson: for a backgrounded process logging to a
  file, trust the DB/state, not the (buffered) log tail. Could also launch the
  worker with `python -u` for unbuffered logs — worth doing next time.
- *mix_norm being sub-band is the expected base rate.* Most single flags won't beat
  a 6-mechanism-deep champion; the value is in the rare winner (SwiGLU may be one).
  The loop's job is to cheaply rule out the 99% — which it's now doing autonomously.
- *I prioritized only 2 of the promising untried flags.* SwiGLU is the evidenced
  lead so that's right, but a fuller campaign would queue the whole shortlist. Kept
  it tight to get SwiGLU a clean, faithful answer first.

**Next moves (priority order)**
1. **Read SwiGLU's faithful result** next fire — does +0.0128 hold registry-clean?
   If it clears the band, confirm_daemon will already be paired-confirming it; watch
   the gate. THIS is the live research question.
2. **Keep the loop fed** — when needs-run drops low, `feeder.py --limit N` (97 more
   untried structural flags) or `--mode stack` on any confirmed winners.
3. **`python -u` for the daemons** so logs are unbuffered (critique #1).
4. **psycopg_pool**.

---

## 2026-06-19 · THE LOOP IS TURNING — full 4-daemon pipeline live, box revived

**Milestone: the autonomous distributed research loop is operational end-to-end,
registry-faithful, and visible on the dashboard.** Not a widget — the actual system.

What's running (verified live):
- **`worker.py loop`** (Mac dispatcher → GPU box) — claimed `auto-use_mix_norm` from
  the 16 queued jobs and SSHed a TORCHDYNAMO_DISABLE=1 + full-champion-base
  EXPERIMENT_CONFIG to the box. Box trains at GPU 98%. Reports to Neon on finish.
- **Box heartbeat REVIVED** — `1.208.108.242:52646` went offline(~9h stale) →
  **healthy (hb_age 12s)** the moment the worker started; the worker's heartbeats
  fixed the dashboard's stale-offline display. Activity panel now shows
  "1 in flight · 15 queued · use_mix_norm running on box · automation".
- **`reaper.py loop`** — self-heals stranded jobs / dark boxes (90s heartbeat
  timeout).
- **`confirm_daemon.py --interval 120`** — judges band-clearers, auto-enqueues 6
  paired jobs per candidate that clears, NEVER auto-promotes (manual guardrail
  intact). First cycle correctly SKIPPED the implausible `use_conv_ffn` (val 0.4388,
  >50% better = broken/forged) and noted the old canon_conv+cross_block_score_share
  confirm still 4/6.
- **Drift fix from earlier this fire** makes every worker run registry-faithful.

Found + fixed mid-fire: **duplicate reaper + confirm_daemon** from a prior session
were still alive (pids 80349/69375) — I'd started a second pair. Two confirm daemons
race on enqueues. Killed the stale duplicates; exactly one of each now runs
(worker 20772, reaper 20918, confirm 20919). (This also explains why confirm was
"running" for days but nothing progressed — the WORKER wasn't up, so no jobs ran.)

Verified through Chrome: Live activity shows the running job + active box, box
online, zero console errors.

**Self-critique**
- *I started duplicate daemons without checking what was already running.* A
  `ps aux | grep` FIRST would have shown the orphaned prior-session reaper/confirm.
  Always inventory running processes before spawning more — especially daemons.
- *These daemons are nohup'd children of this session.* They should survive context
  cycling (nohup ignores SIGHUP), but if the whole Claude session exits they MAY
  die — there's no real supervisor (systemd/pm2). For a truly unattended loop they
  belong under a process manager, not nohup. Flagged, not fixed (would need the
  human's machine setup).
- *15 jobs × ~7 min = ~1.75h of GPU* now committed autonomously, plus whatever
  confirm_daemon enqueues for band-clearers. Cheap on a rented 3060 (~cents) and
  squarely what the user asked for ("do research there"), but it IS unattended
  spend — worth stating plainly.
- *I haven't confirmed the first worker job actually REPORTS to Neon yet* (it's
  mid-run). The claim+dispatch+heartbeat path is verified; the report path is
  assumed-good from the prior 2026-06-18 validation. Next fire must confirm a fresh
  result row landed.

**Next moves (priority order)**
1. **Confirm the first faithful result landed in Neon** (`auto-use_mix_norm` →
   runs row with a sane val_loss) — validates the full claim→train→report cycle on
   the revived loop. If any flag's single-seed clears the band, confirm_daemon will
   auto-enqueue its 3-seed paired confirm.
2. **Enqueue swiglu_ffn on thread `tiny1m3m`** (feeder thread, gate-visible) so the
   +0.0128 hand-sweep lead gets a registry-faithful run + a real confirm. Note:
   `enqueue.py` uses thread "tiny1m3m search" (NOT gate-visible) — must go via
   `feeder` or a thread="tiny1m3m" enqueue for the gate to treat it as a candidate.
3. **Keep the loop fed** — when the 16 drain, re-run `feeder.py --limit N` (99 more
   untried structural flags remain) or `--mode stack` on any new winners.
4. **psycopg_pool**; **freshness badge** already shipped.

---

## 2026-06-19 · Config-drift ROOT-CAUSE FIX + real pipeline armed (16 faithful jobs queued)

**The big win: the config-drift bug is fixed at the root, and the proper automated
pipeline is now armed with registry-faithful jobs.**

1. **Drift fix.** The box had no `autoresearch/champion.json`, so `run_experiment.py`
   built on a BARE config (the 6.2556 mystery two fires ago) and delta configs were
   unfaithful. Copied the Mac's authoritative champion.json (the same one the feeder
   reads) to `/root/universe-lm/autoresearch/champion.json`. Verified: a bare delta
   `{use_swiglu_ffn:true}` now resolves to the FULL champion base (muon_lr 0.048,
   muon_momentum 0.9, use_deepnet_alpha, use_poly_alibi + alibi env) + the flag. So
   delta-config runs are now registry-faithful — the 0.019 drift is gone at the source.
2. **`.env` box coords ARE valid** (my earlier "empty" read was a bad raw grep;
   `db.conn.env_value` returns the right host/port/user/repo/python). So `worker.py`
   can reach the box — the pipeline is wired.
3. **Armed the queue.** Ran `feeder.py --limit 16 --priority 5`: enqueued 16 untried
   structural flags as registry-faithful `needs-run` jobs (champion+flag, deduped vs
   62 already-tried). The queue went from 0 → 16 needs-run. These are ready for
   `worker.py` to drain on the box and report to Neon — the proper, automated,
   registry-faithful path (vs my hand-run sweep).

**Hand-sweep results (continuing, within-session deltas vs baseline 6.1778):**
- `use_swiglu_ffn` 6.1650 → **+0.0128 (clears band — the lead holds)**
- `use_value_residual` 6.1800 → −0.0022 (slightly WORSE; negative result, drop it)
- qk_layernorm/parallel_block/sub_ln/v_rmsnorm/head_gain still running (~30 min).

**Self-critique**
- *I should have copied champion.json to the box the moment I found it missing two
  fires ago* instead of hand-passing full configs as a workaround. The root-cause
  fix is one scp; I deferred it and paid in confusion (the 6.2556 run, the 0.019
  drift analysis). Fix the root, not the symptom.
- *GPU contention is the real constraint now.* One box, and my hand-sweep is using
  it, so I couldn't start the worker this fire (two GPU loads on 12 GB = OOM risk).
  This is why I queued the jobs but deferred the drain. A second box would let the
  proper pipeline run while the hand-sweep finishes — but that's a rental decision
  for the human, not an autonomous one.
- *The hand-sweep is now redundant with the pipeline* (both test untried flags), but
  it tests a DIFFERENT 7 flags than the feeder's 16, so killing it would lose those
  datapoints — I let it finish. Going forward, ONLY the feeder→worker path (no more
  hand-runs): it's faithful, deduped, automated, and reports to the registry.
- *Didn't start confirm_daemon/reaper.* The full loop is 4 daemons; I've only armed
  the queue. Starting worker+confirm+reaper is the next fire's job.

**Next moves (priority order)**
1. **Start `worker.py loop`** once the hand-sweep finishes (GPU free) — drains the 16
   faithful jobs on the box, reports to Neon. Its heartbeats also revive the box's
   ONLINE status on the dashboard (fixing the stale-offline display). THE core loop.
2. **Enqueue swiglu_ffn + the band-clearing sweep flags via the feeder** so the lead
   gets a registry-faithful run + (via confirm_daemon) a real 3-seed paired confirm.
   swiglu at +0.0128 is the first genuine champion challenger in this search.
3. **Start confirm_daemon + reaper** to close the loop (judge confirms, self-heal).
4. **psycopg_pool**; freshness already shipped.

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
(DRY_OK). Running unattended in tmux (`/root/sweep.log`, ~7 min/run, ~50 min for
8). NEXT fire: read `/root/sweep.log` for the remaining deltas.

**FIRST RESULTS — and a REAL >band lead:**
- `baseline` (champion) = **6.1778** (identical to last fire's 6.1778 — the
  champion config is deterministic on this box, so within-session deltas are clean
  signal, and last fire's 0.019 "drift" was the candidate config differing from the
  registry's, NOT nondeterminism).
- `use_swiglu_ffn` = **6.1650** → delta **+0.0128 vs baseline — CLEARS the 0.01
  band.** SwiGLU (the LLaMA FFN) beats the champion on a faithful paired seed. The
  first >band candidate of the session, pulled straight from the untried space.
  Proves the thesis: the search was NOT exhausted — a literature-strong untried
  mechanism improves the champion. (Caveat: 1 seed; needs a 3-seed paired confirm
  to promote, and +0.0128 is a modest margin. But the within-session delta vs the
  rock-stable 6.1778 baseline is trustworthy.)
- 6 more candidates (value_residual, qk_layernorm, parallel_block, sub_ln,
  v_rmsnorm, head_gain) still running → next fire collects them.

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
