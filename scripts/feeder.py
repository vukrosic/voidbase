#!/usr/bin/env python3
"""feeder.py — fill the Neon queue with config-row experiments (the other half of
mass automation; the worker is the first half).

An experiment is a JSON of overrides on top of the champion. The feeder:
  1. reads the champion base (research repo's autoresearch/champion.json),
  2. enumerates candidate STRUCTURAL mechanism flags (default-OFF `use_*` in the
     config) — filtering out optimizer/HP flags to honour RULE 0 (novel
     architecture, never sweep LR/wd/momentum/batch/optimizer),
  3. resolves each as champion + {flag: true}, hashes the resolved config,
  4. DEDUPS against everything already tried (runs.content_hash) or queued
     (queue_items.content_hash) — the whole point of config-as-data: "has anyone
     tried this?" is one indexed lookup, not a diff of opaque code,
  5. enqueues the novel ones as needs-run rows the worker then drains.

  python3 scripts/feeder.py --limit 20            # single-flag levers
  python3 scripts/feeder.py --mode pairs --limit 30 # combinatorial stacks (scale)
  python3 scripts/feeder.py --dry                  # show what WOULD enqueue
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402
# content_hash now lives in the pure voidconfig lib so feeder and the API's
# POST /queue_items hash identically (re-exported here, like confirm_daemon
# re-exports from voidcheck, so existing importers — enqueue.py — keep working).
from voidconfig import content_hash  # noqa: E402,F401

DEFAULT_REPO = Path("/Users/vukrosic/my-life/llm-research-kit-scaling")
THREAD = "tiny1m3m"
RUN_COMMAND = "python run_experiment.py"

# RULE 0: structural mechanisms only. Any flag whose name contains one of these
# optimizer / hyperparameter tokens is excluded — those are the axes the
# operator explicitly closed (LR / wd / momentum / batch / optimizer swaps).
OPTIMIZER_DENY = (
    "adabelief", "adamp", "adan", "adapnm", "adashift", "adafactor", "adagrad",
    "adamw", "came", "dadapt", "prodigy", "lamb", "lars", "lion", "sophia",
    "novograd", "shampoo", "ranger", "radam", "nadam", "sgd", "rmsprop",
    "cautious", "lookahead", "_sam", "gsam", "schedule_free", "muon_",
    "_lr", "weight_decay", "_wd", "momentum", "warmup", "batch_size",
    "grad_accum", "schedule", "ema_opt",
)


def is_structural(flag: str) -> bool:
    return not any(tok in flag for tok in OPTIMIZER_DENY)


def candidate_flags(repo: Path) -> list[str]:
    """Default-OFF `use_*` mechanism flags in the config, minus optimizer/HP."""
    cfg = (repo / "configs" / "llm_config.py").read_text()
    flags = sorted(set(re.findall(r"\b(use_[a-z0-9_]+)\s*:\s*bool\s*=\s*False", cfg)))
    return [f for f in flags if is_structural(f)]


def champion_base(repo: Path) -> dict:
    champ = json.loads((repo / "autoresearch" / "champion.json").read_text())
    fields = {f: True for f in champ.get("flags", [])}
    fields.update(champ.get("config_overrides", {}))
    return {
        "config_class": champ.get("config_class", "configs.llm_config.Tiny1M3MAlibiConfig"),
        "env": dict(champ.get("env", {})),
        "fields": fields,
        "seed": champ.get("seed", 42),
        "champ_flags": set(champ.get("flags", [])),
    }


def already_seen(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("select content_hash from runs where content_hash is not null")
    seen = {r[0] for r in cur.fetchall()}
    cur.execute("select content_hash from queue_items where content_hash is not null")
    seen |= {r[0] for r in cur.fetchall()}
    return seen


def make_experiment(base: dict, flags: list[str]) -> dict:
    """champion + the given mechanism flag(s) set True → a SELF-CONTAINED config
    row. The row carries the FULL resolved config (champion base already merged
    in), so a worker box needs zero local champion state — it just applies it.
    `lever` records which flag(s) this experiment adds, for the human/label."""
    fields = {**base["fields"], **{f: True for f in flags}}
    env = base["env"]
    resolved = {
        "config_class": base["config_class"],
        "env": env,
        "fields": fields,
        "seed": base["seed"],
        "dataset_path": "processed_data/pretrain_1B",
        "lever": "+".join(flags),
    }
    return {
        "config": resolved,           # the self-contained payload stored on the row
        "content_hash": content_hash(env, fields),
        "lever": "+".join(flags),
    }


def gen(base: dict, flags: list[str], mode: str):
    if mode == "single":
        for f in flags:
            yield make_experiment(base, [f])
    elif mode == "pairs":
        for a, b in itertools.combinations(flags, 2):
            yield make_experiment(base, [a, b])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--mode", choices=["single", "pairs"], default="single")
    ap.add_argument("--limit", type=int, default=20, help="max NEW rows to enqueue")
    ap.add_argument("--priority", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="show what would enqueue, write nothing")
    args = ap.parse_args()
    repo = Path(args.repo)

    base = champion_base(repo)
    flags = candidate_flags(repo)
    print(f"[feeder] {len(flags)} structural candidate flags (optimizer/HP filtered out)", file=sys.stderr)

    conn = connect()
    try:
        seen = already_seen(conn)
        cur = conn.cursor()
        cur.execute(
            "insert into threads (name, hypothesis, status) values (%s,'tiny1m3m search','active') "
            "on conflict (name) do nothing", (THREAD,))
        conn.commit()

        enqueued, skipped = 0, 0
        for exp in gen(base, flags, args.mode):
            if enqueued >= args.limit:
                break
            if exp["content_hash"] in seen:
                skipped += 1
                continue
            seen.add(exp["content_hash"])
            qid = f"auto-{exp['lever']}-{exp['content_hash'][:8]}"
            if args.dry:
                print(f"  WOULD ENQUEUE {qid}")
                enqueued += 1
                continue
            cur.execute(
                """insert into queue_items
                     (id, thread_name, name, command, status, config, content_hash,
                      gpu_class, priority)
                   values (%s,%s,%s,%s,'needs-run',%s,%s,'any',%s)
                   on conflict (id) do nothing""",
                (qid, THREAD, exp["lever"], RUN_COMMAND,
                 json.dumps(exp["config"]), exp["content_hash"], args.priority),
            )
            enqueued += 1
        if not args.dry:
            conn.commit()
        print(f"[feeder] enqueued {enqueued} new, skipped {skipped} already-tried "
              f"({'DRY' if args.dry else 'committed'})", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
