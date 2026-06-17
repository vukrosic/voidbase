#!/usr/bin/env python3
"""sync_loop.py — mirror the live flat-file research loop into Neon.

The queue-daemon's source of truth is the repo's flat files
(`autoresearch/ideas/*/idea.md` + `run.json`). That stays untouched — it is
battle-hardened and the daemon's local cache. This bridge makes **Neon the
authoritative shared VIEW** of that loop so the voidspark UI, and (later) remote
contributor boxes, all read one address instead of scraping a laptop's disk.

Two directions, decoupled so a Neon outage can never stall the GPU loop:

  push  (local -> Neon, default)
      Upsert every idea into `ideas` (full backlog, real granular status) and
      project the runnable/terminal ones (those with a run.json) into
      `queue_items` (the GPU job queue the compute-donor UI claims from).
      Idempotent ON CONFLICT upserts; deletes nothing.

  pull  (Neon -> local, opt-in via --feed)
      Materialize any Neon queue_item the maintainer set to `needs-run` that is
      missing locally AND whose arq stub already exists in the repo (the GitHub
      code gate). This is the "feed the queue through Neon" path — how a UI click
      or a remote contributor seeds work the local daemon then runs. Conservative
      by default: never overrides an existing local status (the live daemon wins),
      only fills genuine gaps, and only with --feed.

Graceful degradation: any DB error logs a warning and exits 0 — the daemon keeps
draining the GPU off flat files regardless (memory rule: local cache + retry).

Usage:
  python3 scripts/sync_loop.py push                  # mirror local -> Neon
  python3 scripts/sync_loop.py push --repo PATH       # drain a specific repo
  python3 scripts/sync_loop.py pull --feed            # materialize Neon -> local
  python3 scripts/sync_loop.py loop --interval 120     # push every N s
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# Bound every libpq connect so a Neon hang can never wedge a daemon tick that
# calls us inline (the daemon's GPU drain must not block on the cloud DB).
os.environ.setdefault("PGCONNECT_TIMEOUT", "10")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

# The repo whose loop we mirror. The single live search runs in universe-lm
# (a.k.a. llm-research-kit-scaling). Override with --repo for any other repo.
DEFAULT_REPO = Path("/Users/vukrosic/my-life/llm-research-kit-scaling")

# All ideas in the live loop are one operator-run architecture search; the
# queue_items FK needs a thread, so they hang off this single thread.
THREAD = "tiny1m3m"

# idea.status (rich, mirrored verbatim) -> queue_items.status (narrow GPU vocab).
# Only ideas that map here AND carry a run.json become queue_items. Everything
# else is backlog-only (lives in `ideas`, not the job queue).
QUEUE_STATUS = {
    "needs-run": "needs-run",
    "running": "running",
    "done": "done",
    "needs-confirm": "done",     # ran + won the 1-seed screen; awaiting paired confirm
    "needs-recode": "failed",    # ran/builds broke — needs a code fix
    "rejected": "cancelled",
    "superseded": "done",
}


def log(msg: str) -> None:
    print(f"[sync {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# --- read the flat-file loop --------------------------------------------------

def _front(text: str, key: str) -> str | None:
    """Read a YAML-frontmatter scalar (first colon only — values hold colons)."""
    for line in text.splitlines():
        if line.strip() in ("---", ""):
            continue
        i = line.find(":")
        if i > 0 and line[:i].strip() == key:
            return line[i + 1:].strip()
        if line.startswith("#"):   # past the frontmatter into the body
            break
    return None


def _title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def read_ideas(repo: Path) -> list[dict]:
    ideas_dir = repo / "autoresearch" / "ideas"
    out: list[dict] = []
    if not ideas_dir.is_dir():
        log(f"no ideas dir at {ideas_dir}")
        return out
    for d in sorted(ideas_dir.iterdir()):
        md = d / "idea.md"
        if not md.is_file():
            continue
        text = md.read_text(errors="replace")
        slug = d.name
        status = (_front(text, "status") or "draft").strip()
        plain = _front(text, "plain") or ""
        title = _title(text, slug)
        rj = d / "run.json"
        arq = None
        if rj.is_file():
            try:
                import json
                arq = (json.loads(rj.read_text()).get("arq_file") or "").strip() or None
            except Exception:
                arq = None
        out.append({
            "id": slug, "status": status, "title": title[:300],
            "explanation": plain[:2000], "arq_file": arq,
            "arq_exists": bool(arq and (repo / arq).is_file()),
        })
    return out


# --- push: local -> Neon ------------------------------------------------------

def push(conn, ideas: list[dict]) -> tuple[int, int]:
    """Bulk-mirror local ideas -> Neon in exactly TWO round-trips (one upsert per
    table via unnest()). The old row-at-a-time loop was 300 round-trips to
    us-east-1 (~90s) — fatal in the daemon's hot path; this is sub-second."""
    cur = conn.cursor()
    # one thread to satisfy the queue_items FK
    cur.execute(
        "insert into threads (name, hypothesis, status, priority) "
        "values (%s, %s, 'active', 100) on conflict (name) do nothing",
        (THREAD, "tiny1m3m architecture search (live operator loop)"),
    )

    # ideas: one multi-row upsert
    ids = [it["id"] for it in ideas]
    titles = [it["title"] for it in ideas]
    expl = [it["explanation"] for it in ideas]
    stat = [it["status"] for it in ideas]
    if ids:
        cur.execute(
            """insert into ideas (id, title, explanation, status)
               select * from unnest(%s::text[], %s::text[], %s::text[], %s::text[])
               on conflict (id) do update set
                 title = excluded.title,
                 explanation = excluded.explanation,
                 status = excluded.status""",
            (ids, titles, expl, stat),
        )

    # queue_items: only run.json-bearing runnable/terminal ideas, one upsert
    qrows = [(it["id"], it["title"], f"python {it['arq_file']}", QUEUE_STATUS[it["status"]])
             for it in ideas
             if it["arq_file"] and it["status"] in QUEUE_STATUS]
    if qrows:
        cur.execute(
            """insert into queue_items
                 (id, thread_name, name, command, status, gpu_class, priority)
               select id, %s, name, command, status, 'any', 0
               from unnest(%s::text[], %s::text[], %s::text[], %s::text[])
                    as t(id, name, command, status)
               on conflict (id) do update set
                 status = excluded.status,
                 command = excluded.command,
                 name = excluded.name""",
            (THREAD, [r[0] for r in qrows], [r[1] for r in qrows],
             [r[2] for r in qrows], [r[3] for r in qrows]),
        )
    conn.commit()
    return len(ids), len(qrows)


