#!/usr/bin/env python3
"""confirm_daemon.py — auto-confirm gate (the #1 scaling unblock).

Confirming a screen WIN is run by hand today; at 50+ experiments/week the
maintainer is the bottleneck. This daemon turns "the maintainer confirms things"
into "the system queues confirms automatically" — it watches Neon for runs that
beat the champion on the cheap single-seed screen and, for each, ENQUEUES a
paired 3-seed confirm (3 candidate seeds + 3 champion-baseline seeds at matched
seeds). When all 6 land it computes the paired delta, writes a `confirmations`
row, and flips the candidate run's `verification` to confirmed/rejected.

It deliberately does NOT promote. Promotion to champion stays a maintainer
action — a human in the loop even at scale. `--auto-promote` is a stubbed flag
that is OFF and a no-op (see the separate auto-promotion issue).

The baseline arm is ALWAYS rebuilt from the CURRENT CHAMPION CONFIG (champions
table -> its run's `config`), re-run fresh at the same 3 seeds — never the bare
base config and never a stale log. That is the whole point: the old confirm read
the bare base, not the flag-defined champion, and re-read stale logs, so it
over-credited promotions. Here the control is the live champion re-run in the
same batch, so the paired delta is drift-free.

  python3 scripts/confirm_daemon.py --once          # one poll cycle, then exit
  python3 scripts/confirm_daemon.py --interval 60   # poll loop (default 60s)
  python3 scripts/confirm_daemon.py --once --scope tiny1m3m
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402
# Integrity policy (SEEDS, the bands) and the paired-delta judgement now live in
# voidcheck — the one place the platform's trust rules are defined + property-
# tested. Re-exported from this module so existing importers (scripts, tests that
# do `from scripts.confirm_daemon import SEEDS, paired_verdict`) keep working.
from voidcheck import (  # noqa: E402,F401
    CONFIRM_BAND,
    MAX_DROP_FACTOR,
    SCREEN_BAND,
    SEEDS,
    is_implausible_win,
    paired_verdict,
)

DEFAULT_SCOPE = "tiny1m3m"
RUN_COMMAND = "python run_experiment.py"   # same generic entrypoint the feeder uses
CONFIRM_PREFIX = "confirm"                  # queue_item id namespace for confirm jobs


def log(msg: str) -> None:
    print(f"[confirm {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# --- identity ----------------------------------------------------------------

def automation_contributor(conn) -> str:
    """The singleton automation contributor that owns daemon-written rows."""
    cur = conn.cursor()
    cur.execute(
        "insert into contributors (handle, role) values ('automation','maintainer') "
        "on conflict (handle) do update set handle=excluded.handle returning id")
    cid = cur.fetchone()[0]
    conn.commit()
    return cid


# --- champion (the control arm source of truth) ------------------------------

def current_champion(conn, scope: str) -> dict | None:
    """The live champion for this scope and its backing run's CONFIG.

    We read the config off the champion's RUN (champions.run_id -> runs.config),
    never the bare base — that fresh-from-champion config is what the baseline arm
    re-runs. If the champion run carries no config (e.g. a synthetic lineage row),
    we CANNOT build a trustworthy baseline and refuse to confirm rather than fall
    back to the bare base (that fallback is exactly the over-crediting bug)."""
    cur = conn.cursor()
    cur.execute(
        """select c.run_id, c.val_loss, r.config, r.thread_name
           from champions c
           join runs r on r.id = c.run_id
           where c.scope = %s and c.superseded_at is null
           order by c.promoted_at desc
           limit 1""",
        (scope,))
    row = cur.fetchone()
    if not row:
        return None
    return {"run_id": row[0], "val_loss": row[1], "config": row[2],
            "thread_name": row[3]}


# --- candidate selection -----------------------------------------------------

def candidates(conn, scope: str, champ_val: float, screen_band: float) -> list[dict]:
    """`done`, still-`unverified` runs ON THIS SCOPE'S THREAD that beat the pinned
    champion by more than the screen band — and are not themselves confirm-arm runs
    (those carry a `confirm-*` queue_item_id; without this filter a winning
    candidate-arm re-run would be picked up as a fresh candidate and the daemon
    would confirm forever).

    The `thread_name = scope` filter is essential: val_loss is only comparable
    WITHIN a research question. Without it the gate pulls runs from unrelated
    threads (e.g. a `lr_schedule` 10M-token study at 4.55, a `layerscale` run at
    5.54) and tries to 'confirm' them against the tiny1m3m champion — apples to
    oranges, a false confirmation. A challenger to a scope's champion must be a run
    attempting that same scope."""
    cur = conn.cursor()
    cur.execute(
        """select r.id, r.thread_name, r.name, r.config, r.final_val_loss, r.box_id
           from runs r
           where r.thread_name = %s
             and r.status = 'done'
             and r.verification = 'unverified'
             and r.final_val_loss is not null
             and r.final_val_loss < %s - %s
             and (r.queue_item_id is null or r.queue_item_id not like 'confirm-%%')
             and not exists (select 1 from confirmations cf where cf.run_id = r.id)
           order by r.final_val_loss asc""",
        (scope, champ_val, screen_band))
    return [{"id": r[0], "thread_name": r[1], "name": r[2], "config": r[3],
             "final_val_loss": r[4], "box_id": r[5]} for r in cur.fetchall()]


def gate_field(conn, scope: str, champ_val: float, screen_band: float,
               max_drop_factor: float) -> dict:
    """A snapshot of the candidate field for one scope: plausible done+unverified
    runs ON THIS SCOPE'S THREAD that beat the champion's RAW val, SPLIT by whether
    they clear the screen band. Same scoping/plausibility filters as candidates(),
    so it describes exactly the population the gate judges. Lets an idle daemon say
    precisely WHY: contenders that clear the band vs sub-band near-misses vs nothing
    at all."""
    cur = conn.cursor()
    cur.execute(
        """select r.id, r.final_val_loss
           from runs r
           where r.thread_name = %s
             and r.status='done' and r.verification='unverified'
             and r.final_val_loss is not null
             and r.final_val_loss < %s                  -- beats champion raw
             and r.final_val_loss >= %s                 -- plausible (not nonsense-low)
             and (r.queue_item_id is null or r.queue_item_id not like 'confirm-%%')
             and not exists (select 1 from confirmations cf where cf.run_id = r.id)
           order by r.final_val_loss asc""",
        (scope, champ_val, champ_val * max_drop_factor))
    rows = cur.fetchall()
    clears = [(i, v) for (i, v) in rows if (champ_val - v) > screen_band]
    sub = [(i, v) for (i, v) in rows if (champ_val - v) <= screen_band]
    return {"beat_raw": len(rows), "clears": clears,
            "closest_sub": sub[0] if sub else None}


def log_field(field: dict, champ_val: float, screen_band: float,
              *, champion_has_config: bool) -> None:
    """Turn an idle cycle into a legible signal instead of silence, naming the
    real blocker: a missing champion config vs a plateaued search vs no contender."""
    if field["clears"]:
        top_id, top_val = field["clears"][0]
        n = len(field["clears"])
        if champion_has_config:
            log(f"{n} candidate(s) clear the {screen_band} band; closest {top_id} at "
                f"{top_val:.4f} (+{champ_val - top_val:.4f}) — enqueuing/awaiting confirm.")
        else:
            log(f"{n} candidate(s) CLEAR the {screen_band} band (closest {top_id} at "
                f"{top_val:.4f}, +{champ_val - top_val:.4f}) and WOULD be confirmed — but "
                f"the champion run has no config to build the baseline arm. The blocker "
                f"is the champion config, NOT the science: point the champion at a real "
                f"config-carrying run to unblock.")
        return
    if field["closest_sub"]:
        sid, sval = field["closest_sub"]
        margin = champ_val - sval
        log(f"PLATEAU: {field['beat_raw']} run(s) beat champion {champ_val:.4f} on raw "
            f"val but NONE clear the {screen_band} band. Closest: {sid} at {sval:.4f} "
            f"(+{margin:.4f}). Admit it with --screen-band {max(margin - 1e-6, 0.0):.4f}, "
            f"or the search needs a decisively better idea, not another confirm.")
        return
    log(f"idle: no plausible run beats champion {champ_val:.4f} — no contender yet "
        f"(correct, not a fault).")


# --- confirm queue items (in-flight detection + collection) ------------------

def _confirm_qid(run_id: str, arm: str, seed: int) -> str:
    return f"{CONFIRM_PREFIX}-{run_id}-{arm}-s{seed}"


def _parse_arm_seed(qid: str, run_id: str):
    """('cand', 42) from a confirm queue_item id, or None if it isn't ours."""
    prefix = f"{CONFIRM_PREFIX}-{run_id}-"
    if not qid.startswith(prefix):
        return None
    arm, _, srest = qid[len(prefix):].partition("-s")
    if arm not in ("cand", "base") or not srest.isdigit():
        return None
    return arm, int(srest)


