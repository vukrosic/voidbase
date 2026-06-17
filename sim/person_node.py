#!/usr/bin/env python3
"""person_node.py — a single independent contributor "node".

This is what a YouTube viewer with a GPU + an AI runs. It is NOT the maintainer
worker: it does not claim from the shared queue. The contributor invents one
experiment, trains it on THEIR OWN GPU box over SSH, and posts the result to the
shared leaderboard (Neon) under their own contributor identity, born
`unverified`. The maintainer re-runs winners on the reference box to make them
official — this node never touches the champion or anyone else's work.

  python sim/person_node.py --handle ada --idea "per-head temperature" \
      --config '{"fields":{"use_head_temp": true}}' \
      --box-host 5.6.7.8 --box-port 41999

  # --dry  : build+validate the config on the box, no training, nothing recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

THREAD = "tiny1m3m"  # the shared champion thread so the run shows on the same board

_VAL = re.compile(r"Final Val Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_TRAIN = re.compile(r"Final Train Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_ACC = re.compile(r"Final Val Accuracy[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)


def _last(rx, text):
    m = rx.findall(text or "")
    return float(m[-1]) if m else None


def content_hash(config: dict) -> str:
    blob = json.dumps({"env": config.get("env", {}), "fields": config.get("fields", {})},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def identity(conn, handle: str, box_label: str):
    """This contributor + their box, keyed by handle. role='contributor' — only
    the maintainer is 'maintainer', and only the maintainer can confirm a win."""
    cur = conn.cursor()
    cur.execute(
        "insert into contributors (handle, role) values (%s,'contributor') "
        "on conflict (handle) do update set handle=excluded.handle returning id",
        (handle,))
    cid = cur.fetchone()[0]
    cur.execute(
        "insert into boxes (contributor_id, label, gpu_class, fingerprint) "
        "values (%s,%s,'any',%s) on conflict (contributor_id, fingerprint) "
        "do update set label=excluded.label returning id",
        (cid, box_label, f"{handle}:box"))
    bid = cur.fetchone()[0]
    conn.commit()
    return cid, bid


def run_on_box(host, port, user, repo, python, config, dry):
    cmd = f"cd {shlex.quote(repo)} && "
    cmd += f"EXPERIMENT_CONFIG={shlex.quote(json.dumps(config))} "
    cmd += f"{shlex.quote(python)} run_experiment.py"
    if dry:
        cmd += " --dry"
    ssh = ["ssh", "-p", str(port), "-o", "ConnectTimeout=20",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
           f"{user}@{host}", cmd]
    proc = subprocess.run(ssh, capture_output=True, text=True, timeout=1800)
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or ""), cmd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True, help="your contributor name, e.g. ada")
    ap.add_argument("--idea", required=True, help="one-line description of your mechanism")
    ap.add_argument("--config", required=True, help="EXPERIMENT_CONFIG json (or @file)")
    ap.add_argument("--name", default=None, help="run name (defaults to the lever/flag)")
    ap.add_argument("--box-host", required=True)
    ap.add_argument("--box-port", default="22")
    ap.add_argument("--box-user", default="root")
    ap.add_argument("--box-repo", default="/root/universe-lm")
    ap.add_argument("--box-python", default="/venv/main/bin/python")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg_raw = args.config
    if cfg_raw.startswith("@"):
        cfg_raw = Path(cfg_raw[1:]).read_text()
    config = json.loads(cfg_raw)
    fields = config.get("fields", {})
    lever = "+".join(k for k, v in fields.items() if v) or "champion"
    name = args.name or lever

    print(f"[{args.handle}] training '{name}' on {args.box_host} ...", file=sys.stderr)
    rc, out, cmd = run_on_box(args.box_host, args.box_port, args.box_user,
                              args.box_repo, args.box_python, config, args.dry)
    val, train, acc = _last(_VAL, out), _last(_TRAIN, out), _last(_ACC, out)
    ok = rc == 0 and (not args.dry or "DRY_OK" in out)

    if args.dry:
        print(f"[{args.handle}] DRY {'OK' if ok else 'FAIL'} (rc={rc}); nothing recorded")
        return 0 if ok else 1

    conn = connect()
    try:
        cid, bid = identity(conn, args.handle, f"{args.handle}@{args.box_host}")
        run_id = f"{args.handle}-{lever}--{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute(
            """insert into runs (id, thread_name, name, contributor_id, box_id,
                                 command, config, content_hash, status, verification,
                                 verdict, final_val_loss, final_train_loss,
                                 final_val_accuracy, finished_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'unverified',%s,%s,%s,%s,now())""",
            (run_id, THREAD, name, cid, bid, cmd, json.dumps(config),
             content_hash(config), "done" if ok else "failed",
             args.idea, val, train, acc))
        conn.commit()
    finally:
        conn.close()
    print(f"[{args.handle}] posted run {run_id}: val_loss={val} acc={acc} "
          f"(status={'done' if ok else 'failed'}, unverified)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
