#!/usr/bin/env python3
"""voidbase API — backend-agnostic DB plumbing + resolved runtime config.

The one place that knows HOW to talk to the store. Everything above it (reads.py,
writes.py, server.py) calls `rows()` / `_pg_rows()` / `_pg_exec()` and never
touches a driver. Two backends behind one contract:

  * Postgres (Neon) when DATABASE_URL is configured — the real distributed store,
    served from a bounded connection pool so concurrent multi-writer requests run
    in parallel (one connection each) instead of serializing on one.
  * SQLite (registry/experiments.sqlite) otherwise — the legacy local store, a
    fresh read-only connection per query, zero installs.

Selecting a backend is purely "is DATABASE_URL set?" (PG_URL). The JSON shapes the
callers build are identical across both, so the front end never changes on cutover.
"""
from __future__ import annotations

import os
import queue
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
# Postgres: a BOUNDED POOL of connections, one borrowed per query.
#
# This replaced a single long-lived connection behind a global lock. That was
# fine for a single-operator dashboard, but the platform is multi-writer by
# design (concurrent Voidrunner /claim+/runs and Voidmind /ideas from many donor
# boxes, served by a ThreadingHTTPServer). A global lock serialized every one of
# those onto one connection — the API became the bottleneck the multi-writer
# Postgres backend exists to avoid.
#
# The pool is a fixed set of slots in a thread-safe queue; each request borrows a
# slot, runs its query on its OWN connection (so requests run concurrently), and
# returns it. The bound matters two ways: it lets N requests proceed in parallel
# up to N, AND it caps how many Neon/PgBouncer connections an unbounded thread
# swarm can open — get(timeout) applies backpressure instead of exhausting Neon.
# Connections open lazily (a slot starts empty) and re-open on drop, so a cold
# start costs nothing and a dropped connection self-heals, as before. Dependency-
# free on purpose: psycopg_pool isn't installed and there's no requirements file,
# so this uses only stdlib queue + the already-present psycopg.

_POOL_SIZE = int(os.environ.get("VOIDBASE_PG_POOL_SIZE", "8"))
# How long a request waits for a free slot before failing — backpressure, not a
# hang. Bounds tail latency when every connection is busy.
_POOL_TIMEOUT = float(os.environ.get("VOIDBASE_PG_POOL_TIMEOUT", "15"))

_pool: "queue.Queue | None" = None
_pool_init_lock = threading.Lock()


def _get_pool() -> "queue.Queue":
    """The connection pool, created on first use. Each slot starts as None (an
    unopened connection) and is filled lazily on first borrow, so importing this
    module — or running the SQLite backend — never opens a socket."""
    global _pool
    if _pool is None:
        with _pool_init_lock:
            if _pool is None:
                q: queue.Queue = queue.Queue(maxsize=_POOL_SIZE)
                for _ in range(_POOL_SIZE):
                    q.put(None)  # empty slot — opened on first borrow
                _pool = q
    return _pool


def _borrow():
    """Borrow a slot from the pool, raising a clear backpressure error if every
    connection is busy past the timeout. Returns whatever the slot holds (a live
    connection or None to be opened by the caller)."""
    try:
        return _get_pool().get(timeout=_POOL_TIMEOUT)
    except queue.Empty:
        raise RuntimeError(
            f"db pool exhausted: all {_POOL_SIZE} connections busy for "
            f"{_POOL_TIMEOUT}s (raise VOIDBASE_PG_POOL_SIZE if this is sustained)")


def _run_pg(sql: str, params: tuple, *, fetch: bool) -> list[dict]:
    """Borrow a slot, run one autocommit query on its connection, return the slot.
    Opens the connection if the slot is empty/closed and reconnects once on a
    dropped connection — the same self-healing the single-connection path had,
    now per-slot. `fetch` distinguishes a read (always fetchall) from a write
    (fetchall only when the statement RETURNs)."""
    import psycopg
    from psycopg.rows import dict_row

    conn = _borrow()
    try:
        for attempt in (1, 2):  # one reconnect-and-retry on a dropped connection
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(PG_URL, autocommit=True)
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    if fetch:
                        return list(cur.fetchall())
                    return list(cur.fetchall()) if cur.description else []
            except psycopg.OperationalError:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None  # next attempt opens a fresh connection
                if attempt == 2:
                    raise
        return []
    finally:
        _get_pool().put(conn)  # return the slot (a live conn, or None to reopen)


def _pg_rows(sql: str, params: tuple = ()) -> list[dict]:
    return _run_pg(sql, params, fetch=True)


def _pg_exec(sql: str, params: tuple = ()) -> list[dict]:
    """Write helper (INSERT/UPDATE … RETURNING). autocommit is on so each call is
    its own transaction. Postgres only — the SQLite backend is read-only."""
    return _run_pg(sql, params, fetch=False)


def _pg_executemany(sql: str, seq_of_params) -> None:
    """Batch many rows in ONE round-trip on a single borrowed connection, instead
    of N separate _pg_exec calls (each a full Neon round-trip). Used for per-step
    eval_points, where a run can report hundreds of points. No RETURNING; a no-op
    on an empty sequence."""
    import psycopg
    from psycopg.rows import dict_row

    params_list = list(seq_of_params)
    if not params_list:
        return
    conn = _borrow()
    try:
        for attempt in (1, 2):
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(PG_URL, autocommit=True)
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.executemany(sql, params_list)
                return
            except psycopg.OperationalError:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
                if attempt == 2:
                    raise
    finally:
        _get_pool().put(conn)


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