def confirm_jobs(conn, run_id: str) -> list[dict]:
    """The confirm queue items for one candidate, each with its latest run's val.
    LIKE can over-match (run ids contain '_' from `use_*` levers, a LIKE
    wildcard), so we re-filter on the exact prefix in Python."""
    cur = conn.cursor()
    cur.execute(
        """select q.id, q.status,
                  r.final_val_loss, r.box_id, r.status
           from queue_items q
           left join lateral (
               select final_val_loss, box_id, status
               from runs where queue_item_id = q.id
               order by created_at desc
               limit 1
           ) r on true
           where q.id like %s""",
        (f"{CONFIRM_PREFIX}-{run_id}-%",))
    out = []
    for qid, qstatus, val, box, rstatus in cur.fetchall():
        parsed = _parse_arm_seed(qid, run_id)
        if parsed is None:
            continue
        arm, seed = parsed
        out.append({"qid": qid, "qstatus": qstatus, "arm": arm, "seed": seed,
                    "val": val, "box": box, "rstatus": rstatus})
    return out


# --- phase A: enqueue a paired confirm ---------------------------------------

def _arm_config(base_config: dict, seed: int) -> dict:
    """A self-contained per-seed config: deep-copy the arm's base config and pin
    the seed. The worker ships this whole blob to the box via EXPERIMENT_CONFIG,
    so each confirm run needs zero local state."""
    cfg = json.loads(json.dumps(base_config))
    cfg["seed"] = seed
    return cfg


