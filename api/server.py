#!/usr/bin/env python3
"""voidbase local API — stdlib HTTP server over the registry.

The thin read API that localhost voidspark calls. It serves the SAME endpoint
contract the future Postgres/Supabase backend will, but reads the live SQLite
registry today (registry/experiments.sqlite). When Supabase is stood up, only
`_connect`/the query layer changes — the JSON shapes voidspark consumes stay put.

Stdlib only (http.server + sqlite3) so it runs with zero installs:

  python3 api/server.py            # serves on http://localhost:8787
  VOIDBASE_PORT=9000 python3 api/server.py

Endpoints (all GET, JSON):
  /health        liveness + row counts
  /runs          training runs (forward-compatible: verification, is_paired)
  /threads       hypotheses
  /comparisons   paired deltas (is_paired computed; legacy rows -> false)
  /champions     champion history (empty until the new schema lands)
  /ideas         backlog
  /queue         job queue
"""
from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path(__file__).resolve().parent.parent / "registry" / "experiments.sqlite"

# old status vocab -> forward-compatible vocab (mirrors import_from_sqlite.py)
RUN_STATUS = {"completed": "done", "stopped": "failed"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql)]


def runs() -> list[dict]:
    with _connect() as conn:
        have_eval = {
            row[0] for row in conn.execute(
                "select distinct run_id from eval_points")
        }
        rows = [dict(r) for r in conn.execute(
            "select * from runs order by created_at desc")]
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "thread_name": r.get("thread_name"),
            "name": r.get("name"),
            "seed": r.get("seed"),
            "status": RUN_STATUS.get(r.get("status"), r.get("status")),
            # legacy rows were never independently reproduced -> unverified
            "verification": "unverified",
            "verdict": r.get("verdict"),
            "final_val_loss": r.get("final_val_loss"),
            "tokens_seen": r.get("tokens_seen"),
            "git_commit": r.get("git_commit"),
            "git_branch": r.get("git_branch"),
            "created_at": r.get("created_at"),
            "finished_at": r.get("finished_at"),
            "has_eval": r["id"] in have_eval,
        })
    return out


def eval_points(run_id: str) -> list[dict]:
    """The per-step learning curve for one run, oldest step first."""
    if not run_id:
        return []
    with _connect() as conn:
        cur = conn.execute(
            "select step, tokens, val_loss, val_accuracy, val_perplexity, "
            "learning_rate, elapsed_seconds from eval_points "
            "where run_id = ? order by step asc", (run_id,))
        return [dict(r) for r in cur]


def comparisons() -> list[dict]:
    out = []
    for r in _rows("select * from comparisons order by created_at desc"):
        # legacy comparisons carry no seed/box, so is_paired is false: their
        # deltas are NOT trustworthy signal (the whole point of the new schema).
        out.append({
            "id": r["id"],
            "run_id": r.get("run_id"),
            "baseline_name": r.get("baseline_name"),
            "baseline_run_id": r.get("baseline_run_id"),
            "delta_val_loss": r.get("delta_val_loss"),
            "baseline_val_loss": r.get("baseline_val_loss"),
            "run_val_loss": r.get("run_val_loss"),
            "verdict": r.get("verdict"),
            "is_paired": False,
            "created_at": r.get("created_at"),
        })
    return out


def health() -> dict:
    counts = {}
    try:
        with _connect() as conn:
            for t in ("threads", "queue_items", "runs", "eval_points",
                      "comparisons", "decisions", "ideas"):
                counts[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
        ok = True
    except Exception as e:  # noqa: BLE001 - surface any DB error to the client
        return {"ok": False, "db": str(DB_PATH), "error": str(e)}
    return {"ok": ok, "db": str(DB_PATH), "exists": DB_PATH.exists(),
            "backend": "sqlite(legacy)", "counts": counts}


ROUTES = {
    "/health": health,
    "/runs": runs,
    "/threads": lambda: _rows("select * from threads order by priority desc"),
    "/comparisons": comparisons,
    "/champions": lambda: [],  # new-schema table; empty until cutover
    "/ideas": lambda: _rows("select * from ideas order by created_at desc"),
    "/queue": lambda: _rows("select * from queue_items order by created_at desc"),
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/health"
        try:
            # /eval?run_id=<id> — the per-step curve for one run
            if path == "/eval":
                run_id = parse_qs(parsed.query).get("run_id", [""])[0]
                self._send(200, eval_points(run_id))
                return
            handler = ROUTES.get(path)
            if handler is None:
                routes = sorted([*ROUTES, "/eval?run_id="])
                self._send(404, {"error": "not found", "routes": routes})
                return
            self._send(200, handler())
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, *args) -> None:  # quiet
        pass


def main() -> None:
    port = int(os.environ.get("VOIDBASE_PORT", "8787"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"voidbase api on http://127.0.0.1:{port}  (db={DB_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
