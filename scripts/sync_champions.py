#!/usr/bin/env python3
"""sync_champions.py — push the local champion LINEAGE into Neon.

champion.json (maintainer-local) is the source of truth for the record timeline:
its `lineage` array is every promotion 175→…→323 with the val it was pinned at.
Neon's `champions` table is the shared copy the dashboard reads. This syncs the
former into the latter so localhost voidspark's Records timeline pulls from Neon
instead of a local file.

`champions.run_id` is a NOT-NULL FK into `runs`, so each lineage entry needs a
backing run row too — we materialise a deterministic `champ-<idea>` run carrying
that promotion's val. Fully idempotent: re-running upserts, never duplicates.

  python3 scripts/sync_champions.py            # sync from ../llm-research-kit-scaling
  python3 scripts/sync_champions.py --repo /path/to/universe-lm
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

SCOPE = "tiny1m3m"
REPO = Path(__file__).resolve().parent.parent.parent / "llm-research-kit-scaling"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO))
    args = ap.parse_args()

    champ = json.loads((Path(args.repo) / "autoresearch" / "champion.json").read_text())
    lineage = champ.get("lineage", [])
    # The live champion's val/idea may be richer than its lineage row — trust lineage
    # order, it's append-only oldest→newest.
    if not lineage:
        print("no lineage in champion.json — nothing to sync")
        return 0

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("insert into contributors (handle, role) values ('automation','maintainer') "
                    "on conflict (handle) do update set handle=excluded.handle returning id")
        promoted_by = cur.fetchone()[0]

        # Rebuild this scope's champion history from scratch (idempotent, ordered).
        # Delete champions first (FK), then their backing champ-* runs.
        cur.execute("delete from champions where scope=%s", (SCOPE,))
        cur.execute("delete from runs where id like 'champ-%%'")

        def eff(entry: dict) -> str:
            """An entry's effective date — promotions use `promoted`, re-pins use
            `repinned`. Used for both this row's date AND the next row's supersede."""
            return entry.get("promoted") or entry.get("repinned") or champ.get("updated")

        n = len(lineage)
        for i, e in enumerate(lineage):
            idea = e["idea"]
            val = e["val"]
            promoted = eff(e)
            # superseded when the NEXT lineage entry took over; the last is current
            # (superseded_at NULL). A partial unique index allows exactly one current.
            superseded = eff(lineage[i + 1]) if i + 1 < n else None
            reason = e.get("event", f"promoted to champion at val {val}")
            run_id = f"champ-{i:02d}-{idea}"  # index-unique: an idea can recur (re-pin)

            cur.execute(
                """insert into runs (id, thread_name, name, status, verification,
                                     final_val_loss, content_hash, created_at, finished_at)
                   values (%s,'tiny1m3m search',%s,'done','confirmed',%s,%s,
                           %s::timestamptz, %s::timestamptz)""",
                (run_id, idea, val, f"champ:{idea}", promoted, promoted))
            cur.execute(
                """insert into champions (scope, run_id, val_loss, promoted_by,
                                          promoted_at, superseded_at, reason)
                   values (%s,%s,%s,%s,%s::timestamptz,%s::timestamptz,%s)""",
                (SCOPE, run_id, val, promoted_by, promoted, superseded, reason))

        conn.commit()
        cur.execute("select count(*) from champions where scope=%s", (SCOPE,))
        print(f"synced {cur.fetchone()[0]} champions for scope={SCOPE} "
              f"(current = {lineage[-1]['idea']} @ {lineage[-1]['val']})")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
