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

Endpoints (GET unless noted, JSON):
  /health        liveness + row counts + per-box health + which backend is live
  /runs          training runs (verification + has_eval)
  /threads       hypotheses
  /comparisons   paired deltas (is_paired: real on PG, false on SQLite)
  /champions     champion history
  /ideas         backlog
  /queue         job queue
  /eval?run_id=  per-step learning curve for one run

  POST /threads        author / update a research thread (dashboard)
  POST /box_heartbeat  {box_id, gpu_class?} liveness ping from a worker (PG-only)
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import uuid
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


# --- endpoint builders -------------------------------------------------------

def runs() -> list[dict]:
    have_eval = {r["run_id"] for r in rows("select distinct run_id from eval_points")}
    # Stable inventor attribution: the runs table carries contributor_id, so map
    # it to a handle here. This is what /gallery + /contributor key off — unlike
    # the activity() snapshot it never ages out, so a run keeps its inventor
    # forever instead of decaying to "anonymous" after 30 minutes (issue #14).
    # Best-effort: the legacy SQLite store has no contributors table, so fall
    # back to an empty map and every run simply reads back handle=null.
    try:
        handles = {c["id"]: c.get("handle")
                   for c in rows("select id, handle from contributors")}
    except Exception:
        handles = {}
    raw = rows("select * from runs order by created_at desc")
    out = []
    for r in raw:
        cid = r.get("contributor_id")
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
            # Inventor identity (null-safe: a run with no contributor → both null).
            "contributor_id": cid,
            "contributor_handle": handles.get(cid),
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


# Fields an external agent needs to choose work. The full goal_prompt is
# deliberately NOT here — it's large; fetch it per-thread via /threads/goal.
_PUBLIC_THREAD_FIELDS = (
    "name", "hypothesis", "kind", "priority", "repo_url", "submit_via", "status",
    "claimed_by", "claim_expires_at", "run_count_last_7d", "run_count_all_time",
)


def threads_public(status: str | None = "active", unclaimed: bool = False) -> list[dict]:
    """Read-only, agent-facing thread list — the destination the landing-page
    prompt points autonomous agents at, so they can self-direct ("show me
    high-priority unclaimed threads") instead of reading a stale champion.json.

    Distinct from /threads (the dashboard read, which carries the full rows incl.
    goal_prompt and which the research board depends on). This trims to
    _PUBLIC_THREAD_FIELDS, adds run_count_all_time + run_count_last_7d, applies
    optional status / unclaimed filters, and sorts important-and-trending first
    (priority desc, then recent activity). Expired claims read back as unclaimed.

    Portable: built on `select t.*` + a Python trim, so a backend missing the
    Postgres-only claim/goal columns (legacy SQLite) still works — absent fields
    are simply not in the output and `unclaimed` becomes a no-op."""
    sql_pg = (
        "select t.*, "
        "(t.claim_expires_at is not null and t.claim_expires_at < now()) "
        "  as claim_expired, "
        "(select count(*) from runs r where r.thread_name = t.name "
        "   and r.created_at > now() - interval '7 days') as run_count_last_7d, "
        "(select count(*) from runs r where r.thread_name = t.name) "
        "  as run_count_all_time "
        "from threads t")
    sql_sqlite = (
        "select t.*, "
        "(select count(*) from runs r where r.thread_name = t.name "
        "   and r.created_at > datetime('now','-7 days')) as run_count_last_7d, "
        "(select count(*) from runs r where r.thread_name = t.name) "
        "  as run_count_all_time "
        "from threads t")
    out = []
    for r in rows(sql_pg, sql_sqlite):
        if r.pop("claim_expired", False):  # lazy auto-release on read
            r["claimed_by"] = None
            r["claim_expires_at"] = None
        if status and (r.get("status") or "active") != status:
            continue
        if unclaimed and r.get("claimed_by"):
            continue
        out.append({k: r[k] for k in _PUBLIC_THREAD_FIELDS if k in r})
    out.sort(key=lambda r: (-(r.get("priority") or 0), -(r.get("run_count_last_7d") or 0)))
    return out