def enqueue_confirm(conn, candidate: dict, champ: dict, priority: int) -> int:
    """Enqueue the 6 paired confirm jobs for one candidate: 3 candidate seeds +
    3 champion-baseline seeds, matched. Tagged to the candidate's thread, mirroring
    how feeder.py enqueues. ON CONFLICT DO NOTHING + the caller's in-flight check
    make this safe to call repeatedly — it never double-enqueues."""
    run_id = candidate["id"]
    thread = candidate["thread_name"]
    cand_cfg = candidate["config"]
    champ_cfg = champ["config"]
    cur = conn.cursor()
    n = 0
    for seed in SEEDS:
        for arm, base_cfg in (("cand", cand_cfg), ("base", champ_cfg)):
            qid = _confirm_qid(run_id, arm, seed)
            cfg = _arm_config(base_cfg, seed)
            cur.execute(
                """insert into queue_items
                     (id, thread_name, name, command, status, config,
                      content_hash, gpu_class, priority)
                   values (%s,%s,%s,%s,'needs-run',%s,null,'any',%s)
                   on conflict (id) do nothing""",
                (qid, thread, qid, RUN_COMMAND, json.dumps(cfg), priority))
            n += cur.rowcount
    conn.commit()
    return n


# --- phase B: judge a finished confirm ---------------------------------------
# paired_verdict now lives in voidcheck (imported at the top) — the pure judgement
# is the platform's trust core and belongs in the property-tested library, not
# inline in the daemon. The daemon keeps only the DB orchestration below.


