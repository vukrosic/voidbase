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
import statistics as st
import sys
import time
from pathlib import Path

os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

# Paired confirm runs the SAME three seeds on both arms (matches prior art's
# validated 42/123/7). The candidate arm and the champion-baseline arm are run
# at identical seeds so the only thing that differs is the lever under test.
SEEDS = (42, 123, 7)

DEFAULT_SCOPE = "tiny1m3m"
# Screen band: a candidate must beat the PINNED champion val by more than this on
# the cheap single-seed screen before we spend GPU on a 6-run confirm.
SCREEN_BAND = 0.02
# Confirm band: the paired 3-seed mean must beat the freshly re-run champion by
# more than this tiny epsilon AND favour the candidate at all 3 seeds to AGREE.
# (Prior art operator policy 2026-06-17: the paired same-batch design + 3/3 sign
# agreement is the noise floor, not a wide band.)
CONFIRM_BAND = 0.001

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

def candidates(conn, champ_val: float, screen_band: float) -> list[dict]:
    """`done`, still-`unverified` runs that beat the pinned champion by more than
    the screen band — and are not themselves confirm-arm runs (those carry a
    `confirm-*` queue_item_id; without this filter a winning candidate-arm re-run
    would be picked up as a fresh candidate and the daemon would confirm forever)."""
    cur = conn.cursor()
    cur.execute(
        """select r.id, r.thread_name, r.name, r.config, r.final_val_loss, r.box_id
           from runs r
           where r.status = 'done'
             and r.verification = 'unverified'
             and r.final_val_loss is not null
             and r.final_val_loss < %s - %s
             and (r.queue_item_id is null or r.queue_item_id not like 'confirm-%%')
             and not exists (select 1 from confirmations cf where cf.run_id = r.id)
           order by r.final_val_loss asc""",
        (champ_val, screen_band))
    return [{"id": r[0], "thread_name": r[1], "name": r[2], "config": r[3],
             "final_val_loss": r[4], "box_id": r[5]} for r in cur.fetchall()]


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

def paired_verdict(jobs: list[dict], confirm_band: float) -> dict:
    """Pure paired-delta judgement (no DB) — easy to unit test.

    Paired delta = candidate 3-seed mean − champion 3-seed mean over the MATCHED
    seeds (negative = candidate improves). AGREE iff we have all 3 matched pairs,
    the mean beats the band, AND every seed individually favours the candidate
    (sign-consistency is the noise floor). Returns agrees / delta / cand_mean /
    n_pairs / notes."""
    by_key = {(j["arm"], j["seed"]): j for j in jobs}
    pairs = []  # (seed, cand_val, base_val)
    for seed in SEEDS:
        c = by_key.get(("cand", seed))
        b = by_key.get(("base", seed))
        if c and b and c["val"] is not None and b["val"] is not None:
            pairs.append((seed, c["val"], b["val"]))

    if not pairs:
        return {"agrees": False, "delta": None, "cand_mean": None, "n_pairs": 0,
                "notes": ("confirm produced no paired vals — all 6 runs failed or "
                          "crashed; cannot reproduce, rejecting.")}

    cand_mean = st.mean(cv for _, cv, _ in pairs)
    base_mean = st.mean(bv for _, _, bv in pairs)
    delta = cand_mean - base_mean
    all_favor = all(cv < bv for _, cv, bv in pairs)
    complete = len(pairs) == len(SEEDS)
    agrees = complete and all_favor and (delta < -confirm_band)
    rows = "; ".join(f"s{s}: {cv:.4f} vs {bv:.4f} (Δ{cv - bv:+.4f})"
                     for s, cv, bv in pairs)
    notes = (f"paired {len(pairs)}/{len(SEEDS)} seeds | cand mean {cand_mean:.4f} "
             f"vs champ {base_mean:.4f} | Δ {delta:+.4f} | "
             f"sign {sum(cv < bv for _, cv, bv in pairs)}/{len(pairs)} favour candidate | "
             f"band {confirm_band} | {rows}")
    return {"agrees": agrees, "delta": delta, "cand_mean": cand_mean,
            "n_pairs": len(pairs), "notes": notes}


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
        return {"candidates": 0, "enqueued": 0, "judged": 0}
    if champ["config"] is None:
        log(f"champion run {champ['run_id']} has no config — cannot build a "
            f"baseline arm without falling back to the bare base; skipping cycle")
        return {"candidates": 0, "enqueued": 0, "judged": 0}

    cand = candidates(conn, champ["val_loss"], args.screen_band)
    enqueued, judged = 0, 0
    for c in cand:
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

    return {"candidates": len(cand), "enqueued": enqueued, "judged": judged}


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
            f"judged {s['judged']}")
        return 0

    log(f"confirm daemon: scope={args.scope} screen-band={args.screen_band} "
        f"confirm-band={args.confirm_band} interval={args.interval}s (ctrl-c to stop)")
    while True:
        try:
            s = cycle()
            if s["enqueued"] or s["judged"]:
                log(f"cycle: {s['candidates']} candidate(s), enqueued {s['enqueued']}, "
                    f"judged {s['judged']}")
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
