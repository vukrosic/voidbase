#!/usr/bin/env python3
"""voidbase API — backend-agnostic DB plumbing + resolved runtime config.

The one place that knows HOW to talk to the store. Everything above it (reads.py,
writes.py, server.py) calls `rows()` / `_pg_rows()` / `_pg_exec()` and never
touches a driver. Two backends behind one contract:

  * Postgres (Neon) when DATABASE_URL is configured — the real distributed store,
    a single long-lived connection reused across requests under a lock.
  * SQLite (registry/experiments.sqlite) otherwise — the legacy local store, a
    fresh read-only connection per query, zero installs.

Selecting a backend is purely "is DATABASE_URL set?" (PG_URL). The JSON shapes the
callers build are identical across both, so the front end never changes on cutover.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import database_url  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "registry" / "experiments.sqlite"

# old status vocab -> forward-compatible vocab (mirrors import_from_sqlite.py).
# Harmless on Postgres data (already remapped at import time).
RUN_STATUS = {"completed": "done", "stopped": "failed"}

# Resolved once at startup: Postgres if a connection string is configured.
PG_URL = database_url(pooled=True)
BACKEND = "postgres(neon)" if PG_URL else "sqlite(legacy)"

# How long a claimed job is leased before the reaper may reclaim it (Voidrunner
# /claim). Matches scripts/worker.py's LEASE_SECONDS so the operator-dispatcher
# and donor-runner agree on lease length.
LEASE_SECONDS = int(os.environ.get("VOIDBASE_LEASE_SECONDS", "1800"))

# The localhost dev-bypass (no-token writes from 127.0.0.1 act as 'automation')
# is safe ONLY when the server is genuinely reached over loopback. Behind a
# reverse proxy every request's client address is the proxy's 127.0.0.1, which
# would hand the bypass to the whole internet. A public deployment MUST set
# VOIDBASE_REQUIRE_AUTH=1 — then a valid bearer token is required even from
# loopback and the proxy can't launder anonymous writes. Default off so the
# single-operator localhost workflow (worker.py, the dashboard) is unchanged.
REQUIRE_AUTH = os.environ.get("VOIDBASE_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")


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
