#!/usr/bin/env python3
"""voidbase API — write endpoints + bearer-token auth.

Everything that mutates the store, grouped by protocol:

  * Operator/dashboard (localhost, no auth): thread author/update + claim/release,
    box heartbeats. The operator owns the box, so the loopback bypass is enough.
  * Voidrunner compute-donor protocol (bearer token): register, claim a job,
    report a finished run, release a claim. The client runs on a machine the
    operator does NOT control, holds no DB creds, and authenticates with a token.
  * Voidmind token-donor protocol (bearer token): propose an idea, enqueue a
    runnable experiment. LOW-TRUST proposals — they can never move the champion.

Auth (token → contributor id, with the localhost 'automation' bypass) lives here
too, since it gates exactly these writes. Postgres-only — the SQLite backend is
read-only for the dashboard.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voidconfig  # noqa: E402  — pure config-row shape + authoritative dedup hash (Voidmind writes)

from backend import LEASE_SECONDS, _pg_exec, _pg_rows  # noqa: E402


# --- box heartbeat + dashboard thread writes (operator, localhost) -----------

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

    # Reproducibility bundle (git triple + runtime env): the client captures the
    # commit/branch/dirty of the research repo it ran and its python/torch/CUDA
    # stack, so a confirmed champion carries everything needed to re-run it. All
    # best-effort and nullable — an older client that doesn't send them just yields
    # a bundle voidcheck marks 'not reproducible' rather than a failed report.
    env = body.get("env")
    _pg_exec(
        """insert into runs (id, queue_item_id, thread_name, name, contributor_id,
                             box_id, command, config, content_hash, seed, status,
                             final_val_loss, final_train_loss, final_val_accuracy,
                             git_commit, git_branch, git_dirty, env,
                             finished_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, now())""",
        (run_id, qid, thread_name, name, contributor_id, box_id,
         body.get("command"),
         json.dumps(config) if config is not None else None,
         body.get("content_hash"), seed, status,
         body.get("final_val_loss"), body.get("final_train_loss"),
         body.get("final_val_accuracy"),
         body.get("git_commit"), body.get("git_branch"), body.get("git_dirty"),
         json.dumps(env) if env is not None else None))

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


# --- Voidmind: the token-donor write protocol (ideas + queue_items) ---------
#
# The server side of the idea-loop client (docs/VOIDMIND.md). Voidmind runs on a
# token donor's box (their own LLM keys), reads open threads, and proposes work.
# Like Voidrunner it holds no DB creds and authenticates with a bearer token, but
# its writes are LOW-TRUST PROPOSALS, not results: an idea/queue_item can never
# move the champion (that still goes through the confirm gate), so an open Voidmind
# is safe — worst case is junk rows that never get claimed or lose their pairing.

def create_idea(body: dict, contributor_id: str) -> dict:
    """Record a candidate idea (proposed by this contributor). Pure backlog — an
    idea is a note, not runnable; the runnable artifact is the queue_item. Title is
    required; explanation/notes optional. Born status 'proposed'."""
    title = (body.get("title") or "").strip()
    if not title:
        raise ValueError("idea requires a 'title'")
    idea_id = (body.get("id") or "").strip() or f"idea-{uuid.uuid4().hex[:12]}"
    out = _pg_exec(
        """insert into ideas (id, title, explanation, status, proposed_by, notes)
           values (%s, %s, %s, 'proposed', %s, %s)
           on conflict (id) do nothing
           returning id, title, status, proposed_by, created_at""",
        (idea_id, title, body.get("explanation"), contributor_id, body.get("notes")))
    if not out:
        raise ValueError(f"idea id already exists: {idea_id}")
    return out[0]


def enqueue_item(body: dict, contributor_id: str) -> dict:
    """Enqueue a runnable experiment from a self-contained config (the donor's
    idea, resolved). The SERVER owns the dedup key: it validates the config shape
    and computes content_hash via voidconfig, so a client can neither submit a
    malformed row nor dodge dedup with a forged hash. Born 'needs-run' and
    unclaimed — Voidrunner drains it later, the result is born unverified.

    Idempotent on the resolved config: if its content_hash is already a run or a
    queue item, this is a no-op that reports {deduped: true} rather than a second
    copy. The thread must already exist (a proposal attaches to an open thread)."""
    thread = (body.get("thread") or body.get("thread_name") or "").strip()
    if not thread:
        raise ValueError("queue_items requires a 'thread' (an existing thread name)")
    if not _pg_rows("select 1 from threads where name = %s", (thread,)):
        raise ValueError(f"no such thread: {thread} (create or pick an existing one)")

    config = voidconfig.validate_config(body.get("config"))
    lever = (config.get("lever") or body.get("lever") or "idea").strip() or "idea"
    chash = voidconfig.content_hash(config.get("env") or {}, config.get("fields") or {})

    # Authoritative dedup: the same resolved config already run OR queued is a
    # no-op. This is the same dedup space feeder/enqueue use (one indexed lookup).
    seen = _pg_rows(
        """select 'run' as where_ from runs where content_hash = %(h)s
           union all
           select 'queue' from queue_items where content_hash = %(h)s
           limit 1""", {"h": chash})
    if seen:
        return {"deduped": True, "content_hash": chash, "where": seen[0]["where_"]}

    qid = voidconfig.queue_item_id("mind", lever, chash)
    name = voidconfig.queue_item_name(lever, chash)
    priority = int(body.get("priority") or 0)
    gpu_class = (body.get("gpu_class") or "any").strip() or "any"
    out = _pg_exec(
        """insert into queue_items
             (id, thread_name, name, command, status, config, content_hash,
              gpu_class, priority)
           values (%s, %s, %s, 'python run_experiment.py', 'needs-run', %s, %s, %s, %s)
           on conflict (id) do nothing
           returning id, thread_name, name, status, content_hash, priority""",
        (qid, thread, name, json.dumps(config), chash, gpu_class, priority))
    if not out:
        # id collided (same config raced in between the dedup check and here).
        return {"deduped": True, "content_hash": chash, "queue_item_id": qid}
    row = out[0]
    return {"deduped": False, "queue_item_id": row["id"], "content_hash": chash,
            "thread_name": row["thread_name"], "name": row["name"],
            "status": row["status"], "priority": row["priority"]}
