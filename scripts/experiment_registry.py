#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from registry.store import DEFAULT_DB_PATH, open_registry


def _is_idle(pattern: str) -> bool:
    try:
        proc = subprocess.run(
            ["bash", "-lc", f"pgrep -af {shlex.quote(pattern)} || true"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return True
    keep = [
        line
        for line in proc.stdout.splitlines()
        if pattern in line
        and "pgrep -af" not in line
        and "experiment_registry.py" not in line
    ]
    return len(keep) == 0


def _wait_for_idle(pattern: str, poll_seconds: int, label: str) -> None:
    while not _is_idle(pattern):
        print(f"[queue] waiting for {pattern!r} to be idle before {label}...", file=sys.stderr)
        time.sleep(poll_seconds)


def _run_subprocess(
    cmd: str,
    log_path: Path,
    cwd: Path,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[queue] $ {cmd}")
    print(f"[queue]   cwd={cwd}")
    print(f"[queue]   log={log_path}")
    with log_path.open("a") as log_f:
        log_f.write(f"\n### START {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        log_f.write(f"### CMD: {cmd}\n")
        log_f.write(f"### CWD: {cwd}\n")
        log_f.flush()
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            executable="/bin/bash",
        )
        rc = proc.wait()
        log_f.write(f"\n### END rc={rc}\n")
    return rc


def _select_queue_items(
    registry,
    *,
    thread_name: str | None,
    name: str | None,
    status_filter: str,
) -> list:
    sql = "SELECT * FROM queue_items WHERE status = ?"
    params: list = [status_filter]
    if thread_name:
        sql += " AND thread_name = ?"
        params.append(thread_name)
    if name:
        sql += " AND name = ?"
        params.append(name)
    sql += " ORDER BY priority DESC, created_at"
    rows = registry.conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _await_decision(registry, *, thread_name: str, poll_seconds: int) -> None:
    """Block until a decision row appears for this thread since we last looked."""
    marker = time.time()
    print(f"[queue] pause-for-decision: waiting for a `decision` row on thread={thread_name!r}")
    while True:
        row = registry.conn.execute(
            """
            SELECT id, decision, reason, decided_by, decided_at
            FROM decisions
            WHERE thread_name = ? AND decided_at >= ?
            ORDER BY decided_at DESC LIMIT 1
            """,
            (thread_name, _iso_from_epoch(marker)),
        ).fetchone()
        if row:
            print(
                f"[queue] decision received: {row['decision']} "
                f"(by={row['decided_by']}, reason={row['reason']})"
            )
            return
        time.sleep(poll_seconds)


def _iso_from_epoch(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


def run_queue_from_db(
    registry,
    *,
    thread_name: str | None,
    name: str | None,
    status_filter: str,
    wait_for_idle: bool,
    idle_pattern: str,
    poll_seconds: int,
    stop_on_failure: bool,
    pause_for_decision: bool,
) -> None:
    items = _select_queue_items(
        registry,
        thread_name=thread_name,
        name=name,
        status_filter=status_filter,
    )
    if not items:
        print(f"[queue] no queue items with status={status_filter!r} (filter: thread={thread_name}, name={name})")
        return
    print(f"[queue] picked up {len(items)} item(s)")
    for item in items:
        item_id = item["id"]
        item_name = item["name"]
        item_thread = item["thread_name"]
        cmd = item["command"]
        log_path = Path(item["log_path"]) if item["log_path"] else Path(f"logs/queue/{item_thread}__{item_name}.log")
        cwd = Path(ROOT)
        if wait_for_idle:
            _wait_for_idle(idle_pattern, poll_seconds, label=item_name)
        registry.update_queue_item_status(
            item_thread,
            item_name,
            status="running",
            started_at=registry.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0],
        )
        rc = _run_subprocess(cmd, log_path, cwd)
        finished_at = registry.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
        registry.update_queue_item_status(
            item_thread,
            item_name,
            status="done" if rc == 0 else "failed",
            finished_at=finished_at,
        )
        print(f"[queue] {item_thread}/{item_name} -> rc={rc}")
        if rc != 0 and stop_on_failure:
            print("[queue] stop-on-failure: aborting")
            return
        if pause_for_decision:
            _await_decision(registry, thread_name=item_thread, poll_seconds=poll_seconds)
    print("[queue] done")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local experiment registry")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the SQLite registry database",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("init", help="Create or upgrade the registry schema")

    thread = subparsers.add_parser("thread", help="Manage research threads")
    thread_sub = thread.add_subparsers(dest="thread_command", required=True)
    thread_upsert = thread_sub.add_parser("upsert", help="Insert or update a thread")
    thread_upsert.add_argument("--name", required=True)
    thread_upsert.add_argument("--hypothesis")
    thread_upsert.add_argument("--status", default="active")
    thread_upsert.add_argument("--priority", type=int, default=0)
    thread_upsert.add_argument("--notes-path")
    thread_upsert.add_argument("--summary")

    queue = subparsers.add_parser("queue", help="Manage queue items")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_upsert = queue_sub.add_parser("upsert", help="Insert or update a queue item")
    queue_upsert.add_argument("--thread-name", required=True)
    queue_upsert.add_argument("--name", required=True)
    queue_upsert.add_argument("--command", required=True)
    queue_upsert.add_argument("--status", default="planned")
    queue_upsert.add_argument("--priority", type=int, default=0)
    queue_upsert.add_argument("--gpu-class")
    queue_upsert.add_argument("--log-path")
    queue_upsert.add_argument("--output-dir")
    queue_upsert.add_argument("--decision")

    queue_run = queue_sub.add_parser(
        "run",
        help="Execute queued items from the DB, updating status as we go",
    )
    queue_run.add_argument("--thread-name", help="Restrict to a single thread")
    queue_run.add_argument("--name", help="Run a single queue item by name (requires --thread-name)")
    queue_run.add_argument(
        "--status-filter",
        default="queued",
        help="Status of items to pick up (default: queued)",
    )
    queue_run.add_argument(
        "--wait-for-idle",
        action="store_true",
        help="Wait until no train_llm.py is running before each job",
    )
    queue_run.add_argument("--idle-pattern", default="train_llm.py")
    queue_run.add_argument("--poll-seconds", type=int, default=30)
    queue_run.add_argument(
        "--stop-on-failure",
        action="store_true",
        default=True,
    )
    queue_run.add_argument(
        "--pause-for-decision",
        action="store_true",
        help="After each job, wait for a `decision` row in the DB before continuing",
    )

    run = subparsers.add_parser("run", help="Manage run records")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_start = run_sub.add_parser("start", help="Create a run row")
    run_start.add_argument("--thread-name", required=True)
    run_start.add_argument("--name", required=True)
    run_start.add_argument("--command")
    run_start.add_argument("--status", default="running")
    run_start.add_argument("--queue-item-id")
    run_start.add_argument("--config")
    run_start.add_argument("--seed", type=int)
    run_start.add_argument("--tokens-target", type=int)
    run_start.add_argument("--output-dir")
    run_start.add_argument("--metrics-path")
    run_start.add_argument("--checkpoint-path")
    run_start.add_argument("--git-commit")
    run_start.add_argument("--git-branch")
    run_start.add_argument("--git-dirty", action="store_true")
    run_start.add_argument("--run-id")

    run_finish = run_sub.add_parser("finish", help="Update a run row")
    run_finish.add_argument("--run-id", required=True)
    run_finish.add_argument("--status", required=True)
    run_finish.add_argument("--verdict")
    run_finish.add_argument("--tokens-seen", type=int)
    run_finish.add_argument("--actual-steps", type=int)
    run_finish.add_argument("--final-val-loss", type=float)
    run_finish.add_argument("--final-val-accuracy", type=float)
    run_finish.add_argument("--final-train-loss", type=float)
    run_finish.add_argument("--metrics-path")
    run_finish.add_argument("--checkpoint-path")
    run_finish.add_argument("--output-dir")
    run_finish.add_argument("--git-commit")
    run_finish.add_argument("--git-branch")
    run_finish.add_argument("--git-dirty", action="store_true")

    metrics = subparsers.add_parser("metrics", help="Import metrics.json data")
    metrics_sub = metrics.add_subparsers(dest="metrics_command", required=True)
    metrics_import = metrics_sub.add_parser("import", help="Load metrics.json into the registry")
    metrics_import.add_argument("--run-id", required=True)
    metrics_import.add_argument("--metrics-path", required=True, type=Path)
    metrics_import.add_argument("--checkpoint-path")
    metrics_import.add_argument("--output-dir")
    metrics_import.add_argument("--status", default="completed")
    metrics_import.add_argument("--verdict")

    comparison = subparsers.add_parser("comparison", help="Record baseline comparisons")
    comparison_sub = comparison.add_subparsers(dest="comparison_command", required=True)
    comparison_record = comparison_sub.add_parser("record", help="Insert a comparison row")
    comparison_record.add_argument("--run-id", required=True)
    comparison_record.add_argument("--baseline-name")
    comparison_record.add_argument("--baseline-run-id")
    comparison_record.add_argument("--matched-step", type=int)
    comparison_record.add_argument("--matched-tokens", type=int)
    comparison_record.add_argument("--baseline-val-loss", type=float)
    comparison_record.add_argument("--run-val-loss", type=float)
    comparison_record.add_argument("--delta-val-loss", type=float)
    comparison_record.add_argument("--verdict")

    decision = subparsers.add_parser("decision", help="Record a human decision")
    decision.add_argument("--thread-name")
    decision.add_argument("--run-id")
    decision.add_argument("--decision", required=True)
    decision.add_argument("--reason")
    decision.add_argument("--decided-by")

    idea = subparsers.add_parser("idea", help="Track experiment ideas (title + description)")
    idea_sub = idea.add_subparsers(dest="idea_command", required=True)

    idea_add = idea_sub.add_parser("add", help="Add an idea (status=proposed)")
    idea_add.add_argument("--title", required=True)
    idea_add.add_argument("--explanation", help="Step-by-step plain-English walkthrough")
    idea_add.add_argument("--status", default="proposed")

    idea_list = idea_sub.add_parser("list", help="List ideas (optionally by status)")
    idea_list.add_argument("--status", help="proposed|open|rejected|scaled|...")

    idea_delete = idea_sub.add_parser("delete", help="Permanently delete an idea")
    idea_delete.add_argument("--id", required=True)

    idea_notes = idea_sub.add_parser("set-notes", help="Set freeform notes for an idea")
    idea_notes.add_argument("--id", required=True)
    idea_notes.add_argument("--notes", required=True)

    idea_import = idea_sub.add_parser(
        "import-known-levers",
        help="Import docs/KNOWN_LEVERS.md-style lever rows into the ideas table",
    )
    idea_import.add_argument("--path", required=True, type=Path)
    idea_import.add_argument("--thread-name", default="recipe")

    subparsers.add_parser("summary", help="Print table counts and status breakdowns")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with open_registry(args.db) as registry:
        if args.action == "init":
            print(json.dumps({"db": str(Path(args.db)), "status": "initialized"}, indent=2))
            return 0
        if args.action == "summary":
            print(json.dumps(registry.summary(), indent=2, sort_keys=True))
            return 0
        if args.action == "thread":
            if args.thread_command == "upsert":
                registry.upsert_thread(
                    args.name,
                    hypothesis=args.hypothesis,
                    status=args.status,
                    priority=args.priority,
                    notes_path=args.notes_path,
                    summary=args.summary,
                )
                print(json.dumps({"thread": args.name, "status": args.status}, indent=2))
                return 0
        if args.action == "queue":
            if args.queue_command == "upsert":
                queue_id = registry.upsert_queue_item(
                    args.thread_name,
                    args.name,
                    args.command,
                    status=args.status,
                    priority=args.priority,
                    gpu_class=args.gpu_class,
                    log_path=args.log_path,
                    output_dir=args.output_dir,
                    decision=args.decision,
                )
                print(json.dumps({"queue_item_id": queue_id, "status": args.status}, indent=2))
                return 0
            if args.queue_command == "run":
                run_queue_from_db(
                    registry,
                    thread_name=args.thread_name,
                    name=args.name,
                    status_filter=args.status_filter,
                    wait_for_idle=args.wait_for_idle,
                    idle_pattern=args.idle_pattern,
                    poll_seconds=args.poll_seconds,
                    stop_on_failure=args.stop_on_failure,
                    pause_for_decision=args.pause_for_decision,
                )
                return 0
        if args.action == "run":
            if args.run_command == "start":
                run_id = registry.start_run(
                    thread_name=args.thread_name,
                    name=args.name,
                    status=args.status,
                    queue_item_id=args.queue_item_id,
                    command=args.command,
                    config=args.config,
                    seed=args.seed,
                    tokens_target=args.tokens_target,
                    output_dir=args.output_dir,
                    metrics_path=args.metrics_path,
                    checkpoint_path=args.checkpoint_path,
                    git_commit=args.git_commit,
                    git_branch=args.git_branch,
                    git_dirty=args.git_dirty,
                    run_id=args.run_id,
                )
                print(json.dumps({"run_id": run_id, "status": args.status}, indent=2))
                return 0
            if args.run_command == "finish":
                registry.finish_run(
                    args.run_id,
                    status=args.status,
                    verdict=args.verdict,
                    tokens_seen=args.tokens_seen,
                    actual_steps=args.actual_steps,
                    final_val_loss=args.final_val_loss,
                    final_val_accuracy=args.final_val_accuracy,
                    final_train_loss=args.final_train_loss,
                    metrics_path=args.metrics_path,
                    checkpoint_path=args.checkpoint_path,
                    output_dir=args.output_dir,
                    git_commit=args.git_commit,
                    git_branch=args.git_branch,
                    git_dirty=args.git_dirty,
                )
                print(json.dumps({"run_id": args.run_id, "status": args.status}, indent=2))
                return 0
        if args.action == "metrics":
            if args.metrics_command == "import":
                registry.import_metrics(
                    run_id=args.run_id,
                    metrics_path=args.metrics_path,
                    checkpoint_path=args.checkpoint_path,
                    output_dir=args.output_dir,
                    status=args.status,
                    verdict=args.verdict,
                )
                print(json.dumps({"run_id": args.run_id, "metrics_path": str(args.metrics_path)}, indent=2))
                return 0
        if args.action == "comparison":
            if args.comparison_command == "record":
                comparison_id = registry.record_comparison(
                    args.run_id,
                    baseline_name=args.baseline_name,
                    baseline_run_id=args.baseline_run_id,
                    matched_step=args.matched_step,
                    matched_tokens=args.matched_tokens,
                    baseline_val_loss=args.baseline_val_loss,
                    run_val_loss=args.run_val_loss,
                    delta_val_loss=args.delta_val_loss,
                    verdict=args.verdict,
                )
                print(json.dumps({"comparison_id": comparison_id}, indent=2))
                return 0
        if args.action == "idea":
            if args.idea_command == "add":
                idea_id = registry.add_idea(
                    args.title,
                    explanation=args.explanation,
                    status=args.status,
                )
                print(json.dumps({"idea_id": idea_id, "status": args.status}, indent=2))
                return 0
            if args.idea_command == "list":
                ideas = registry.list_ideas(status=args.status)
                for i in ideas:
                    print(json.dumps(
                        {k: i[k] for k in ("id", "status", "title")},
                        indent=2,
                    ))
                print(f"\n{len(ideas)} idea(s)")
                return 0
            if args.idea_command == "delete":
                deleted = registry.delete_idea(args.id)
                print(json.dumps({"idea_id": args.id, "deleted": deleted}, indent=2))
                return 0
            if args.idea_command == "set-notes":
                notes = registry.set_idea_notes(args.id, args.notes)
                print(json.dumps({"idea_id": args.id, "notes": notes}, indent=2))
                return 0
            if args.idea_command == "import-known-levers":
                imported = registry.import_known_levers(
                    args.path,
                    thread_name=args.thread_name,
                )
                print(json.dumps({"path": str(args.path), "imported": imported}, indent=2))
                return 0
        if args.action == "decision":
            decision_id = registry.record_decision(
                thread_name=args.thread_name,
                run_id=args.run_id,
                decision=args.decision,
                reason=args.reason,
                decided_by=args.decided_by,
            )
            print(json.dumps({"decision_id": decision_id}, indent=2))
            return 0
    raise SystemExit("Unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
