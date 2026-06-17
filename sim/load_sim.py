#!/usr/bin/env python3
"""load_sim.py — concurrency + fault stress for the voidbase coordination layer.

Two jobs in one: (1) generate visible concurrent activity so the dashboard's Live
panel actually moves, and (2) hunt coordination bugs that only show up under load —
double-claims, lease reclaim, dedup, orphaned in-flight jobs. It is SYNTHETIC: no
GPU, no SSH. Jobs are command='true', tagged thread='sim', and every row it writes
is removed by `teardown`. It exercises the REAL claim SQL from worker.py.

  python3 sim/load_sim.py seed --jobs 12 --boxes 4
  python3 sim/load_sim.py workers --workers 4 --hold 4   # watch the dashboard move
  python3 sim/load_sim.py faults                          # the bug hunt
  python3 sim/load_sim.py teardown
  python3 sim/load_sim.py all                             # seed+workers+faults+report (keeps data)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402
from scripts.worker import CLAIM_SQL  # the REAL atomic claim under test  # noqa: E402

THREAD = "sim"
BUGS: list[str] = []


def bug(msg: str) -> None:
    BUGS.append(msg)
    print(f"  🐛 BUG: {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def ensure_thread(conn) -> None:
    cur = conn.cursor()
    cur.execute("insert into threads (name, hypothesis, status) "
                "values (%s,'load sim','active') on conflict (name) do nothing", (THREAD,))
    conn.commit()


def make_boxes(conn, n: int) -> list[str]:
    """n synthetic contributor+box identities so the dashboard shows many 'people'."""
    cur = conn.cursor()
    ids = []
    for i in range(n):
        h = f"sim-bot-{i}"
        cur.execute("insert into contributors (handle, role) values (%s,'contributor') "
                    "on conflict (handle) do update set handle=excluded.handle returning id", (h,))
        cid = cur.fetchone()[0]
        cur.execute("insert into boxes (contributor_id, label, gpu_class, fingerprint) "
                    "values (%s,%s,'any',%s) on conflict (contributor_id, fingerprint) "
                    "do update set label=excluded.label returning id",
                    (cid, f"{h}@sim", f"{h}:box"))
        ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def claim(conn, box_id: str, lease: int):
    cur = conn.cursor()
    cur.execute(CLAIM_SQL, {"box": box_id, "lease": lease})
    row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


# ---------------------------------------------------------------------------

def cmd_seed(jobs: int, boxes: int) -> int:
    conn = connect()
    try:
        ensure_thread(conn)
        box_ids = make_boxes(conn, boxes)
        cur = conn.cursor()
        for i in range(jobs):
            tok = uuid.uuid4().hex[:8]
            jid = f"sim-{tok}"
            # name carries the token too: queue_items has a UNIQUE (thread_name,name),
            # so re-seeding the same plain names ("sim-job-0") would collide.
            cur.execute(
                "insert into queue_items (id, thread_name, name, command, status, content_hash) "
                "values (%s,%s,%s,'true','needs-run',%s)",
                (jid, THREAD, f"sim-job-{i}-{tok}", f"hash-{jid}"))
        conn.commit()
        print(f"seeded {jobs} needs-run jobs + {boxes} boxes (thread={THREAD})")
        return 0
    finally:
        conn.close()


def cmd_workers(workers: int, hold: float, lease: int) -> int:
    """Fire M concurrent workers that claim -> running -> (hold) -> done. The hold
    keeps jobs in-flight long enough to SEE on the dashboard, and the concurrency
    is the double-claim test: every job must be claimed exactly once."""
    conn = connect()
    try:
        box_ids = [r[0] for r in _q(conn, "select b.id from boxes b join contributors c "
                                          "on c.id=b.contributor_id where c.handle like 'sim-bot-%%'")]
        if not box_ids:
            box_ids = make_boxes(conn, workers)
    finally:
        conn.close()

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(wi: int):
        c = connect()
        box = box_ids[wi % len(box_ids)]
        try:
            while True:
                jid = claim(c, box, lease)
                if not jid:
                    break
                with lock:
                    claimed.append(jid)
                cur = c.cursor()
                cur.execute("update queue_items set status='running', started_at=now() where id=%s", (jid,))
                c.commit()
                time.sleep(hold)  # simulate work — visible as 'running' on the dashboard
                # synthetic result
                rid = f"{jid}--{uuid.uuid4().hex[:6]}"
                cur.execute(
                    "insert into runs (id, queue_item_id, thread_name, name, box_id, command, "
                    "content_hash, status, verification, final_val_loss, finished_at) "
                    "values (%s,%s,%s,%s,%s,'true',%s,'done','unverified',%s,now())",
                    (rid, jid, THREAD, f"sim-run", box, f"hash-{jid}", 6.0 + (wi % 5) * 0.01))
                cur.execute("update queue_items set status='done', finished_at=now() where id=%s", (jid,))
                c.commit()
        finally:
            c.close()

    print(f"firing {workers} concurrent workers (hold={hold}s, lease={lease}s) — watch localhost:3000/voidbase")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    dupes = len(claimed) - len(set(claimed))
    print(f"claimed {len(claimed)} job(s); distinct={len(set(claimed))}")
    if dupes:
        bug(f"{dupes} job(s) claimed by more than one worker — SKIP LOCKED broke")
    else:
        ok("no double-claims under concurrency")
    return 0


def cmd_faults() -> int:
    """The bug hunt: lease reclaim, double-run-on-reclaim, dedup, orphan detection."""
    conn = connect()
    try:
        ensure_thread(conn)
        boxes = make_boxes(conn, 2)
        cur = conn.cursor()

        # --- 1. lease reclaim + double-run-on-reclaim ----------------------
        jid = f"sim-lease-{uuid.uuid4().hex[:6]}"
        cur.execute("insert into queue_items (id, thread_name, name, command, status, content_hash) "
                    "values (%s,%s,'lease-test','true','needs-run',%s)", (jid, THREAD, f"h-{jid}"))
        conn.commit()
        a = claim(conn, boxes[0], lease=2)       # box A claims with a 2s lease
        cur.execute("update queue_items set status='running' where id=%s", (a,))
        conn.commit()
        # box A "dies" — never reports. After the lease expires, box B must reclaim.
        time.sleep(3)
        b = claim(conn, boxes[1], lease=30)
        if b == jid:
            ok("expired lease reclaimed by a second worker (no job stranded)")
            # NOTE: this is also the double-run risk — if A were merely SLOW (not
            # dead) and finished after B, the job runs twice -> two runs rows.
            print("     ⚠ design note: reclaim assumes the first worker is DEAD; a job that "
                  "outlives its lease will be run twice (2 runs rows). lease must exceed max job time.")
        else:
            bug(f"expired-lease job NOT reclaimed (got {b!r}); a dead worker strands the job forever")

        # --- 2. dedup: identical content_hash should be detectable ----------
        seen = {r[0] for r in _q(conn, "select content_hash from queue_items where content_hash is not null")}
        seen |= {r[0] for r in _q(conn, "select content_hash from runs where content_hash is not null")}
        dup_hash = f"h-{jid}"  # already used above
        if dup_hash in seen:
            ok("dedup key lookup finds an already-seen content_hash")
        else:
            bug("content_hash dedup set does not contain a known-seen hash")
        # exact-match limitation, surfaced honestly:
        print("     ⚠ design note: dedup is EXACT-match on resolved config. Two people who "
              "implement the SAME idea under different flag NAMES get different hashes -> NOT "
              "deduped. Semantic twins die at maintainer review, not here.")

        # --- 3. orphan detection: a 'running' job past its lease ------------
        orphan = [r[0] for r in _q(conn,
            "select id from queue_items where status in ('claimed','running') "
            "and lease_expires_at is not null and lease_expires_at < now() and thread_name=%s", (THREAD,))]
        ok(f"orphan query found {len(orphan)} stuck in-flight job(s) past lease "
           "(this is what a monitor/watchdog should auto-requeue)")

        # --- 4. status vocabulary sanity -----------------------------------
        bad = _q(conn, "select distinct status from queue_items where status not in "
                       "('needs-run','claimed','running','done','failed','cancelled','needs-confirm')")
        if bad:
            bug(f"unexpected queue status values: {[r[0] for r in bad]}")
        else:
            ok("queue status vocabulary is clean")
        return 0
    finally:
        conn.close()


def cmd_teardown() -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("delete from runs where thread_name=%s", (THREAD,))
        nr = cur.rowcount
        cur.execute("delete from queue_items where thread_name=%s", (THREAD,))
        nq = cur.rowcount
        cur.execute("delete from boxes b using contributors c where c.id=b.contributor_id "
                    "and c.handle like 'sim-bot-%%'")
        cur.execute("delete from contributors where handle like 'sim-bot-%%'")
        cur.execute("delete from threads where name=%s", (THREAD,))
        conn.commit()
        print(f"teardown: removed {nr} runs, {nq} queue_items, sim boxes/contributors, sim thread")
        return 0
    finally:
        conn.close()


def _q(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed"); s.add_argument("--jobs", type=int, default=12); s.add_argument("--boxes", type=int, default=4)
    w = sub.add_parser("workers"); w.add_argument("--workers", type=int, default=4); w.add_argument("--hold", type=float, default=4.0); w.add_argument("--lease", type=int, default=1800)
    sub.add_parser("faults")
    sub.add_parser("teardown")
    a = sub.add_parser("all"); a.add_argument("--jobs", type=int, default=12); a.add_argument("--boxes", type=int, default=4); a.add_argument("--workers", type=int, default=4); a.add_argument("--hold", type=float, default=4.0)
    args = ap.parse_args()

    if args.cmd == "seed":
        return cmd_seed(args.jobs, args.boxes)
    if args.cmd == "workers":
        return cmd_workers(args.workers, args.hold, args.lease)
    if args.cmd == "faults":
        rc = cmd_faults()
    elif args.cmd == "teardown":
        return cmd_teardown()
    elif args.cmd == "all":
        cmd_seed(args.jobs, args.boxes)
        print("--- workers ---"); cmd_workers(args.workers, args.hold, 1800)
        print("--- faults ---"); cmd_faults()
        rc = 0
    print(f"\n=== {len(BUGS)} bug(s) found ===")
    for b in BUGS:
        print(f"  - {b}")
    return rc if 'rc' in dir() else 0


if __name__ == "__main__":
    raise SystemExit(main())