def judge_and_record(conn, candidate: dict, jobs: list[dict],
                     confirm_band: float, contributor_id: str) -> str:
    """All 6 jobs are terminal — compute the paired delta and write the verdict.
    Writes one `confirmations` row, flips the run's verification, and logs a
    `decisions` row — all in one transaction."""
    run_id = candidate["id"]
    v = paired_verdict(jobs, confirm_band)
    agrees, delta, cand_mean, notes = v["agrees"], v["delta"], v["cand_mean"], v["notes"]
    box = next((j["box"] for j in jobs if j["box"] is not None), None)

    verification = "confirmed" if agrees else "rejected"
    verdict = (f"{verification}: paired Δ "
               f"{('%+.4f' % delta) if delta is not None else 'n/a'} "
               f"({v['n_pairs']}/{len(SEEDS)} seeds)")

    cur = conn.cursor()
    cur.execute(
        """insert into confirmations
             (run_id, reproduced_by_box, reproduced_by, reproduced_val_loss,
              delta_from_original, agrees, notes)
           values (%s,%s,%s,%s,%s,%s,%s)""",
        (run_id, box, contributor_id, cand_mean, delta, agrees, notes))
    cur.execute(
        "update runs set verification=%s, verdict=%s where id=%s",
        (verification, verdict, run_id))
    cur.execute(
        """insert into decisions (thread_name, run_id, decision, reason, decided_by)
           values (%s,%s,%s,%s,%s)""",
        (candidate["thread_name"], run_id, verification, notes, contributor_id))
    conn.commit()
    log(f"{run_id} -> {verification.upper()} ({notes})")
    return verification


# --- one poll cycle ----------------------------------------------------------

def poll_cycle(conn, args, contributor_id: str) -> dict:
    """One pass: enqueue confirms for fresh candidates, judge finished ones.
    Returns a small summary for the caller to print."""
    champ = current_champion(conn, args.scope)
    if champ is None:
        log(f"no current champion for scope '{args.scope}' — nothing to confirm")
        return {"candidates": 0, "enqueued": 0, "judged": 0, "skipped_implausible": 0}
    if champ["config"] is None:
        log(f"champion run {champ['run_id']} has no config — cannot build a "
            f"baseline arm without falling back to the bare base; skipping cycle")
        # Still tell the operator WHERE the field stands, so a config-less champion
        # doesn't read as a dead daemon — and name the real blocker (the config).
        log_field(gate_field(conn, args.scope, champ["val_loss"], args.screen_band,
                             args.max_drop_factor),
                  champ["val_loss"], args.screen_band, champion_has_config=False)
        return {"candidates": 0, "enqueued": 0, "judged": 0, "skipped_implausible": 0}

    cand = candidates(conn, args.scope, champ["val_loss"], args.screen_band)
    enqueued, judged, skipped_implausible = 0, 0, 0
    for c in cand:
        # "Too good to be true" floor: a broken or forged nonsense-low val_loss
        # screens as the BIGGEST win and would otherwise burn a 6-run paired
        # confirm on garbage. Skip it (non-destructively — the run is left
        # untouched for a human to inspect/reject) and keep going; the real
        # candidates after it still get confirmed. This is the confirm-side guard
        # for the new untrusted-donor /runs path.
        if is_implausible_win(c["final_val_loss"], champ["val_loss"], args.max_drop_factor):
            skipped_implausible += 1
            log(f"SKIP implausible candidate {c['id']} "
                f"(val {c['final_val_loss']:.4f} vs champ {champ['val_loss']:.4f} — "
                f"more than {1 - args.max_drop_factor:.0%} better; likely broken metric "
                f"or forged report, NOT auto-confirming; needs human review)")
            continue
        jobs = confirm_jobs(conn, c["id"])
        if not jobs:
            # Phase A: no confirm in flight yet — enqueue the paired 6.
            n = enqueue_confirm(conn, c, champ, args.priority)
            enqueued += 1
            log(f"enqueued confirm for {c['id']} "
                f"(val {c['final_val_loss']:.4f} beats champ {champ['val_loss']:.4f} "
                f"by {champ['val_loss'] - c['final_val_loss']:.4f}) -> {n} jobs")
            continue
        # Phase B: confirm in flight — judge once all 6 are terminal.
        expected = 2 * len(SEEDS)
        terminal = [j for j in jobs if j["qstatus"] in ("done", "failed")]
        if len(jobs) >= expected and len(terminal) == len(jobs):
            judge_and_record(conn, c, jobs, args.confirm_band, contributor_id)
            judged += 1
        else:
            log(f"{c['id']} confirm in flight "
                f"({len(terminal)}/{len(jobs)} jobs terminal) — waiting")

    if args.auto_promote:
        # Intentional no-op stub. Promotion to champion stays a maintainer action
        # (see the separate auto-promotion issue). This flag exists only so the
        # surface is wired; it must never promote.
        log("--auto-promote is a stub and does nothing — promotion stays a "
            "maintainer action")

    # Nothing moved this cycle: say WHY (search plateaued vs no contender) instead
    # of going silent, which reads as a broken daemon. Champion has a config here
    # (we passed the early return), so any band-clearing run was already enqueued.
    if enqueued == 0 and judged == 0 and skipped_implausible == 0:
        log_field(gate_field(conn, args.scope, champ["val_loss"], args.screen_band,
                             args.max_drop_factor),
                  champ["val_loss"], args.screen_band, champion_has_config=True)

    return {"candidates": len(cand), "enqueued": enqueued, "judged": judged,
            "skipped_implausible": skipped_implausible}


