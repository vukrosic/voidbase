#!/usr/bin/env python3
"""loop_status.py — one-shot health snapshot of the autonomous research loop.

The unattended loop is four moving parts (worker / reaper / confirm_daemon on the
Mac + the GPU box) plus the Neon queue. Checking it meant a fistful of ad-hoc
queries every time — and the daemons' stdout is block-buffered to their log files,
so `tail`-ing a log can read as "stuck" when the job actually finished (this bit me
once). This reads the AUTHORITATIVE state instead: local process table for the
daemons, and the live API for box health / queue / results. One command, no DB
creds (API-only), no buffered logs.

  python3 scripts/loop_status.py              # human summary
  python3 scripts/loop_status.py --json       # machine-readable
  VOIDBASE_API=http://host:port python3 scripts/loop_status.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

API = os.environ.get("VOIDBASE_API", "http://127.0.0.1:8787")
CHAMP_SCOPE = "tiny1m3m"

# The daemons that make up the loop. (command-substring, friendly name) — the
# substring is matched against the process table; feeder is one-shot, not here.
DAEMONS = [
    ("scripts/worker.py loop", "worker"),
    ("scripts/reaper.py", "reaper"),
    ("scripts/confirm_daemon.py", "confirm_daemon"),
]


def _get(path: str, timeout: int = 12):
    """GET a JSON endpoint, or return None on any failure (the tool must degrade,
    never crash — a status check that errors is worse than useless)."""
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001
        return None


def _procs() -> str:
    try:
        return subprocess.run(["ps", "axo", "pid,command"], capture_output=True,
                              text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return ""


def daemon_status(ps_text: str) -> list[dict]:
    """For each loop daemon, whether exactly one copy is running — and flag
    duplicates (two confirm_daemons race on enqueues; a prior-session orphan is
    the usual cause)."""
    out = []
    for needle, name in DAEMONS:
        pids = [ln.split(None, 1)[0] for ln in ps_text.splitlines()
                if needle in ln and "loop_status" not in ln and " grep " not in ln]
        out.append({"name": name, "count": len(pids), "pids": pids,
                    "ok": len(pids) == 1})
    return out


def box_health(health: dict | None) -> list[dict]:
    """Per-box liveness from /health. heartbeat_age_s under ~90s = a live worker is
    pinging it; a large/None age = the box is dark (or never started)."""
    if not health:
        return []
    out = []
    for b in health.get("boxes", []):
        age = b.get("heartbeat_age_s")
        out.append({"label": b.get("label"), "status": b.get("status"),
                    "heartbeat_age_s": age,
                    "live": isinstance(age, (int, float)) and age < 90})
    return out


def collect() -> dict:
    ps_text = _procs()
    health = _get("/health")
    activity = _get("/activity")
    gate = _get(f"/gate?scope={CHAMP_SCOPE}")

    champ_val = None
    if gate and gate.get("champion"):
        champ_val = gate["champion"].get("val_loss")

    queue = (activity or {}).get("queue", {}) if activity else {}
    in_flight = (activity or {}).get("in_flight", []) if activity else []

    # Recent results with the (unpaired) delta vs champion — a quick "is anything
    # beating the champion" read. Unpaired, so a >band delta is a LEAD to confirm,
    # not a verdict (the gate/confirm_daemon own the real judgement).
    recent = []
    for r in (activity or {}).get("recent_runs", []) if activity else []:
        v = r.get("final_val_loss")
        delta = round(champ_val - v, 4) if (champ_val and v) else None
        recent.append({"name": r.get("name"), "status": r.get("status"),
                       "val_loss": v, "delta": delta})

    return {
        "api": API,
        "daemons": daemon_status(ps_text),
        "boxes": box_health(health),
        "queue": queue,
        "in_flight": [{"name": j.get("name"), "box": j.get("box"),
                       "age_s": j.get("age_s")} for j in in_flight],
        "champion": {"scope": CHAMP_SCOPE, "val_loss": champ_val},
        "gate_blocker": (gate or {}).get("blocker"),
        "gate_clears": [c.get("name") for c in (gate or {}).get("clears", [])],
        "recent_runs": recent[:8],
        "api_reachable": health is not None,
    }


def _fmt(s: dict) -> str:
    L = []
    ok = lambda b: "OK " if b else "!! "  # noqa: E731
    L.append(f"voidbase loop status  ({s['api']})")
    if not s["api_reachable"]:
        L.append("  !! API UNREACHABLE — start it: cd voidbase && python3 api/server.py")
    L.append("  daemons:")
    for d in s["daemons"]:
        extra = "" if d["ok"] else f"  <-- expected 1, found {d['count']} {d['pids']}"
        L.append(f"    {ok(d['ok'])}{d['name']:16s} x{d['count']}{extra}")
    L.append("  boxes:")
    for b in s["boxes"]:
        age = b["heartbeat_age_s"]
        age_s = f"{age}s" if age is not None else "never"
        L.append(f"    {ok(b['live'])}{(b['label'] or '?')[:34]:34s} {b['status']:8s} hb {age_s}")
    q = s["queue"]
    L.append(f"  queue: needs-run={q.get('needs-run', 0)} running={q.get('running', 0)} "
             f"done={q.get('done', 0)} failed={q.get('failed', 0)}")
    for j in s["in_flight"]:
        L.append(f"    -> running {j['name']} on {j['box']} ({j['age_s']}s)")
    cv = s["champion"]["val_loss"]
    L.append(f"  champion ({s['champion']['scope']}): {cv}")
    if s["gate_clears"]:
        L.append(f"  GATE CLEARS (leads): {', '.join(s['gate_clears'])}")
    elif s["gate_blocker"]:
        L.append(f"  gate blocker: {s['gate_blocker'][:70]}")
    if s["recent_runs"]:
        L.append("  recent results (delta vs champ; unpaired = lead not verdict):")
        for r in s["recent_runs"]:
            d = r["delta"]
            mark = " <== >band LEAD" if (d and d > 0.01) else ""
            dv = f"{d:+.4f}" if d is not None else "  ?   "
            L.append(f"    {(r['name'] or '?')[:38]:38s} {str(r['val_loss']):8s} {dv}{mark}")
    return "\n".join(L)


def main() -> int:
    as_json = "--json" in sys.argv
    s = collect()
    print(json.dumps(s, indent=2, default=str) if as_json else _fmt(s))
    # exit non-zero if the loop is unhealthy (API down or a daemon miscount) so a
    # wrapper/cron can alert on it.
    healthy = s["api_reachable"] and all(d["ok"] for d in s["daemons"])
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
