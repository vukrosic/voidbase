#!/usr/bin/env python3
"""enqueue.py — hand-enqueue ONE config-row experiment (any lever, not just flags).

feeder.py auto-generates boolean `use_*` flag experiments. But many structural
levers are string-keyed (out_op, resid_mode) or env-driven — feeder can't express
them. This is the deliberate single-experiment primitive: it builds the SAME
self-contained config row feeder builds (champion base merged in, content_hash
dedup, full payload on the row so the box needs zero local champion state) for an
arbitrary set of field/env overrides.

  # a string-keyed structural lever on the attention-output choke point:
  python3 scripts/enqueue.py --lever attn-out-headmix-lr1 --field out_op=headmix_lowrank1

  # an env lever, multiple overrides, a label:
  python3 scripts/enqueue.py --lever my-thing --field resid_mode=branch_gain --env FOO=bar

Mirrors feeder.champion_base / make_experiment / content_hash EXACTLY so a row from
here is indistinguishable from an auto-fed one (same dedup space, same worker path).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402
from scripts.feeder import champion_base, content_hash, already_seen  # reuse the real logic  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent / "llm-research-kit-scaling"
THREAD = "tiny1m3m search"


def _coerce(v: str):
    """'true'->True, '0.9'->0.9, '3'->3, else the string. Matches how a human would
    mean a config field value on the CLI."""
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lever", required=True, help="human label for this experiment")
    ap.add_argument("--field", action="append", default=[], metavar="K=V",
                    help="a config dataclass field override (repeatable)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="an env override (repeatable)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--priority", type=int, default=10)
    args = ap.parse_args()

    base = champion_base(Path(args.repo))
    fields = dict(base["fields"])
    for kv in args.field:
        k, _, v = kv.partition("=")
        fields[k] = _coerce(v)
    env = dict(base["env"])
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v
    seed = args.seed if args.seed is not None else base["seed"]

    resolved = {
        "config_class": base["config_class"],
        "env": env,
        "fields": fields,
        "seed": seed,
        "dataset_path": "processed_data/pretrain_1B",
        "lever": args.lever,
    }
    chash = content_hash(env, fields)

    conn = connect()
    try:
        if chash in already_seen(conn):
            print(f"DEDUP: content_hash {chash} already run or queued — skipping enqueue.")
            return 0
        cur = conn.cursor()
        cur.execute("insert into threads (name, hypothesis, status) values (%s,'tiny1m3m search','active') "
                    "on conflict (name) do nothing", (THREAD,))
        qid = f"man-{args.lever}-{chash[:8]}"
        cur.execute(
            """insert into queue_items
                 (id, thread_name, name, command, status, config, content_hash, priority)
               values (%s,%s,%s,'python run_experiment.py','needs-run',%s,%s,%s)""",
            (qid, THREAD, args.lever, json.dumps(resolved), chash, args.priority))
        conn.commit()
        print(f"ENQUEUED {qid}")
        print(f"  content_hash: {chash}")
        print(f"  lever: {args.lever}")
        print(f"  fields delta: {[kv for kv in args.field]}  env delta: {[kv for kv in args.env]}  seed: {seed}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