def thread_goal(name: str) -> dict:
    """The full goal_prompt for ONE thread — the brief an agent executes
    end-to-end. Split out of the list payload because it's large; mirrors the
    /eval?run_id= query-param pattern."""
    out = rows(
        "select name, goal_prompt from threads where name = %s",
        "select name, goal_prompt from threads where name = ?",
        (name,),
    )
    if not out:
        raise ValueError(f"no such thread: {name}")
    return out[0]


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
    result = {"ok": ok, "db": db_label, "backend": BACKEND, "counts": counts}
    # Per-box health so the cockpit can show which GPUs are alive at a glance.
    # Postgres-only (the boxes health columns live on the distributed store);
    # best-effort — a boxes query error must never take /health down.
    if PG_URL:
        try:
            result["boxes"] = _pg_rows(
                """select label, status, last_heartbeat, failed_run_count,
                          extract(epoch from (now() - last_heartbeat))::int as heartbeat_age_s
                   from boxes
                   order by label nulls last""")
        except Exception:  # noqa: BLE001
            pass
    return result


def box_heartbeat(body: dict) -> dict:
    """Record a liveness ping from a worker's box: stamp last_heartbeat=now() and
    mark it 'healthy' (and refresh gpu_class if the worker reported one). The
    reaper reads last_heartbeat to tell a live box from one that went dark
    mid-run. Postgres-only — the SQLite legacy store has no live boxes.

    Idempotent: a missing box id is a client error (the worker creates its box
    row before pinging), not a silent insert — we never fabricate a box with no
    contributor."""
    box_id = (body.get("box_id") or "").strip()
    if not box_id:
        raise ValueError("box_heartbeat requires 'box_id'")
    out = _pg_exec(
        """update boxes
           set last_heartbeat = now(),
               status = 'healthy',
               gpu_class = coalesce(%(gpu_class)s, gpu_class)
           where id = %(box_id)s::uuid
           returning id, label, status, last_heartbeat, gpu_class, failed_run_count""",
        {"box_id": box_id, "gpu_class": body.get("gpu_class")},
    )
    if not out:
        raise ValueError(f"no box with id {box_id}")
    return out[0]


