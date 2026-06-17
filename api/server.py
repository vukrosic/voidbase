#!/usr/bin/env python3
"""voidbase local API — stdlib HTTP server over the registry.

The thin read API that localhost voidspark calls. It serves ONE endpoint
contract over either backend:

  * Postgres (Neon) when DATABASE_URL is configured (voidbase/.env or env) —
    the real distributed store, with the live `verification` and generated
    `is_paired` columns.
  * SQLite (registry/experiments.sqlite) otherwise — the legacy local store,
    zero installs, every comparison forced unpaired (legacy had no pairing).

The JSON shapes voidspark consumes are identical across both, so the front end
and the /api/voidbase proxy never change when you cut over. Selecting a backend
is purely "is DATABASE_URL set?".

  python3 api/server.py            # serves on http://localhost:8787
  VOIDBASE_PORT=9000 python3 api/server.py

Endpoints (all GET, JSON):
  /health        liveness + row counts + which backend is live
  /runs          training runs (verification + has_eval)
  /threads       hypotheses
  /comparisons   paired deltas (is_paired: real on PG, false on SQLite)
  /champions     champion history
  /ideas         backlog
  /queue         job queue
  /eval?run_id=  per-step learning curve for one run
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import database_url  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "registry" / "experiments.sqlite"

# old status vocab -> forward-compatible vocab (mirrors import_from_sqlite.py).
# Harmless on Postgres data (already remapped at import time).
RUN_STATUS = {"completed": "done", "stopped": "failed"}

# Resolved once at startup: Postgres if a connection string is configured.
PG_URL = database_url(pooled=True)
BACKEND = "postgres(neon)" if PG_URL else "sqlite(legacy)"


# --- backend-agnostic query helpers -----------------------------------------
#
# Postgres: ONE long-lived connection reused across requests, guarded by a lock.
# Opening a fresh TLS connection to Neon per query is ~0.5s each — a single
# /health (7 counts) would stall for seconds. A persistent connection makes warm
# requests sub-millisecond. Serializing with a lock is fine for a localhost,
# single-operator dashboard; swap in psycopg_pool if this ever serves many.

_pg_conn = None
_pg_lock = threading.Lock()


def _pg_connection():
    """Return the cached connection, opening it if absent. No round-trip ping —
    a dropped connection is detected lazily when a query fails (see _pg_rows),
    which reconnects and retries. Pinging here would double every endpoint's
    round-trips to a distant Neon region (the dominant cost)."""
    global _pg_conn
    import psycopg

    if _pg_conn is None or _pg_conn.closed:
        _pg_conn = psycopg.connect(PG_URL, autocommit=True)
    return _pg_conn


def _pg_rows(sql: str, params: tuple = ()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with _pg_lock:
        for attempt in (1, 2):  # one reconnect-and-retry on a dropped connection
            try:
                conn = _pg_connection()
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())
            except psycopg.OperationalError:
                global _pg_conn
                _pg_conn = None
                if attempt == 2:
                    raise
        return []


def _pg_exec(sql: str, params: tuple = ()) -> list[dict]:
    """Write helper (INSERT/UPDATE … RETURNING). Same reconnect-and-retry as
    _pg_rows; autocommit is on so each call is its own transaction. Postgres
    only — the SQLite backend is read-only for the dashboard."""
    import psycopg
    from psycopg.rows import dict_row

    with _pg_lock:
        for attempt in (1, 2):
            try:
                conn = _pg_connection()
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall()) if cur.description else []
            except psycopg.OperationalError:
                global _pg_conn
                _pg_conn = None
                if attempt == 2:
                    raise
        return []


def _sqlite_rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def rows(sql_pg: str, sql_sqlite: str | None = None, params: tuple = ()) -> list[dict]:
    """Run a query on the active backend. sql_sqlite defaults to sql_pg when the
    SQL is portable; pass both when placeholder/column dialects differ."""
    if PG_URL:
        return _pg_rows(sql_pg, params)
    return _sqlite_rows(sql_sqlite if sql_sqlite is not None else sql_pg, params)


# --- endpoint builders -------------------------------------------------------

def runs() -> list[dict]:
    have_eval = {r["run_id"] for r in rows("select distinct run_id from eval_points")}
    raw = rows("select * from runs order by created_at desc")
    out = []
    for r in raw:
        out.append({
            "id": r["id"],
            "thread_name": r.get("thread_name"),
            "name": r.get("name"),
            "seed": r.get("seed"),
            "status": RUN_STATUS.get(r.get("status"), r.get("status")),
            # Postgres carries a real verification column; legacy SQLite has none.
            "verification": r.get("verification") or "unverified",
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
    sql_pg = ("select step, tokens, val_loss, val_accuracy, val_perplexity, "
              "learning_rate, elapsed_seconds from eval_points "
              "where run_id = %s order by step asc")
    sql_sqlite = sql_pg.replace("%s", "?")
    return rows(sql_pg, sql_sqlite, (run_id,))


def comparisons() -> list[dict]:
    out = []
    for r in rows("select * from comparisons order by created_at desc"):
        out.append({
            "id": r["id"],
            "run_id": r.get("run_id"),
            "baseline_name": r.get("baseline_name"),
            "baseline_run_id": r.get("baseline_run_id"),
            "delta_val_loss": r.get("delta_val_loss"),
            "baseline_val_loss": r.get("baseline_val_loss"),
            "run_val_loss": r.get("run_val_loss"),
            "verdict": r.get("verdict"),
            # Postgres: the generated column (same seed AND box, both non-null).
            # SQLite legacy: no pairing record exists -> always false.
            "is_paired": bool(r.get("is_paired", False)),
            "created_at": r.get("created_at"),
        })
    return out


def threads() -> list[dict]:
    """Research threads for the board, enriched with two card signals:

      * run_count_last_7d — how many `runs` landed under this thread in the last
        7 days. The "is this hot" badge (🔥 N runs this week).
      * lazy auto-release — a claim whose claim_expires_at is in the past reads
        back as unclaimed (the three claim fields nulled in the response). The
        row is left untouched in the DB; the next claim overwrites it. This way
        an abandoned claim never permanently parks a thread, with no sweeper job.

    Portable across both backends. The claim columns only exist on Postgres
    (migration 0006); on the legacy SQLite store they're simply absent and the
    expiry branch is a no-op."""
    sql_pg = (
        "select t.*, "
        "(t.claim_expires_at is not null and t.claim_expires_at < now()) "
        "  as claim_expired, "
        "(select count(*) from runs r "
        "   where r.thread_name = t.name "
        "     and r.created_at > now() - interval '7 days') as run_count_last_7d "
        "from threads t order by t.priority desc")
    sql_sqlite = (
        "select t.*, "
        "(select count(*) from runs r "
        "   where r.thread_name = t.name "
        "     and r.created_at > datetime('now','-7 days')) as run_count_last_7d "
        "from threads t order by t.priority desc")
    out = []
    for r in rows(sql_pg, sql_sqlite):
        if r.pop("claim_expired", False):  # lazy auto-release on read
            r["claimed_by"] = None
            r["claimed_at"] = None
            r["claim_expires_at"] = None
        out.append(r)
    return out


def activity() -> dict:
    """Live 'what is being worked on RIGHT NOW' snapshot for the dashboard.
    Postgres-only (the distributed store): in-flight claims, active boxes, and
    runs that landed in the last 30 minutes, each tagged with the contributor +
    box so the operator can watch concurrent work stream in."""
    if not PG_URL:
        return {"backend": BACKEND, "note": "activity requires the postgres backend"}
    queue = {r["status"]: r["n"]
             for r in _pg_rows("select status, count(*) as n from queue_items group by status")}
    in_flight = _pg_rows(
        """select q.id, q.name, q.status, q.claimed_at,
                  extract(epoch from (now() - q.claimed_at))::int as age_s,
                  b.label as box, c.handle
           from queue_items q
           left join boxes b on b.id = q.claimed_by_box
           left join contributors c on c.id = b.contributor_id
           where q.status in ('claimed','running')
           order by q.claimed_at asc nulls last""")
    recent_runs = _pg_rows(
        """select r.id, r.name, r.status, r.final_val_loss, r.verification,
                  r.created_at, extract(epoch from (now() - r.created_at))::int as age_s,
                  c.handle, b.label as box
           from runs r
           left join contributors c on c.id = r.contributor_id
           left join boxes b on b.id = r.box_id
           where r.created_at > now() - interval '30 minutes'
           order by r.created_at desc""")
    contributors = _pg_rows(
        """select c.handle, c.role, count(r.id) as runs_total,
                  count(r.id) filter (where r.created_at > now() - interval '30 minutes') as runs_recent
           from contributors c left join runs r on r.contributor_id = c.id
           group by c.handle, c.role
           having count(r.id) > 0
           order by runs_total desc""")
    active_boxes = _pg_rows(
        """select b.label, c.handle,
                  count(*) filter (where q.status in ('claimed','running')) as in_flight
           from boxes b
           left join contributors c on c.id = b.contributor_id
           left join queue_items q on q.claimed_by_box = b.id
           group by b.label, c.handle
           having count(*) filter (where q.status in ('claimed','running')) > 0
           order by in_flight desc""")
    return {
        "backend": BACKEND,
        "queue": queue,
        "in_flight": in_flight,
        "active_boxes": active_boxes,
        "recent_runs": recent_runs,
        "contributors": contributors,
    }


_COUNT_TABLES = ("threads", "queue_items", "runs", "eval_points",
                 "comparisons", "decisions", "ideas")


def health() -> dict:
    # One query, one round trip: scalar subquery per table. Portable across
    # Postgres and SQLite. (Was 7 separate queries — 7× the network latency.)
    sql = "select " + ", ".join(
        f"(select count(*) from {t}) as {t}" for t in _COUNT_TABLES)
    try:
        row = rows(sql)[0]
        counts = {t: row[t] for t in _COUNT_TABLES}
        ok = True
    except Exception as e:  # noqa: BLE001 - surface any DB error to the client
        return {"ok": False, "db": BACKEND, "backend": BACKEND, "error": str(e)}
    db_label = "neon" if PG_URL else str(DB_PATH)
    return {"ok": ok, "db": db_label, "backend": BACKEND, "counts": counts}


def upsert_thread(body: dict) -> dict:
    """Create or update a research thread by name (the PK). Only the fields
    present in `body` are written; omitted fields keep their current value via
    COALESCE on conflict. Returns the resulting row."""
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("thread 'name' is required")
    cols = {
        "hypothesis": body.get("hypothesis"),
        "goal_prompt": body.get("goal_prompt"),
        "kind": body.get("kind") or "question",
        "submit_via": body.get("submit_via") or "pr",
        "repo_url": body.get("repo_url"),
        "status": body.get("status") or "active",
        "priority": int(body.get("priority") or 0),
        "summary": body.get("summary"),
    }
    out = _pg_exec(
        """
        insert into threads (name, hypothesis, goal_prompt, kind, submit_via,
                             repo_url, status, priority, summary)
        values (%(name)s, %(hypothesis)s, %(goal_prompt)s, %(kind)s, %(submit_via)s,
                %(repo_url)s, %(status)s, %(priority)s, %(summary)s)
        on conflict (name) do update set
            hypothesis  = coalesce(excluded.hypothesis,  threads.hypothesis),
            goal_prompt = coalesce(excluded.goal_prompt, threads.goal_prompt),
            kind        = excluded.kind,
            submit_via  = excluded.submit_via,
            repo_url    = coalesce(excluded.repo_url, threads.repo_url),
            status      = excluded.status,
            priority    = excluded.priority,
            summary     = coalesce(excluded.summary, threads.summary),
            updated_at  = now()
        returning *
        """,
        {"name": name, **cols},
    )
    return out[0] if out else {}


def claim_thread(body: dict) -> dict:
    """Claim a thread for a contributor — an async 'I'm on this' signal so two
    people (or two agents) don't run the same thread and waste GPU-hours. Sets a
    48h expiry; an expired claim is treated as open (see threads()), and the
    guard below lets a re-claim by the SAME handle extend, or anyone re-claim an
    expired/open thread. Single-operator localhost, so the read-then-explain on
    contention is fine — no auth (matches upsert_thread)."""
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("thread 'name' is required")
    claimed_by = (body.get("claimed_by") or "").strip()
    if not claimed_by:
        raise ValueError("'claimed_by' handle is required to claim a thread")
    out = _pg_exec(
        """
        update threads set
            claimed_by       = %(claimed_by)s,
            claimed_at       = now(),
            claim_expires_at = now() + interval '48 hours',
            updated_at       = now()
        where name = %(name)s
          and (claimed_by is null
               or claimed_by = %(claimed_by)s
               or claim_expires_at < now())
        returning *
        """,
        {"name": name, "claimed_by": claimed_by},
    )
    if out:
        return out[0]
    # No row updated: the thread is missing, or actively claimed by someone else.
    existing = _pg_rows(
        "select claimed_by, claim_expires_at from threads where name = %s", (name,))
    if not existing:
        raise ValueError(f"no such thread: {name}")
    raise ValueError(f"thread already claimed by {existing[0]['claimed_by']}")


def release_thread(body: dict) -> dict:
    """Drop a claim, returning the thread to the open queue. Clears all three
    claim fields."""
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("thread 'name' is required")
    out = _pg_exec(
        """
        update threads set
            claimed_by       = null,
            claimed_at       = null,
            claim_expires_at = null,
            updated_at       = now()
        where name = %(name)s
        returning *
        """,
        {"name": name},
    )
    if not out:
        raise ValueError(f"no such thread: {name}")
    return out[0]


ROUTES = {
    "/health": health,
    "/activity": activity,
    "/runs": runs,
    "/threads": threads,
    "/comparisons": comparisons,
    "/champions": lambda: rows("select * from champions order by promoted_at desc"),
    "/ideas": lambda: rows("select * from ideas order by created_at desc"),
    "/queue": lambda: rows("select * from queue_items order by created_at desc"),
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

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Writes the dashboard performs, all on /threads (localhost, single
        operator, no auth — see module docstring):
          * author / update a thread          (default)
          * claim a thread     action=claim    (sets claimed_by + 48h expiry)
          * release a claim    action=release  (clears the claim fields)"""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"bad json: {e}"})
            return
        if path != "/threads":
            self._send(404, {"error": "not found", "writable": ["/threads"]})
            return
        if not PG_URL:
            self._send(501, {"error": "thread writes require the Postgres backend"})
            return
        action = (body.get("action") or "").strip().lower()
        handler = {"claim": claim_thread, "release": release_thread}.get(
            action, upsert_thread)
        try:
            self._send(200, handler(body))
        except ValueError as e:  # bad input / contention — client-correctable
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/health"
        try:
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
    target = "neon postgres" if PG_URL else f"sqlite {DB_PATH}"
    print(f"voidbase api on http://127.0.0.1:{port}  (backend={BACKEND}, {target})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
