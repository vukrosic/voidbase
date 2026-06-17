#!/usr/bin/env python3
"""reaper.py — requeue jobs stranded by a dead GPU box (graceful recovery).

If a box drops mid-run today, its job hangs in 'claimed'/'running' forever and
the loop silently starves. The reaper is the self-heal: every ~60s it finds the
stranded jobs and hands them back to the queue.

A job is stranded when EITHER signal fires:
  * its lease expired   — lease_expires_at < now() (a dead worker never renewed)
  * its box went dark    — the box's last_heartbeat is older than the timeout
                           (default 90s), or it never pinged at all.

For each stranded job, in ONE transaction:
  * reset the queue_item to 'needs-run' and clear claimed_by_box / claimed_at /
    lease_expires_at / started_at (so the next worker can claim it cleanly);
  * bump that box's failed_run_count and mark the box 'offline'.
Every requeue is logged with the reason.

Idempotent + flap-proof: a requeued job is now 'needs-run', so the very next
sweep does not see it again — the reaper can never requeue the same job twice or
flap a box's status in a loop. The matching live signal (the worker's heartbeat,
POST box_heartbeat) flips a box back to 'healthy' as soon as it pings again.

  python3 scripts/reaper.py once          # one sweep, exit
  python3 scripts/reaper.py loop          # sweep every VOIDBASE_REAPER_INTERVAL s
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Bound every libpq connect so a Neon hang can never wedge the sweep loop.
os.environ.setdefault("PGCONNECT_TIMEOUT", "10")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

# A box is "dark" when its last heartbeat is older than this. Must exceed the
# worker's heartbeat interval (30s) with margin so a single missed ping doesn't
# trigger a requeue — 90s = three missed beats.
HEARTBEAT_TIMEOUT = int(os.environ.get("VOIDBASE_BOX_HEARTBEAT_TIMEOUT", "90"))
SWEEP_INTERVAL = int(os.environ.get("VOIDBASE_REAPER_INTERVAL", "60"))


def log(msg: str) -> None:
    print(f"[reaper {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# One transaction does the whole sweep: pick the stranded jobs, requeue them,
# and bump/offline their boxes. CTEs keep it atomic and collision-proof.
SWEEP_SQL = """
with dead as (
    -- Freshness is the most recent of (last heartbeat, claim time): a box that
    -- never pinged is judged by how long ago it CLAIMED, so a just-claimed job
    -- whose first beat hasn't landed yet is not reaped — only one genuinely
    -- silent for the whole timeout is. This is what keeps the sweep from flapping.
    select q.id, q.name, q.claimed_by_box,
           (q.lease_expires_at is not null and q.lease_expires_at < now()) as lease_expired,
           (q.claimed_by_box is not null
            and coalesce(b.last_heartbeat, q.claimed_at)
                < now() - make_interval(secs => %(timeout)s)) as box_dark
    from queue_items q
    left join boxes b on b.id = q.claimed_by_box
    where q.status in ('claimed', 'running')
      and (
        (q.lease_expires_at is not null and q.lease_expires_at < now())
        or (q.claimed_by_box is not null
            and coalesce(b.last_heartbeat, q.claimed_at)
                < now() - make_interval(secs => %(timeout)s))
      )
),
requeued as (
    update queue_items q
    set status = 'needs-run',
        claimed_by_box = null,
        claimed_at = null,
        lease_expires_at = null,
        started_at = null
    from dead
    where q.id = dead.id
    returning dead.id, dead.name, dead.claimed_by_box, dead.lease_expired, dead.box_dark
),
bumped as (
    update boxes b
    set failed_run_count = b.failed_run_count + cnt.n,
        status = 'offline'
    from (select claimed_by_box as box_id, count(*) as n
          from requeued
          where claimed_by_box is not null
          group by claimed_by_box) cnt
    where b.id = cnt.box_id
    returning b.id
)
select id, name, claimed_by_box, lease_expired, box_dark from requeued;
"""


def sweep(conn) -> int:
    """Requeue every stranded job in one transaction; log each. Returns count."""
    cur = conn.cursor()
    cur.execute(SWEEP_SQL, {"timeout": HEARTBEAT_TIMEOUT})
    rows = cur.fetchall()
    conn.commit()
    for qid, name, box_id, lease_expired, box_dark in rows:
        reasons = []
        if lease_expired:
            reasons.append("lease expired")
        if box_dark:
            reasons.append(f"box heartbeat stale (>{HEARTBEAT_TIMEOUT}s)")
        log(f"REQUEUED {qid} ({name}) -> needs-run [{', '.join(reasons)}]; "
            f"box {box_id} marked offline, failed_run_count++")
    if rows:
        log(f"sweep requeued {len(rows)} stranded job(s)")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["once", "loop"], nargs="?", default="once")
    ap.add_argument("--interval", type=int, default=SWEEP_INTERVAL,
                    help="loop: seconds between sweeps")
    args = ap.parse_args()

    def one_sweep() -> None:
        try:
            conn = connect()
        except Exception as e:  # noqa: BLE001 — a Neon blip must not kill the loop
            log(f"DB unreachable ({e}) — skipping this sweep")
            return
        try:
            sweep(conn)
        except Exception as e:  # noqa: BLE001
            log(f"sweep failed ({e}) — skipping this cycle")
        finally:
            conn.close()

    if args.mode == "once":
        one_sweep()
        return 0

    log(f"reaper sweeping every {args.interval}s "
        f"(heartbeat timeout {HEARTBEAT_TIMEOUT}s, ctrl-c to stop)")
    while True:
        try:
            one_sweep()
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