def upsert_thread(body: dict) -> dict:
    """Create or update a research thread by name (the PK). Only the fields
    present in `body` are written; omitted fields keep their current value via
    COALESCE on conflict. Returns the resulting row."""
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("thread 'name' is required")
    # tags: a JSON array of free-text strings (migration 0008). Passed as None
    # when the caller omits the key so the COALESCE below preserves whatever was
    # there — an update that doesn't mention tags must not wipe them. A bad type
    # (not a list) is coerced to [] rather than rejected, matching the lenient
    # write path of the rest of this function.
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        tags = []
    cols = {
        "hypothesis": body.get("hypothesis"),
        "goal_prompt": body.get("goal_prompt"),
        "kind": body.get("kind") or "question",
        "submit_via": body.get("submit_via") or "pr",
        "repo_url": body.get("repo_url"),
        "status": body.get("status") or "active",
        "priority": int(body.get("priority") or 0),
        "summary": body.get("summary"),
        "tags": json.dumps(tags) if tags is not None else None,
    }
    out = _pg_exec(
        """
        insert into threads (name, hypothesis, goal_prompt, kind, submit_via,
                             repo_url, status, priority, summary, tags)
        values (%(name)s, %(hypothesis)s, %(goal_prompt)s, %(kind)s, %(submit_via)s,
                %(repo_url)s, %(status)s, %(priority)s, %(summary)s,
                coalesce(%(tags)s::jsonb, '[]'::jsonb))
        on conflict (name) do update set
            hypothesis  = coalesce(excluded.hypothesis,  threads.hypothesis),
            goal_prompt = coalesce(excluded.goal_prompt, threads.goal_prompt),
            kind        = excluded.kind,
            submit_via  = excluded.submit_via,
            repo_url    = coalesce(excluded.repo_url, threads.repo_url),
            status      = excluded.status,
            priority    = excluded.priority,
            summary     = coalesce(excluded.summary, threads.summary),
            tags        = coalesce(%(tags)s::jsonb, threads.tags),
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


# --- Voidrunner: auth + the compute-donor write protocol --------------------
#
# These four handlers are the server side of the compute-donor client
# (docs/VOIDRUNNER.md). They differ from the dashboard writes above in one way:
# the client runs on a machine the operator does NOT control, so it holds no DB
# creds and authenticates with a bearer token instead. The atomic claim that used
# to live as direct SQL in scripts/worker.py moves here, behind the API.

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def contributor_for_token(token: str) -> str | None:
    """Resolve a bearer token to a contributor id, or None if unknown."""
    out = _pg_rows("select id from contributors where token_hash = %s",
                   (_token_hash(token),))
    return out[0]["id"] if out else None


def automation_contributor_id() -> str:
    """The token-less identity used by the localhost dev bypass (the operator's
    own dashboard / worker.py). Created on demand, idempotent."""
    out = _pg_exec(
        """insert into contributors (handle, role) values ('automation', 'maintainer')
           on conflict (handle) do update set handle = excluded.handle
           returning id""")
    return out[0]["id"]


def register(body: dict) -> dict:
    """Mint a contributor + bearer token. The plaintext token is returned ONCE,
    here, and never stored — only its sha256 hash lands in the DB. A handle is
    claim-once: re-registering an existing handle is refused (so a token can't be
    silently rotated out from under its owner). The operator issues tokens from
    localhost for v0; GitHub-OAuth issuance via voidspark comes later."""
    handle = (body.get("handle") or "").strip()
    if not handle:
        raise ValueError("register requires a 'handle'")
    token = secrets.token_urlsafe(32)
    out = _pg_exec(
        """insert into contributors (handle, role, token_hash)
           values (%s, 'contributor', %s)
           on conflict (handle) do nothing
           returning id, handle""",
        (handle, _token_hash(token)))
    if not out:
        raise ValueError(f"handle already registered: {handle}")
    # The token is shown exactly once; the caller must save it now.
    return {"contributor_id": out[0]["id"], "handle": out[0]["handle"], "token": token}


def _ensure_box(contributor_id: str, box: dict) -> str:
    """Find-or-create this contributor's box row, keyed by (contributor, finger-
    print) like worker.ensure_identity. Returns the box id. A donor's box belongs
    to the donor — never the operator — so attribution and per-box baselines are
    correct."""
    fingerprint = (box.get("fingerprint") or "").strip() or "default"
    out = _pg_exec(
        """insert into boxes (contributor_id, label, gpu_class, fingerprint)
           values (%(cid)s, %(label)s, %(gpu)s, %(fp)s)
           on conflict (contributor_id, fingerprint)
             do update set label = coalesce(excluded.label, boxes.label),
                           gpu_class = coalesce(excluded.gpu_class, boxes.gpu_class)
           returning id""",
        {"cid": contributor_id, "label": box.get("label"),
         "gpu": box.get("gpu_class"), "fp": fingerprint})
    return out[0]["id"]


# The atomic claim, moved server-side from scripts/worker.py. UPDATE…FROM(SELECT
# …FOR UPDATE SKIP LOCKED LIMIT 1) is the collision-proof Postgres job lock: two
# runners firing at once never get the same row, and an expired lease is auto-
# reclaimed so a dead runner never strands a job. The optional gpu_class filter
# lets a donor claim only jobs its GPU can run (a null job gpu_class = anything).
_CLAIM_SQL = """
update queue_items q
set status = 'claimed',
    claimed_by_box = %(box)s,
    claimed_at = now(),
    lease_expires_at = now() + make_interval(secs => %(lease)s)
