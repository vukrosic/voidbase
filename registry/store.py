from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("registry/experiments.sqlite")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _bool_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(bool(value))


def _load_schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "idea"


def _normalize_known_lever_status(raw_status: str) -> str:
    lowered = raw_status.lower()
    if "have" in lowered:
        return "have"
    if "open" in lowered:
        return "open"
    if "partial" in lowered or "untuned" in lowered:
        return "partial"
    if "alt" in lowered:
        return "alt"
    if "speculative" in lowered:
        return "speculative"
    return "open"


def _clean_markdown_cell(text: str) -> str:
    return re.sub(r"[*_`]", "", text).strip()


def _parse_known_lever_rows(markdown_path: Path) -> list[dict]:
    rows: list[dict] = []
    section = None
    table_re = re.compile(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$")
    for raw_line in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        match = table_re.match(line)
        if not match:
            continue
        try:
            row_num = int(match.group(1))
        except ValueError:
            continue
        lever = _clean_markdown_cell(match.group(2))
        what_it_is = _clean_markdown_cell(match.group(3))
        status = match.group(4).strip()
        notes = _clean_markdown_cell(match.group(5))
        if lever == "Lever":
            continue
        rows.append(
            {
                "row_num": row_num,
                "section": section,
                "lever": lever,
                "what_it_is": what_it_is,
                "status": _normalize_known_lever_status(status),
                "raw_status": status,
                "notes": notes,
            }
        )
    return rows


@dataclass
class MetricsPayload:
    final_metrics: Dict[str, Any]
    history: Dict[str, list]
    raw: Dict[str, Any]


def load_metrics_payload(metrics_path: Path) -> MetricsPayload:
    raw = json.loads(metrics_path.read_text(encoding="utf-8"))
    final_metrics = (
        raw.get("final_metrics")
        or raw.get("final_eval")
        or raw.get("metrics")
        or {}
    )
    history = raw.get("history") or raw.get("metrics_history") or {}
    return MetricsPayload(final_metrics=final_metrics, history=history, raw=raw)


class ExperimentRegistry:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ExperimentRegistry":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize(self) -> None:
        self.conn.executescript(_load_schema())
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        existing = {
            row[1] for row in self.conn.execute("PRAGMA table_info(ideas)").fetchall()
        }
        if "notes" not in existing:
            self.conn.execute("ALTER TABLE ideas ADD COLUMN notes TEXT")

    def upsert_thread(
        self,
        name: str,
        *,
        hypothesis: Optional[str] = None,
        status: str = "active",
        priority: int = 0,
        notes_path: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO threads (
                name, hypothesis, status, priority, notes_path, summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                hypothesis=excluded.hypothesis,
                status=excluded.status,
                priority=excluded.priority,
                notes_path=excluded.notes_path,
                summary=excluded.summary,
                updated_at=excluded.updated_at
            """,
            (name, hypothesis, status, priority, notes_path, summary, now, now),
        )
        self.conn.commit()

    def upsert_queue_item(
        self,
        thread_name: str,
        name: str,
        command: str,
        *,
        status: str = "planned",
        priority: int = 0,
        gpu_class: Optional[str] = None,
        log_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> str:
        now = utc_now()
        queue_id = self.get_queue_item_id(thread_name, name) or new_id()
        self.conn.execute(
            """
            INSERT INTO queue_items (
                id, thread_name, name, command, status, priority,
                gpu_class, created_at, started_at,
                finished_at, log_path, output_dir, decision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            ON CONFLICT(thread_name, name) DO UPDATE SET
                command=excluded.command,
                status=excluded.status,
                priority=excluded.priority,
                gpu_class=excluded.gpu_class,
                log_path=excluded.log_path,
                output_dir=excluded.output_dir,
                decision=excluded.decision
            """,
            (
                queue_id,
                thread_name,
                name,
                command,
                status,
                priority,
                gpu_class,
                now,
                log_path,
                output_dir,
                decision,
            ),
        )
        self.conn.commit()
        return queue_id

    def get_queue_item_id(self, thread_name: str, name: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT id FROM queue_items WHERE thread_name = ? AND name = ?",
            (thread_name, name),
        ).fetchone()
        return row["id"] if row else None

    def update_queue_item_status(
        self,
        thread_name: str,
        name: str,
        *,
        status: str,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        log_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> None:
        queue_id = self.get_queue_item_id(thread_name, name)
        if not queue_id:
            raise KeyError(f"queue item not found: {thread_name}/{name}")
        self.conn.execute(
            """
            UPDATE queue_items
            SET status = ?,
                started_at = COALESCE(?, started_at),
                finished_at = COALESCE(?, finished_at),
                log_path = COALESCE(?, log_path),
                output_dir = COALESCE(?, output_dir),
                decision = COALESCE(?, decision)
            WHERE id = ?
            """,
            (status, started_at, finished_at, log_path, output_dir, decision, queue_id),
        )
        self.conn.commit()

    def start_run(
        self,
        *,
        thread_name: str,
        name: str,
        status: str = "running",
        queue_item_id: Optional[str] = None,
        command: Optional[str] = None,
        config: Optional[str] = None,
        seed: Optional[int] = None,
        tokens_target: Optional[int] = None,
        output_dir: Optional[str] = None,
        metrics_path: Optional[str] = None,
        checkpoint_path: Optional[str] = None,
        git_commit: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_dirty: Optional[bool] = None,
        run_id: Optional[str] = None,
    ) -> str:
        run_id = run_id or new_id()
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO runs (
                id, queue_item_id, thread_name, name, command, config, seed,
                tokens_target, tokens_seen, actual_steps, status, verdict, output_dir,
                metrics_path, checkpoint_path, final_val_loss, final_val_accuracy,
                final_train_loss, git_commit, git_branch, git_dirty, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, NULL)
            """,
            (
                run_id,
                queue_item_id,
                thread_name,
                name,
                command,
                config,
                seed,
                tokens_target,
                status,
                output_dir,
                metrics_path,
                checkpoint_path,
                git_commit,
                git_branch,
                _bool_to_int(git_dirty),
                now,
            ),
        )
        self.conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        verdict: Optional[str] = None,
        tokens_seen: Optional[int] = None,
        actual_steps: Optional[int] = None,
        final_val_loss: Optional[float] = None,
        final_val_accuracy: Optional[float] = None,
        final_train_loss: Optional[float] = None,
        metrics_path: Optional[str] = None,
        checkpoint_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        git_commit: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_dirty: Optional[bool] = None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE runs
            SET status = ?,
                verdict = COALESCE(?, verdict),
                tokens_seen = COALESCE(?, tokens_seen),
                actual_steps = COALESCE(?, actual_steps),
                final_val_loss = COALESCE(?, final_val_loss),
                final_val_accuracy = COALESCE(?, final_val_accuracy),
                final_train_loss = COALESCE(?, final_train_loss),
                metrics_path = COALESCE(?, metrics_path),
                checkpoint_path = COALESCE(?, checkpoint_path),
                output_dir = COALESCE(?, output_dir),
                git_commit = COALESCE(?, git_commit),
                git_branch = COALESCE(?, git_branch),
                git_dirty = COALESCE(?, git_dirty),
                finished_at = ?
            WHERE id = ?
            """,
            (
                status,
                verdict,
                tokens_seen,
                actual_steps,
                final_val_loss,
                final_val_accuracy,
                final_train_loss,
                metrics_path,
                checkpoint_path,
                output_dir,
                git_commit,
                git_branch,
                _bool_to_int(git_dirty),
                now,
                run_id,
            ),
        )
        self.conn.commit()

    def record_eval_point(
        self,
        run_id: str,
        *,
        step: int,
        tokens: Optional[int] = None,
        val_loss: Optional[float] = None,
        val_accuracy: Optional[float] = None,
        val_perplexity: Optional[float] = None,
        learning_rate: Optional[float] = None,
        elapsed_seconds: Optional[float] = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO eval_points (
                run_id, step, tokens, val_loss, val_accuracy, val_perplexity,
                learning_rate, elapsed_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step,
                tokens,
                val_loss,
                val_accuracy,
                val_perplexity,
                learning_rate,
                elapsed_seconds,
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_comparison(
        self,
        run_id: str,
        *,
        baseline_name: Optional[str] = None,
        baseline_run_id: Optional[str] = None,
        matched_step: Optional[int] = None,
        matched_tokens: Optional[int] = None,
        baseline_val_loss: Optional[float] = None,
        run_val_loss: Optional[float] = None,
        delta_val_loss: Optional[float] = None,
        verdict: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO comparisons (
                run_id, baseline_name, baseline_run_id, matched_step, matched_tokens,
                baseline_val_loss, run_val_loss, delta_val_loss, verdict, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                baseline_name,
                baseline_run_id,
                matched_step,
                matched_tokens,
                baseline_val_loss,
                run_val_loss,
                delta_val_loss,
                verdict,
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_decision(
        self,
        *,
        thread_name: Optional[str] = None,
        run_id: Optional[str] = None,
        decision: str,
        reason: Optional[str] = None,
        decided_by: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO decisions (
                thread_name, run_id, decision, reason, decided_by, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_name, run_id, decision, reason, decided_by, utc_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ── Ideas: proposal → human approval → scale ────────────────────────────
    def add_idea(
        self,
        title: str,
        *,
        explanation: Optional[str] = None,
        status: str = "proposed",
        idea_id: Optional[str] = None,
    ) -> str:
        return self.upsert_idea(
            idea_id or new_id(),
            title,
            explanation=explanation,
            status=status,
            preserve_existing=False,
        )

    def upsert_idea(
        self,
        idea_id: str,
        title: str,
        *,
        explanation: Optional[str] = None,
        status: str = "proposed",
        preserve_existing: bool = True,
    ) -> str:
        now = utc_now()
        row = self.conn.execute("SELECT created_at FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        created_at = row["created_at"] if row else now
        self.conn.execute(
            """
            INSERT INTO ideas (id, title, explanation, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                explanation = excluded.explanation
                {status_clause}
            """.format(
                status_clause="" if preserve_existing else ", status = excluded.status",
            ),
            (idea_id, title, explanation, status, created_at),
        )
        self.conn.commit()
        return idea_id

    def import_known_levers(
        self,
        markdown_path: Path,
        *,
        thread_name: str = "recipe",
    ) -> int:
        imported = 0
        for row in _parse_known_lever_rows(markdown_path):
            lever_title = row["lever"]
            idea_id = f"known-lever-{row['row_num']:02d}-{_slugify(lever_title)}"
            explanation = f"Section: {row['section']}\nNotes: {row['notes']}" if row["section"] else row["notes"]
            self.upsert_idea(
                idea_id,
                lever_title,
                explanation=explanation,
                status=row["status"],
                preserve_existing=True,
            )
            imported += 1
        return imported

    def list_ideas(self, status: Optional[str] = None) -> list:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM ideas WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ideas ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_idea(self, idea_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        return dict(row) if row else None

    def delete_idea(self, idea_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM ideas WHERE id = ?",
            (idea_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_idea_notes(self, idea_id: str, notes: str) -> str:
        self.conn.execute(
            "UPDATE ideas SET notes = ? WHERE id = ?",
            (notes, idea_id),
        )
        self.conn.commit()
        return notes

    def import_metrics(
        self,
        *,
        run_id: str,
        metrics_path: Path,
        checkpoint_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        status: str = "completed",
        verdict: Optional[str] = None,
    ) -> None:
        payload = load_metrics_payload(metrics_path)
        final = payload.final_metrics
        history = payload.history
        steps = list(history.get("steps", []))
        val_losses = list(history.get("val_losses", []))
        val_accuracies = list(history.get("val_accuracies", []))
        val_perplexities = list(history.get("val_perplexities", []))
        learning_rates = list(history.get("learning_rates", []))
        elapsed_times = list(history.get("elapsed_times", []))
        tokens_series = list(history.get("tokens_seen", [])) if history.get("tokens_seen") else []

        for index, step in enumerate(steps):
            self.record_eval_point(
                run_id,
                step=_as_int(step) or 0,
                tokens=_as_int(tokens_series[index]) if index < len(tokens_series) else None,
                val_loss=_as_float(val_losses[index]) if index < len(val_losses) else None,
                val_accuracy=_as_float(val_accuracies[index]) if index < len(val_accuracies) else None,
                val_perplexity=_as_float(val_perplexities[index]) if index < len(val_perplexities) else None,
                learning_rate=_as_float(learning_rates[index]) if index < len(learning_rates) else None,
                elapsed_seconds=_as_float(elapsed_times[index]) if index < len(elapsed_times) else None,
            )

        self.finish_run(
            run_id,
            status=status,
            verdict=verdict,
            tokens_seen=_as_int(final.get("tokens_seen") or payload.raw.get("tokens_seen")),
            actual_steps=_as_int(final.get("actual_steps") or payload.raw.get("actual_steps")),
            final_val_loss=_as_float(final.get("val_loss")),
            final_val_accuracy=_as_float(final.get("val_accuracy")),
            final_train_loss=_as_float(final.get("train_loss")),
            metrics_path=str(metrics_path),
            checkpoint_path=checkpoint_path,
            output_dir=output_dir,
            git_commit=payload.raw.get("git_commit") or payload.raw.get("commit"),
            git_branch=payload.raw.get("git_branch") or payload.raw.get("branch"),
            git_dirty=_bool_to_int(payload.raw.get("git_dirty")),
        )

    def summary(self) -> Dict[str, Any]:
        def count(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "threads": count("SELECT COUNT(*) FROM threads"),
            "ideas": count("SELECT COUNT(*) FROM ideas"),
            "ideas_by_status": {
                row["status"]: row["count"]
                for row in self.conn.execute(
                    "SELECT status, COUNT(*) AS count FROM ideas GROUP BY status ORDER BY count DESC, status"
                )
            },
            "queue_items": count("SELECT COUNT(*) FROM queue_items"),
            "runs": count("SELECT COUNT(*) FROM runs"),
            "eval_points": count("SELECT COUNT(*) FROM eval_points"),
            "comparisons": count("SELECT COUNT(*) FROM comparisons"),
            "decisions": count("SELECT COUNT(*) FROM decisions"),
            "runs_by_status": {
                row["status"]: row["count"]
                for row in self.conn.execute(
                    "SELECT status, COUNT(*) AS count FROM runs GROUP BY status ORDER BY count DESC, status"
                )
            },
            "queue_by_status": {
                row["status"]: row["count"]
                for row in self.conn.execute(
                    "SELECT status, COUNT(*) AS count FROM queue_items GROUP BY status ORDER BY count DESC, status"
                )
            },
        }


def open_registry(db_path: Path | str = DEFAULT_DB_PATH) -> ExperimentRegistry:
    registry = ExperimentRegistry(db_path)
    registry.initialize()
    return registry