# --- pull: Neon -> local (opt-in feed) ----------------------------------------

def pull(conn, repo: Path, feed: bool) -> int:
    """Materialize Neon needs-run queue_items that are missing locally and whose
    arq stub is already in the repo. Returns count materialized (or would-be)."""
    cur = conn.cursor()
    cur.execute(
        "select id, name, command from queue_items where status = 'needs-run' order by priority desc")
    rows = cur.fetchall()
    ideas_dir = repo / "autoresearch" / "ideas"
    made = 0
    for qid, name, command in rows:
        d = ideas_dir / qid
        if d.exists():
            continue  # local daemon already owns this idea — never override it
        m = re.search(r"_arq_[\w.-]+\.py", command or "")
        arq = m.group(0) if m else None
        if not arq or not (repo / arq).is_file():
            log(f"SKIP feed {qid}: arq stub not in repo (needs a merged PR first)")
            continue
        made += 1
        if not feed:
            log(f"would feed {qid} -> needs-run (arq {arq} present); pass --feed to write")
            continue
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (d / "idea.md").write_text(
            f"---\nid: {qid}\nstatus: needs-run\nupdated: {ts}\n"
            f"plain: (fed from Neon queue)\n---\n\n# {name}\n\n"
            f"Materialized from the Neon queue by sync_loop.py (--feed).\n")
        import json
        (d / "run.json").write_text(json.dumps(
            {"name": qid, "arq_file": arq, "job_timeout": "12m"}, indent=2) + "\n")
        log(f"FED {qid} -> needs-run (idea.md + run.json written)")
    return made


# --- entry --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["push", "pull", "loop"], default="push", nargs="?")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--feed", action="store_true", help="pull: actually write idea dirs (default dry-run)")
    ap.add_argument("--interval", type=int, default=120, help="loop: seconds between pushes")
    args = ap.parse_args()
    repo = Path(args.repo)

    def one_push() -> None:
        try:
            conn = connect()
        except Exception as e:  # noqa: BLE001 — Neon blip must not stall the loop
            log(f"DB unreachable ({e}) — skipping, daemon runs off flat files")
            return
        try:
            ideas = read_ideas(repo)
            ni, nq = push(conn, ideas)
            log(f"pushed {ni} ideas, {nq} queue_items -> Neon")
        except Exception as e:  # noqa: BLE001
            log(f"push failed ({e}) — skipping this cycle")
        finally:
            conn.close()

    if args.mode == "push":
        one_push()
    elif args.mode == "pull":
        try:
            conn = connect()
        except Exception as e:  # noqa: BLE001
            log(f"DB unreachable ({e}) — nothing to pull")
            return 0
        try:
            n = pull(conn, repo, args.feed)
            log(f"{'fed' if args.feed else 'would feed'} {n} idea(s) from Neon")
        finally:
            conn.close()
    elif args.mode == "loop":
        log(f"push loop every {args.interval}s (ctrl-c to stop)")
        while True:
            one_push()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
