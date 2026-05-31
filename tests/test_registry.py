import json
import tempfile
import unittest
from pathlib import Path

from registry.store import ExperimentRegistry


class ExperimentRegistryTest(unittest.TestCase):
    def test_init_and_basic_ingest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "experiments.sqlite"
            metrics_path = Path(tmpdir) / "metrics.json"
            metrics_payload = {
                "final_metrics": {
                    "val_loss": 4.5,
                    "val_accuracy": 0.25,
                    "train_loss": 4.2,
                    "tokens_seen": 4096,
                    "actual_steps": 2,
                },
                "history": {
                    "steps": [1, 2],
                    "val_losses": [4.8, 4.5],
                    "val_accuracies": [0.2, 0.25],
                    "val_perplexities": [10.0, 9.5],
                    "elapsed_times": [1.0, 2.0],
                    "learning_rates": [0.1, 0.05],
                },
                "git_commit": "abc123",
                "git_branch": "main",
                "git_dirty": False,
            }
            metrics_path.write_text(json.dumps(metrics_payload), encoding="utf-8")

            with ExperimentRegistry(db_path) as registry:
                registry.initialize()
                registry.upsert_thread("layerscale", hypothesis="test", status="active")
                queue_id = registry.upsert_queue_item(
                    "layerscale",
                    "screen5m",
                    "python train_llm.py --config screen5m",
                    status="planned",
                )
                run_id = registry.start_run(
                    thread_name="layerscale",
                    name="screen5m",
                    command="python train_llm.py --config screen5m",
                    queue_item_id=queue_id,
                )
                registry.import_metrics(run_id=run_id, metrics_path=metrics_path)
                registry.record_comparison(
                    run_id,
                    baseline_name="screen5m_baseline",
                    matched_step=2,
                    matched_tokens=4096,
                    baseline_val_loss=4.6,
                    run_val_loss=4.5,
                    delta_val_loss=-0.1,
                    verdict="scale",
                )
                registry.record_decision(
                    thread_name="layerscale",
                    run_id=run_id,
                    decision="scale",
                    reason="beats baseline",
                    decided_by="test",
                )
                idea_id = registry.add_idea(
                    "Example lever",
                    command="python train_llm.py --config 10m",
                    thread_name="layerscale",
                    status="proposed",
                    proposed_by="test",
                )
                queue_id_2 = registry.approve_and_promote_idea(
                    idea_id,
                    reviewed_by="test",
                    review_note="looks good",
                    created_by="test",
                )

                summary = registry.summary()
                self.assertEqual(summary["threads"], 1)
                self.assertEqual(summary["queue_items"], 2)
                self.assertEqual(summary["runs"], 1)
                self.assertEqual(summary["eval_points"], 2)
                self.assertEqual(summary["comparisons"], 1)
                self.assertEqual(summary["decisions"], 1)
                self.assertEqual(summary["ideas"], 1)
                self.assertEqual(summary["runs_by_status"]["completed"], 1)
                self.assertEqual(summary["queue_by_status"]["planned"], 2)

                idea = registry.get_idea(idea_id)
                self.assertEqual(idea["status"], "queued")
                self.assertEqual(idea["queue_item_id"], queue_id_2)

                row = registry.conn.execute(
                    "SELECT final_val_loss, final_val_accuracy, final_train_loss, git_commit, git_branch, git_dirty "
                    "FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                self.assertAlmostEqual(row["final_val_loss"], 4.5)
                self.assertAlmostEqual(row["final_val_accuracy"], 0.25)
                self.assertAlmostEqual(row["final_train_loss"], 4.2)
                self.assertEqual(row["git_commit"], "abc123")
                self.assertEqual(row["git_branch"], "main")
                self.assertEqual(row["git_dirty"], 0)

    def test_import_known_levers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "experiments.sqlite"
            markdown_path = Path(tmpdir) / "KNOWN_LEVERS.md"
            markdown_path.write_text(
                """# Known Levers

### Example Section
| # | Lever | What it is | Status | Notes / where |
|---|---|---|---|---|
| 1 | **Value residual / value embeddings** | Learnable mix of value across depth | 🔲 open | modded-nanoGPT trick |
| 2 | **Zero-init output projections** | Init attn-out & MLP-out to 0 | ✅ have | already in model |
""",
                encoding="utf-8",
            )

            with ExperimentRegistry(db_path) as registry:
                registry.initialize()
                imported = registry.import_known_levers(markdown_path)
                self.assertEqual(imported, 2)

                rows = registry.conn.execute(
                    "SELECT title, status, priority, outcome, reference_url, thread_name "
                    "FROM ideas ORDER BY priority, title"
                ).fetchall()
                self.assertEqual(rows[0]["title"], "Value residual / value embeddings")
                self.assertEqual(rows[0]["status"], "open")
                self.assertEqual(rows[0]["priority"], 0)
                self.assertEqual(rows[0]["outcome"], "🔲 open")
                self.assertEqual(rows[0]["thread_name"], "recipe")
                self.assertTrue(str(rows[0]["reference_url"]).endswith("KNOWN_LEVERS.md"))
                self.assertEqual(rows[1]["status"], "have")


if __name__ == "__main__":
    unittest.main()
