#!/usr/bin/env python3
"""findings.py — the research OUTPUT: what has the tiny1m3m search actually learned?

`loop_status.py` answers "is the loop running?"; this answers "what did it find?".
It distills every tested structural mechanism into honest buckets by EVIDENCE
STRENGTH — paired-confirmed results are trustworthy; single-seed deltas are only
suggestive (the paired-vs-unpaired trap this whole platform exists to avoid). One
read of the registry, no GPU, no writes.

  CONFIRMED  passed a paired 3-seed confirm and beats the champion → real win
  REJECTED   ran the paired confirm and FAILED it → disproven (was screen-luck)
  LEAD       single run clears the screen band but isn't confirmed yet → confirm it
  MARGINAL   beats the champion but inside the band → noise, not worth confirming
  NEUTRAL/↓  doesn't beat the champion (== or worse)
  FAILED     never produced a result (crash / infra) — no signal

  python3 scripts/findings.py            # human summary
  python3 scripts/findings.py --json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402
import voidcheck  # noqa: E402  — single source for the screen band

SCOPE = "tiny1m3m"


def _champion_val(cur) -> float | None:
    cur.execute(
        "select c.val_loss from champions c "
        "where c.scope=%s and c.superseded_at is null "
        "order by c.promoted_at desc limit 1", (SCOPE,))
    row = cur.fetchone()
    return float(row[0]) if row else None


def bucket_for(val: float | None, verdict: tuple | None,
               champ: float | None, band: float) -> str:
    """Which evidence bucket a mechanism lands in. Pure (the implausibility screen
    is injected via voidcheck) so it's unit-testable without a DB. Precedence:
    a PAIRED verdict beats any single-run heuristic; then failed (no val); then the
    too-good-to-be-true screen; then margin-vs-band."""
    if verdict is not None:
        return "confirmed" if verdict[0] else "rejected"
    if val is None:
        return "failed"
    if champ is not None and voidcheck.is_implausible_win(val, champ):
        return "implausible"
    if champ is None:
        return "neutral"
    margin = champ - val
    if margin >= band:
        return "lead"
    if margin > 0:
        return "marginal"
    return "neutral"


def collect() -> dict:
    conn = connect()
    cur = conn.cursor()
    champ = _champion_val(cur)
    band = voidcheck.SCREEN_BAND

    # paired verdicts (the trustworthy layer): mechanism name -> (agrees, delta).
    # A `confirm-*` run id carries the candidate's name minus the prefix; we key on
    # the candidate run's name, which the join gives directly.
    cur.execute(
        "select r.name, c.agrees, c.delta_from_original "
        "from confirmations c join runs r on r.id = c.run_id "
        "where r.thread_name=%s", (SCOPE,))
    verdict: dict[str, tuple[bool, float]] = {}
    for name, agrees, delta in cur.fetchall():
        # keep the most decisive (a name should have one confirm; be defensive)
        if name not in verdict or agrees:
            verdict[name] = (bool(agrees), float(delta) if delta is not None else None)

    # every mechanism's best (lowest) val over its DONE runs; exclude the confirm
    # machinery rows (they're re-runs of a candidate, not a distinct lever).
    cur.execute(
        "select name, min(final_val_loss) "
        "from runs where thread_name=%s and name like 'use_%%' "
        "and (name not like 'confirm-%%') "
        "group by name", (SCOPE,))
    best: dict[str, float | None] = {}
    for name, val in cur.fetchall():
        best[name] = float(val) if val is not None else None

    # names that have ONLY failed runs (no val anywhere)
    cur.execute(
        "select distinct name from runs where thread_name=%s and name like 'use_%%' "
        "and name not in (select name from runs where thread_name=%s "
        "  and name like 'use_%%' and final_val_loss is not null)",
        (SCOPE, SCOPE))
    failed_only = {r[0] for r in cur.fetchall()}

    buckets: dict[str, list] = {k: [] for k in
                                ("confirmed", "rejected", "lead", "marginal",
                                 "neutral", "implausible", "failed")}
    names = set(best) | failed_only | set(verdict)
    for name in names:
        v = best.get(name)
        bucket = bucket_for(v, verdict.get(name), champ, band)
        entry = {"name": name, "val": v}
        if name in verdict:
            entry["paired_delta"] = verdict[name][1]
        elif v is not None and champ is not None:
            entry["margin"] = round(champ - v, 4)
        buckets[bucket].append(entry)

    # best-first within the scored buckets
    for k in ("lead", "marginal", "neutral"):
        buckets[k].sort(key=lambda e: e["val"] if e["val"] is not None else 9e9)
    buckets["confirmed"].sort(key=lambda e: e["val"] if e["val"] is not None else 9e9)
    conn.close()
    return {"scope": SCOPE, "champion_val": champ, "screen_band": band,
            "buckets": buckets,
            "counts": {k: len(v) for k, v in buckets.items()}}


def _fmt(s: dict) -> str:
    b = s["buckets"]
    L = [f"tiny1m3m search findings  (champion {s['champion_val']}, band {s['screen_band']})"]
    L.append(f"  tested: {sum(s['counts'].values())} mechanisms  |  "
             + "  ".join(f"{k}={n}" for k, n in s["counts"].items() if n))

    def rows(title, items, val_key, extra):
        if not items:
            return
        L.append(f"\n  {title} ({len(items)}):")
        for e in items:
            v = e.get("val")
            vs = f"{v:.4f}" if isinstance(v, (int, float)) else "  —   "
            L.append(f"    {e['name'][:46]:46s} {vs}  {extra(e)}")

    rows("✅ CONFIRMED (paired — real wins)", b["confirmed"], "val",
         lambda e: f"paired Δ{e['paired_delta']:+.4f}" if e.get("paired_delta") is not None else "")
    rows("🎯 LEAD (clears band, confirm next)", b["lead"], "val",
         lambda e: f"+{e['margin']:.4f}")
    rows("❌ REJECTED (paired — disproven)", b["rejected"], "val",
         lambda e: f"paired Δ{e['paired_delta']:+.4f}" if e.get("paired_delta") is not None else "")
    rows("· marginal (beats champ, in-band noise)", b["marginal"], "val",
         lambda e: f"+{e['margin']:.4f}")
    rows("· neutral / worse", b["neutral"], "val",
         lambda e: f"{e['margin']:+.4f}" if e.get("margin") is not None else "")
    rows("⚠ implausible (broken/forged metric — not a lead)", b["implausible"], "val",
         lambda e: "screened out")
    rows("💥 FAILED (no result)", b["failed"], "val", lambda e: "")
    return "\n".join(L)


def main() -> int:
    s = collect()
    print(json.dumps(s, indent=2, default=str) if "--json" in sys.argv else _fmt(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