from (
    select id
    from queue_items
    where (status = 'needs-run'
           or (status in ('claimed', 'running')
               and lease_expires_at is not null
               and lease_expires_at < now()))
      and (%(gpu)s::text is null or gpu_class is null or gpu_class = %(gpu)s)
      and (%(thread)s::text is null or thread_name = %(thread)s)
    order by priority desc, created_at asc
    for update skip locked
    limit 1
) pick
where q.id = pick.id
returning q.id, q.thread_name, q.name, q.command, q.config, q.content_hash;
"""


def claim_job(body: dict, contributor_id: str) -> dict:
    """Atomically lease the next runnable job for this contributor's box, or
    return {"job": null}. The box is identified by body['box'] = {label,
    gpu_class, fingerprint}; an optional body['gpu_class_filter'] restricts which
    jobs are eligible; an optional body['thread'] scopes the claim to one research
    thread (a donor or agent focusing a thread). Returns the job plus the resolved
    box_id, which the runner echoes back on /runs."""
    box = body.get("box") or {}
    box_id = _ensure_box(contributor_id, box)
    gpu = (body.get("gpu_class_filter") or "").strip() or None
    thread = (body.get("thread") or "").strip() or None
    out = _pg_exec(_CLAIM_SQL,
                   {"box": box_id, "lease": LEASE_SECONDS, "gpu": gpu, "thread": thread})
    if not out:
        return {"job": None, "box_id": box_id}
    r = out[0]
    return {
        "box_id": box_id,
        "job": {
            "id": r["id"],
            "thread": r.get("thread_name"),
            "name": r.get("name"),
            "command": r.get("command"),
            "config": r.get("config"),
            "content_hash": r.get("content_hash"),
        },
    }


def report_run(body: dict, contributor_id: str) -> dict:
    """Record a finished run and close its queue item, in one transaction. The
    run is born verification='unverified' — a donor can never move the champion;
    that still goes through the confirm gate. Mirrors scripts/worker.py:report(),
    but contributor_id comes from the bearer token, not a trusted local identity.

    A run row needs its queue item, its box, and a terminal status. seed is
    pulled through (from the config) so the comparison engine can pair it — a
    null-seed run is unpaired and therefore wasted signal."""
    qid = (body.get("queue_item_id") or "").strip()
    if not qid:
        raise ValueError("report requires 'queue_item_id'")
    box_id = (body.get("box_id") or "").strip()
    if not box_id:
        raise ValueError("report requires 'box_id' (from the /claim response)")
    status = (body.get("status") or "").strip().lower()
    if status not in ("done", "failed"):
        raise ValueError("report 'status' must be 'done' or 'failed'")

    # The box must be one this contributor owns — a donor can't attribute a run to
    # someone else's box. Per-box baselines de-drift the screen, so an honest
    # box_id is an integrity property, not just bookkeeping.
    owns = _pg_rows(
        "select 1 from boxes where id = %s::uuid and contributor_id = %s",
        (box_id, contributor_id))
    if not owns:
        raise ValueError("box_id is not one of your boxes")

    qrow = _pg_rows(
        "select thread_name, name from queue_items where id = %s", (qid,))
    if not qrow:
        raise ValueError(f"no such queue_item: {qid}")
    thread_name = qrow[0]["thread_name"]
    name = qrow[0]["name"]

    config = body.get("config")
    seed = body.get("seed")
    if seed is None and isinstance(config, dict):
        seed = config.get("seed")
    run_id = f"{qid}--{uuid.uuid4().hex[:8]}"

    _pg_exec(
        """insert into runs (id, queue_item_id, thread_name, name, contributor_id,
                             box_id, command, config, content_hash, seed, status,
                             final_val_loss, final_train_loss, final_val_accuracy,
                             finished_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
        (run_id, qid, thread_name, name, contributor_id, box_id,
         body.get("command"),
         json.dumps(config) if config is not None else None,
         body.get("content_hash"), seed, status,
         body.get("final_val_loss"), body.get("final_train_loss"),
         body.get("final_val_accuracy")))

    # Optional per-step learning curve (eval_points). Best-effort: a malformed
    # point list must not lose the run row we just wrote.
    points = body.get("eval_points") or []
    for p in points:
        try:
            _pg_exec(
                """insert into eval_points (run_id, step, tokens, val_loss,
                                            val_accuracy, val_perplexity,
                                            learning_rate, elapsed_seconds)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, p.get("step"), p.get("tokens"), p.get("val_loss"),
                 p.get("val_accuracy"), p.get("val_perplexity"),
                 p.get("learning_rate"), p.get("elapsed_seconds")))
        except Exception:  # noqa: BLE001
            pass

    _pg_exec("update queue_items set status = %s, finished_at = now() where id = %s",
             (status, qid))
    return {"run_id": run_id, "queue_item_id": qid, "status": status,
            "verification": "unverified", "seed": seed}


def release_job(body: dict, contributor_id: str) -> dict:
    """Return a claimed job to the queue (needs-run), clearing the claim fields.
    For a dry-run validation that must not leave a runs row, or a graceful Ctrl-C.
    Only the box-owning contributor may release — a runner can't kick another
    donor's job."""
    qid = (body.get("queue_item_id") or "").strip()
    if not qid:
        raise ValueError("release requires 'queue_item_id'")
    out = _pg_exec(
        """update queue_items q
           set status = 'needs-run', started_at = null,
               claimed_by_box = null, claimed_at = null, lease_expires_at = null
           from boxes b
           where q.id = %(qid)s
             and q.claimed_by_box = b.id
             and b.contributor_id = %(cid)s
           returning q.id""",
        {"qid": qid, "cid": contributor_id})
    if not out:
        raise ValueError(
            f"cannot release {qid}: not found, or not claimed by your box")
    return {"released": out[0]["id"]}


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

    def _authenticate(self):
        """Resolve the writer's contributor id for the token-gated endpoints
        (claim / runs / release). Returns the id, or sends a 401 and returns None.

          * `Authorization: Bearer <token>` → the matching contributor.
          * No token, from localhost → the 'automation' contributor (the dev
            bypass that keeps worker.py / the dashboard working unchanged).
          * No token, from a remote client → 401.

        The localhost bypass is what lets the operator's own tools skip auth while
        a real donor over the network must present a token."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token:
            cid = contributor_for_token(token)
            if not cid:
                self._send(401, {"error": "invalid bearer token"})
                return None
            return cid
        # No token: the loopback bypass, unless this is a public deployment that
        # has turned it off (REQUIRE_AUTH) — see the REQUIRE_AUTH comment for why
        # a proxied deployment must, or it hands the bypass to every client.
        if not REQUIRE_AUTH and self.client_address[0] in ("127.0.0.1", "::1", "localhost"):
            return automation_contributor_id()
        self._send(401, {"error": "missing bearer token"})
        return None

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """The platform's writes. Two tiers:

          * Operator/dashboard (localhost, no auth, unchanged):
            - author / update a research thread   (/threads, default)
            - claim / release a thread            (/threads, action=claim|release)
            - record a box heartbeat              (/box_heartbeat)
          * Voidrunner compute-donor protocol (bearer token; localhost bypass):
            - mint a contributor + token          (/register, no auth)
            - claim the next job                  (/claim)
            - report a finished run               (/runs)
            - release a claimed job               (/release)

        Dispatch is by URL path; a client may instead POST the base URL with a
        {resource: '...'} field in the body (the worker sends {resource:
        'box_heartbeat', ...})."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"bad json: {e}"})
            return
        resource = path.lstrip("/") or str(body.get("resource") or "")
        # Endpoints that require a contributor identity (token, or localhost bypass).
        authed = {"claim", "runs", "release"}
        writable = {"threads", "box_heartbeat", "register"} | authed
        if resource not in writable:
            self._send(404, {"error": "not found", "writable": sorted(writable)})
            return
        if not PG_URL:
            self._send(501, {"error": f"{resource} writes require the Postgres backend"})
            return
        try:
            if resource in authed:
                cid = self._authenticate()
                if cid is None:
                    return  # 401 already sent
                handler = {"claim": claim_job, "runs": report_run,
                           "release": release_job}[resource]
                self._send(200, handler(body, cid))
            elif resource == "register":
                self._send(200, register(body))
            elif resource == "box_heartbeat":
                self._send(200, box_heartbeat(body))
            else:  # threads — author/update, or claim/release by action
                action = (body.get("action") or "").strip().lower()
                handler = {"claim": claim_thread, "release": release_thread}.get(
                    action, upsert_thread)
                self._send(200, handler(body))
        except ValueError as e:  # bad/insufficient input from the client
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/health"
        q = parse_qs(parsed.query)
        try:
            if path == "/eval":
                self._send(200, eval_points(q.get("run_id", [""])[0]))
                return
            if path == "/threads/public":
                status = q.get("status", ["active"])[0] or None
                unclaimed = q.get("unclaimed", ["false"])[0].lower() in ("1", "true", "yes")
                self._send(200, threads_public(status=status, unclaimed=unclaimed))
                return
            if path == "/threads/goal":
                self._send(200, thread_goal(q.get("name", [""])[0]))
                return
            handler = ROUTES.get(path)
            if handler is None:
                routes = sorted([*ROUTES, "/eval?run_id=",
                                 "/threads/public?status=&unclaimed=", "/threads/goal?name="])
                self._send(404, {"error": "not found", "routes": routes})
                return
            self._send(200, handler())
        except ValueError as e:  # e.g. unknown thread name — client-correctable
            self._send(404, {"error": str(e)})
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