# --- entry -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll loop interval seconds (default 60)")
    ap.add_argument("--scope", default=DEFAULT_SCOPE,
                    help=f"champion scope to confirm against (default {DEFAULT_SCOPE})")
    ap.add_argument("--screen-band", type=float, default=SCREEN_BAND,
                    help=f"candidate must beat champion by more than this (default {SCREEN_BAND})")
    ap.add_argument("--confirm-band", type=float, default=CONFIRM_BAND,
                    help=f"paired-mean epsilon for AGREE (default {CONFIRM_BAND})")
    ap.add_argument("--max-drop-factor", type=float, default=MAX_DROP_FACTOR,
                    help=("skip candidates below this fraction of the champion val_loss "
                          f"as too-good-to-be-true (default {MAX_DROP_FACTOR}; broken/"
                          "forged metrics never auto-consume confirm GPU)"))
    ap.add_argument("--priority", type=int, default=100,
                    help="queue priority for confirm jobs (default 100 — ahead of search)")
    ap.add_argument("--auto-promote", action="store_true",
                    help="STUB / no-op — promotion stays a maintainer action (default off)")
    args = ap.parse_args()

    def cycle() -> dict:
        conn = connect()
        try:
            cid = automation_contributor(conn)
            return poll_cycle(conn, args, cid)
        finally:
            conn.close()

    if args.once:
        s = cycle()
        log(f"once: {s['candidates']} candidate(s), enqueued {s['enqueued']}, "
            f"judged {s['judged']}, skipped-implausible {s['skipped_implausible']}")
        return 0

    log(f"confirm daemon: scope={args.scope} screen-band={args.screen_band} "
        f"confirm-band={args.confirm_band} interval={args.interval}s (ctrl-c to stop)")
    while True:
        try:
            s = cycle()
            if s["enqueued"] or s["judged"] or s["skipped_implausible"]:
                log(f"cycle: {s['candidates']} candidate(s), enqueued {s['enqueued']}, "
                    f"judged {s['judged']}, skipped-implausible {s['skipped_implausible']}")
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            break
        except Exception as e:  # noqa: BLE001
            # A transient Neon drop must never kill an unattended daemon — log and
            # retry; nothing is half-written (each phase commits its own work).
            log(f"loop error ({type(e).__name__}: {e}); retrying in {args.interval}s")
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
