#!/usr/bin/env python3
"""voidbase API — read endpoints (the dashboard's GET contract).

Pure read builders: each returns the JSON shape voidspark consumes. They lean on
backend.rows()/_pg_rows() for the store and on the pure policy libs for any
judgement that must not be re-derived client-side:

  * voidcredit — attribution/leaderboard rules (compute-seconds, champion-runs,
    idea→run→champion chains). Postgres-only edges; SQLite returns empty.
  * voidcheck  — the trust rules (screen band + plausibility floor; single source)
    behind the confirm gate, so the dashboard can never disagree with the daemon.

No writes here. The composite /dashboard (bottom) stitches six of these into one
cached round-trip — see its block comment for why that matters under one Neon
connection.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voidcredit  # noqa: E402  — pure attribution/leaderboard policy (Postgres-only edges)
import voidcheck   # noqa: E402  — pure trust rules (the screen band + plausibility floor; single source)
from scripts.findings import bucket_for  # noqa: E402 — the pure evidence classifier (one source)

from backend import BACKEND, DB_PATH, PG_URL, RUN_STATUS, _pg_rows, rows  # noqa: E402


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


# --- Voidcredit: attribution & leaderboard (read-only; Postgres-only) --------
#
# These do the aggregation SQL and hand the rows to the pure voidcredit policy —
# mirroring how confirm_daemon passes rows to voidcheck. Credit is derived on
# read (no stored credit table to drift). Postgres-only: the joins/filters need
# the distributed store; the legacy SQLite backend returns an empty result.

def leaderboard() -> list[dict]:
    """Contributors ranked by the credit policy (impact first). One aggregate row
    per contributor with at least one run."""
    if not PG_URL:
        return []
    stats = _pg_rows(
        """select c.handle, c.role,
                  count(r.id)                                          as runs_total,
                  count(r.id) filter (where r.verification='confirmed') as runs_confirmed,
                  count(distinct ch.id)                                as champion_runs,
                  c.compute_seconds, c.tokens_donated
           from contributors c
           left join runs r on r.contributor_id = c.id
           left join champions ch on ch.run_id = r.id and ch.superseded_at is null
           group by c.id, c.handle, c.role, c.compute_seconds, c.tokens_donated
           having count(r.id) > 0""")
    return voidcredit.rank_contributors(stats)


def contributor(handle: str) -> dict:
    """One contributor's card: totals, best run, champion-holding runs, recent
    runs. Unknown handle is a 404 (client-correctable)."""
    if not handle:
        raise ValueError("contributor requires 'handle'")
    if not PG_URL:
        return {}
    exists = _pg_rows("select 1 from contributors where handle = %s", (handle,))
    if not exists:
        raise ValueError(f"no such contributor: {handle}")
    runs = _pg_rows(
        """select r.id, r.name, r.thread_name, r.verification, r.final_val_loss,
                  r.created_at, r.finished_at
           from runs r join contributors c on c.id = r.contributor_id
           where c.handle = %s order by r.created_at desc""", (handle,))
    champ_ids = [row["run_id"] for row in
                 _pg_rows("select run_id from champions where superseded_at is null")]
    return voidcredit.contributor_card(handle, runs, champion_run_ids=champ_ids)


def lineage(run_id: str) -> dict:
    """The provenance chain for one run: thread → queue_item → run → champion."""
    if not run_id:
        raise ValueError("lineage requires 'run'")
    if not PG_URL:
        return {}
    runs = _pg_rows("select * from runs where id = %s", (run_id,))
    if not runs:
        raise ValueError(f"no such run: {run_id}")
    run = runs[0]
    qi = None
    if run.get("queue_item_id"):
        qrows = _pg_rows("select id, name from queue_items where id = %s",
                         (run["queue_item_id"],))
        qi = qrows[0] if qrows else None
    thread = None
    if run.get("thread_name"):
        trows = _pg_rows("select name, hypothesis from threads where name = %s",
                         (run["thread_name"],))
        thread = trows[0] if trows else None
    champs = _pg_rows("select run_id, scope, promoted_at, superseded_at "
                      "from champions where run_id = %s", (run_id,))
    return voidcredit.run_lineage(run, queue_item=qi, thread=thread, champions=champs)


def champion_bundle(scope: str) -> dict:
    """The reproducibility bundle for a scope's CURRENT champion: everything a
    third party needs to re-run it, plus voidcheck's judgement of whether it
    actually can be. This is "trust without reputation" made checkable — a
    confirmed champion that isn't reproducible is a result nobody else can stand
    behind, and this endpoint surfaces exactly that.

    Joins the live champion (superseded_at is null) to its run and box, then hands
    the rows to voidcheck.repro_bundle (the single source for the bundle shape +
    the reproducible verdict). Postgres-only; the legacy SQLite store has no
    champions table, so it returns {}."""
    scope = scope or "tiny1m3m"
    if not PG_URL:
        return {}
    champs = _pg_rows(
        """select c.run_id, c.val_loss, c.promoted_at,
                  r.config, r.seed, r.command, r.content_hash,
                  r.git_commit, r.git_branch, r.git_dirty, r.env,
                  b.gpu_class, b.label as box_label
           from champions c
           join runs r on r.id = c.run_id
           left join boxes b on b.id = r.box_id
           where c.scope = %s and c.superseded_at is null""",
        (scope,))
    if not champs:
        return {"scope": scope, "champion": None, "bundle": None}
    row = champs[0]
    run = {"id": row["run_id"], "config": row.get("config"), "seed": row.get("seed"),
           "command": row.get("command"), "content_hash": row.get("content_hash"),
           "git_commit": row.get("git_commit"), "git_branch": row.get("git_branch"),
           "git_dirty": row.get("git_dirty"), "env": row.get("env")}
    box = {"gpu_class": row.get("gpu_class"), "label": row.get("box_label")}
    return {
        "scope": scope,
        "champion": {"run_id": row["run_id"], "val_loss": row.get("val_loss"),
                     "promoted_at": row.get("promoted_at")},
        "bundle": voidcheck.repro_bundle(run, box=box),
    }


def _confirm_progress(run_id: str) -> dict | None:
    """How far along the paired 3-seed confirm for one candidate is: terminal vs
    total jobs (target 6 = 3 seeds x 2 arms), or None if no confirm is in flight.
    Mirrors the daemon's id-prefix re-filter in Python (a run id's '_' is a LIKE
    wildcard, so the SQL LIKE can over-match)."""
    prefix = f"confirm-{run_id}-"
    rows = _pg_rows("select id, status from queue_items where id like %s",
                    (f"{prefix}%%",))
    jobs = [r for r in rows
            if r["id"].startswith(prefix)
            and r["id"][len(prefix):].split("-s")[0] in ("cand", "base")]
    if not jobs:
        return None
    terminal = sum(1 for j in jobs if j["status"] in ("done", "failed"))
    return {"terminal": terminal, "total": len(jobs)}


def gate(scope: str) -> dict:
    """Read-only status of the confirm gate for a scope: the live champion, the
    candidate field (runs that CLEAR the screen band vs the closest sub-band
    near-miss), and the SINGLE blocker keeping the gate from promoting. Surfaces in
    one HTTP call what the confirm daemon only logs, so the dashboard can show WHY
    the champion is or isn't moving. Band + plausibility come from voidcheck (the
    single source), so this can never disagree with the daemon's own judgement.
    Each band-clearing candidate carries its live confirm progress (terminal/total
    jobs) when a paired confirm is in flight."""
    scope = scope or "tiny1m3m"
    if not PG_URL:
        return {}
    champs = _pg_rows(
        "select c.run_id, c.val_loss, (r.config is not null) as has_config "
        "from champions c join runs r on r.id = c.run_id "
        "where c.scope = %s and c.superseded_at is null "
        "order by c.promoted_at desc limit 1", (scope,))
    if not champs:
        return {"scope": scope, "screen_band": voidcheck.SCREEN_BAND, "champion": None,
                "clears": [], "near_miss": None,
                "blocker": "no champion set for this scope"}
    champ = champs[0]
    champ_val = champ["val_loss"]
    # Same population + scoping the confirm daemon's candidates() judges.
    field = _pg_rows(
        "select r.id, r.name, r.final_val_loss from runs r "
        "where r.thread_name = %s and r.status = 'done' and r.verification = 'unverified' "
        "and r.final_val_loss is not null and r.final_val_loss < %s "
        "and (r.queue_item_id is null or r.queue_item_id not like 'confirm-%%') "
        "and not exists (select 1 from confirmations cf where cf.run_id = r.id) "
        "order by r.final_val_loss asc", (scope, champ_val))
    clears: list[dict] = []
    near_miss: dict | None = None
    for row in field:
        v = row["final_val_loss"]
        if voidcheck.is_implausible_win(v, champ_val):
            continue  # too-good-to-be-true (broken/forged) — not a real candidate
        entry = {"id": row["id"], "name": row["name"], "val_loss": v,
                 "margin": round(champ_val - v, 4)}
        if voidcheck.beats_screen(v, champ_val):
            entry["confirm"] = _confirm_progress(row["id"])  # in-flight progress or None
            clears.append(entry)
        elif near_miss is None:
            near_miss = entry  # rows are val-ascending, so the first sub-band is closest
    # Exactly one blocker, in priority order — what to fix to make the gate move.
    if clears and not champ["has_config"]:
        blocker = ("champion run has no config — cannot build the baseline arm; point "
                   "the champion at a config-carrying run to unblock")
    elif clears:
        blocker = None  # gate is live: these candidates are confirmable right now
    elif near_miss is not None:
        blocker = (f"search plateaued — closest ({near_miss['name']}, +{near_miss['margin']}) "
                   f"is inside the {voidcheck.SCREEN_BAND} screen band; needs a better idea")
    else:
        blocker = "no contender — no plausible run beats the champion yet"
    # Recently judged candidates — a confirmed/rejected run drops out of `clears`
    # (candidates() excludes anything with a confirmation), so without this the
    # outcome would silently vanish. Closes the lifecycle: clears → confirming →
    # verdict. delta < 0 = candidate improved on the freshly re-run champion.
    verdicts = _pg_rows(
        "select r.name, cf.run_id, cf.agrees, cf.delta_from_original, cf.created_at "
        "from confirmations cf join runs r on r.id = cf.run_id "
        "where r.thread_name = %s order by cf.created_at desc limit 5", (scope,))
    recent_verdicts = [
        {"name": v["name"], "run_id": v["run_id"], "agrees": v["agrees"],
         "delta": v["delta_from_original"], "at": str(v["created_at"])}
        for v in verdicts]
    # CONFIRMED but not yet promoted — the actionable list. A run that PASSED its
    # paired confirm (verification='confirmed') and still beats the live champion,
    # but isn't the champion yet (promotion is a manual maintainer action — the
    # daemon never auto-swaps). This is what the operator needs to SEE: "a verified
    # improvement is waiting for you to promote it." Best (lowest val) first.
    confirmed_pending = _pg_rows(
        "select r.id, r.name, r.final_val_loss, cf.delta_from_original "
        "from runs r join confirmations cf on cf.run_id = r.id "
        "where r.thread_name = %s and cf.agrees = true "
        "and r.final_val_loss is not null and r.final_val_loss < %s "
        "and not exists (select 1 from champions ch "
        "                where ch.run_id = r.id and ch.superseded_at is null) "
        "order by r.final_val_loss asc", (scope, champ_val))
    pending = [
        {"id": r["id"], "name": r["name"], "val_loss": r["final_val_loss"],
         "paired_delta": r["delta_from_original"],
         "margin": round(champ_val - r["final_val_loss"], 4)}
        for r in confirmed_pending]
    return {"scope": scope, "screen_band": voidcheck.SCREEN_BAND,
            "champion": {"run_id": champ["run_id"], "val_loss": champ_val,
                         "has_config": champ["has_config"]},
            "clears": clears, "near_miss": near_miss, "blocker": blocker,
            "recent_verdicts": recent_verdicts,
            "confirmed_pending": pending}


def findings(scope: str) -> dict:
    """The research OUTPUT: every tested structural mechanism binned by EVIDENCE
    strength (the same `bucket_for` the findings CLI uses, one source). Paired-
    confirmed = real; single-seed = suggestive only. Postgres-only (needs the
    confirmations join); SQLite returns empty buckets."""
    scope = scope or "tiny1m3m"
    empty = {"scope": scope, "champion_val": None,
             "screen_band": voidcheck.SCREEN_BAND, "counts": {}, "buckets": {}}
    if not PG_URL:
        return empty
    champ = _pg_rows("select val_loss from champions where scope=%s "
                     "and superseded_at is null order by promoted_at desc limit 1",
                     (scope,))
    champ_val = float(champ[0]["val_loss"]) if champ else None
    band = voidcheck.SCREEN_BAND
    # paired verdicts: mechanism name -> (agrees, delta)
    verdict: dict[str, tuple] = {}
    for r in _pg_rows("select r.name, c.agrees, c.delta_from_original from "
                      "confirmations c join runs r on r.id=c.run_id "
                      "where r.thread_name=%s", (scope,)):
        if r["name"] not in verdict or r["agrees"]:
            verdict[r["name"]] = (bool(r["agrees"]),
                                  float(r["delta_from_original"])
                                  if r["delta_from_original"] is not None else None)
    # best (lowest) val per mechanism, excluding confirm-machinery rows
    best: dict[str, float | None] = {}
    for r in _pg_rows("select name, min(final_val_loss) as v from runs "
                      "where thread_name=%s and name like 'use_%%' "
                      "and name not like 'confirm-%%' group by name", (scope,)):
        best[r["name"]] = float(r["v"]) if r["v"] is not None else None
    names = set(best) | set(verdict)
    buckets: dict[str, list] = {k: [] for k in
                                ("confirmed", "rejected", "lead", "marginal",
                                 "neutral", "implausible", "failed")}
    for name in names:
        v = best.get(name)
        b = bucket_for(v, verdict.get(name), champ_val, band)
        entry = {"name": name, "val": v}
        if name in verdict:
            entry["paired_delta"] = verdict[name][1]
        elif v is not None and champ_val is not None:
            entry["margin"] = round(champ_val - v, 4)
        buckets[b].append(entry)
    for k in buckets:
        buckets[k].sort(key=lambda e: e["val"] if e.get("val") is not None else 9e9)
    return {"scope": scope, "champion_val": champ_val, "screen_band": band,
            "counts": {k: len(v) for k, v in buckets.items()}, "buckets": buckets}


# --- Composite dashboard endpoint (one round-trip + short TTL cache) ---------
#
# The voidspark dashboard needs six things at once: health, the champion
# lineage, the confirm gate, recent runs, comparisons, and live activity.
# Fetched separately that is six slow Neon round-trips PER POLL from every open
# tab — and because the whole backend shares one connection behind `_pg_lock`
# (see backend._pg_rows), those round-trips serialize, so concurrent pollers pile
# up and the page hangs. /dashboard composes them server-side into one payload and
# memoizes it for a few seconds: N polling clients then cost at most ONE DB pass
# per TTL window instead of 6·N. A cache hit returns without ever taking the
# lock, so it cannot contend with live writes.
_DASH_CACHE: dict[str, tuple[float, dict]] = {}
_DASH_CACHE_LOCK = threading.Lock()
# The composite query itself takes ~10–13s against a distant Neon region, so the
# TTL must comfortably exceed that — otherwise a snapshot expires before a second
# poll can ever reuse it. 12s keeps concurrent tabs + the page/gate pollers off
# the DB while staying fresher than the ~6.5-min cadence at which runs land.
_DASH_REFRESHING: set[str] = set()
_DASH_FRESH_TTL_S = 12.0
_DASH_STALE_TTL_S = 90.0


def _dash_compute(scope: str) -> dict:
    """The slow part: one (serialized) DB pass building the whole composite payload.
    Called on a cold miss (inline) or by the background refresher (off-thread)."""
    return {
        "scope": scope,
        "health": health(),
        "champions": rows(
            "select * from champions where scope=%s order by promoted_at desc",
            "select * from champions where scope=? order by promoted_at desc",
            (scope,),
        ),
        "gate": gate(scope),
        "runs": runs(),
        "comparisons": comparisons(),
        "activity": activity(),
        # Recent idea backlog — the proposal stream (Voidmind + manual). Ideas are
        # not scope-keyed, so this is a global recent slice; the dashboard renders
        # it as the "what is the search thinking about" panel. Capped so the payload
        # stays small (the full backlog is /ideas).
        "ideas": rows("select * from ideas order by created_at desc limit 24"),
        "cached": False,
    }


def _dash_store(scope: str, payload: dict) -> None:
    # Stamp the cache when the data becomes AVAILABLE (after the slow work), not
    # when the request began — the TTL window must start now, or a 13s query would
    # publish an already-expired entry.
    with _DASH_CACHE_LOCK:
        _DASH_CACHE[scope] = (time.monotonic(), payload)


def _dash_refresh_async(scope: str) -> None:
    """Recompute the snapshot off the request path, then publish it. Always clears
    the refreshing flag so a transient DB error can't wedge the scope into a state
    where no future refresh is ever spawned."""
    try:
        _dash_store(scope, _dash_compute(scope))
    except Exception:  # noqa: BLE001 — a failed refresh just leaves the stale cache
        pass
    finally:
        with _DASH_CACHE_LOCK:
            _DASH_REFRESHING.discard(scope)


def warm_dashboard(scope: str = "tiny1m3m") -> None:
    """Populate the cache for a scope in the background at startup, so even the
    FIRST user request is served from cache instead of paying the cold ~10-13s
    wait. Best-effort and fire-and-forget."""
    with _DASH_CACHE_LOCK:
        if scope in _DASH_REFRESHING:
            return
        _DASH_REFRESHING.add(scope)
    threading.Thread(target=_dash_refresh_async, args=(scope,), daemon=True).start()


def dashboard(scope: str) -> dict:
    """Composite dashboard payload with stale-while-revalidate caching. A warm cache
    is ALWAYS served instantly; a stale-but-usable snapshot is served instantly too
    and triggers a single background refresh. Only a cold/too-old cache blocks on
    the DB. The payload carries `cached`/`stale`/`age_s` so the client can tell.

    The page polls every 10s, so once warm the cache always lands in the
    FRESH..STALE window and never blocks again — only the first cold request (or a
    tab left idle past STALE) pays the ~10-13s Neon wait."""
    scope = scope or "tiny1m3m"
    spawn = False
    with _DASH_CACHE_LOCK:
        hit = _DASH_CACHE.get(scope)
        age = (time.monotonic() - hit[0]) if hit is not None else None
        if hit is not None and age < _DASH_FRESH_TTL_S:
            return {**hit[1], "cached": True, "stale": False, "age_s": round(age, 1)}
        if hit is not None and age < _DASH_STALE_TTL_S:
            # Serve stale now; spawn ONE background refresh if none is running.
            if scope not in _DASH_REFRESHING:
                _DASH_REFRESHING.add(scope)
                spawn = True
            stale_payload = {**hit[1], "cached": True, "stale": True,
                             "age_s": round(age, 1)}
        else:
            stale_payload = None
    if stale_payload is not None:
        if spawn:
            threading.Thread(target=_dash_refresh_async, args=(scope,),
                             daemon=True).start()
        return stale_payload
    # Cold or too-old: block and recompute inline, then publish.
    payload = _dash_compute(scope)
    _dash_store(scope, payload)
    with _DASH_CACHE_LOCK:
        _DASH_REFRESHING.discard(scope)
    return {**payload, "age_s": 0.0, "stale": False}
