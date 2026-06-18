"""Server-free unit tests for the Voidrunner client core (runner/core.py).

Two things matter here and neither needs a DB or a network:

  1. The trust boundary: execute() REFUSES any job whose command isn't the pinned
     run_experiment.py — this is what stops "claim a job" from meaning "run
     arbitrary code on a donor's box".
  2. The hard rule: nothing in runner/ may import the DB layer (db.conn / psycopg).
     A donor's machine holds no DB creds; this test greps the package to prove it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runner import core  # noqa: E402


class ExecuteGuardTest(unittest.TestCase):
    def test_refuses_non_pinned_command(self):
        job = {"id": "j1", "command": "rm -rf /", "config": {}}
        with self.assertRaises(core.RefusedCommand):
            core.execute(job, ROOT)  # repo irrelevant — refusal happens first

    def test_refuses_arbitrary_python(self):
        job = {"id": "j2", "command": "python evil.py", "config": {}}
        with self.assertRaises(core.RefusedCommand):
            core.execute(job, ROOT)

    def test_missing_script_in_repo_is_an_error_not_a_refusal(self):
        # A legit run_experiment.py command, but a repo that doesn't contain it.
        job = {"id": "j3", "command": "python run_experiment.py", "config": {"seed": 1}}
        with self.assertRaises(FileNotFoundError):
            core.execute(job, ROOT / "does-not-exist")

    def test_runs_pinned_script_and_parses_metrics(self):
        # A fake run_experiment.py that prints the metric line the trainer prints.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "run_experiment.py").write_text(
                "print('Final Val Loss: 6.2219')\n")
            job = {"id": "j4", "command": "python run_experiment.py",
                   "config": {"seed": 42}, "content_hash": "abc"}
            result = core.execute(job, repo, python=sys.executable, timeout=30)
            self.assertTrue(result["ok"], result.get("stdout"))
            self.assertEqual(result["final_val_loss"], 6.2219)
            self.assertEqual(result["seed"], 42)
            self.assertEqual(result["queue_item_id"], "j4")
            self.assertEqual(result["content_hash"], "abc")

    def test_config_is_passed_via_env_not_shell(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            # Echo the config back out of the environment to prove it arrived there.
            (repo / "run_experiment.py").write_text(
                "import os, json\n"
                "c = json.loads(os.environ['EXPERIMENT_CONFIG'])\n"
                "print('Final Val Loss:', c['seed'] / 10)\n")
            job = {"id": "j5", "command": "python run_experiment.py",
                   "config": {"seed": 50}}
            result = core.execute(job, repo, python=sys.executable, timeout=30)
            self.assertEqual(result["final_val_loss"], 5.0)


class NoDbImportsTest(unittest.TestCase):
    """The package must not reach the DB. A static AST check over actual import
    statements (not docstring mentions), so it catches a regression even on a box
    with no psycopg installed and never false-flags a comment."""

    FORBIDDEN_ROOTS = ("psycopg", "db")  # `db` = the voidbase db.* package

    def _imported_roots(self, tree) -> set[str]:
        import ast
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_runner_package_has_no_db_imports(self):
        import ast
        offenders = []
        for py in (ROOT / "runner").glob("*.py"):
            roots = self._imported_roots(ast.parse(py.read_text()))
            for bad in self.FORBIDDEN_ROOTS:
                if bad in roots:
                    offenders.append(f"{py.name}: imports {bad!r}")
        self.assertEqual(offenders, [], f"runner/ must not import the DB: {offenders}")


if __name__ == "__main__":
    unittest.main()
