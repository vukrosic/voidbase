"""Tests for voidcredit — the attribution policy (pure; no DB/network needed).

Guards: ranking is impact-first and deterministic, a contributor card derives
totals/best/champion correctly, lineage walks thread→queue_item→run→champion and
only flags a current champion when one isn't superseded, and the package stays
pure.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voidcredit as vcr  # noqa: E402


class RankTest(unittest.TestCase):
    def test_impact_ranks_above_volume(self):
        stats = [
            {"handle": "volume", "runs_total": 100, "runs_confirmed": 0, "champion_runs": 0},
            {"handle": "winner", "runs_total": 3, "runs_confirmed": 1, "champion_runs": 1},
        ]
        ranked = vcr.rank_contributors(stats)
        self.assertEqual(ranked[0]["handle"], "winner")
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[1]["handle"], "volume")
        self.assertEqual(ranked[1]["rank"], 2)

    def test_ties_break_on_handle_deterministically(self):
        stats = [{"handle": "bob"}, {"handle": "ann"}]
        ranked = vcr.rank_contributors(stats)
        self.assertEqual([r["handle"] for r in ranked], ["ann", "bob"])

    def test_confirmed_beats_volume_when_no_champions(self):
        stats = [
            {"handle": "many", "runs_total": 50, "runs_confirmed": 1},
            {"handle": "solid", "runs_total": 10, "runs_confirmed": 4},
        ]
        ranked = vcr.rank_contributors(stats)
        self.assertEqual(ranked[0]["handle"], "solid")


class ContributorCardTest(unittest.TestCase):
    def setUp(self):
        self.runs = [
            {"id": "r1", "verification": "confirmed", "final_val_loss": 6.0, "created_at": "2026-06-10"},
            {"id": "r2", "verification": "unverified", "final_val_loss": 5.5, "created_at": "2026-06-12"},
            {"id": "r3", "verification": "rejected", "final_val_loss": None, "created_at": "2026-06-11"},
        ]

    def test_totals_and_best(self):
        card = vcr.contributor_card("alice", self.runs, champion_run_ids={"r2"})
        self.assertEqual(card["runs_total"], 3)
        self.assertEqual(card["runs_confirmed"], 1)
        self.assertEqual(card["champion_runs"], 1)
        self.assertEqual(card["best_run"], {"id": "r2", "final_val_loss": 5.5})

    def test_recent_first_and_null_loss_ignored_for_best(self):
        card = vcr.contributor_card("alice", self.runs)
        self.assertEqual(card["recent_runs"][0]["id"], "r2")  # 06-12 newest
        # r3 has no loss -> never the best
        self.assertNotEqual(card["best_run"]["id"], "r3")

    def test_empty_contributor(self):
        card = vcr.contributor_card("nobody", [])
        self.assertEqual(card["runs_total"], 0)
        self.assertIsNone(card["best_run"])


class LineageTest(unittest.TestCase):
    def test_full_chain_with_current_champion(self):
        run = {"id": "r1", "verification": "confirmed", "final_val_loss": 6.0, "contributor_id": "c1"}
        out = vcr.run_lineage(
            run,
            queue_item={"id": "q1", "name": "lever-x"},
            thread={"name": "tiny1m3m", "hypothesis": "h"},
            champions=[{"run_id": "r1", "scope": "tiny1m3m", "promoted_at": "2026-06-13", "superseded_at": None}],
        )
        kinds = [c["kind"] for c in out["chain"]]
        self.assertEqual(kinds, ["thread", "queue_item", "run", "champion"])
        self.assertTrue(out["is_current_champion"])
        self.assertTrue(out["was_champion"])

    def test_superseded_champion_is_not_current(self):
        run = {"id": "r1"}
        out = vcr.run_lineage(run, champions=[
            {"run_id": "r1", "scope": "s", "superseded_at": "2026-06-14"}])
        self.assertTrue(out["was_champion"])
        self.assertFalse(out["is_current_champion"])

    def test_non_champion_run(self):
        out = vcr.run_lineage({"id": "r9"}, thread={"name": "t"})
        self.assertFalse(out["was_champion"])
        self.assertEqual([c["kind"] for c in out["chain"]], ["thread", "run"])


class PureLibraryTest(unittest.TestCase):
    FORBIDDEN_ROOTS = ("psycopg", "db", "socket", "urllib", "requests", "subprocess")

    def test_no_io_imports(self):
        import ast
        offenders = []
        for py in (ROOT / "voidcredit").glob("*.py"):
            tree = ast.parse(py.read_text())
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            offenders += [f"{py.name}: {b}" for b in self.FORBIDDEN_ROOTS if b in roots]
        self.assertEqual(offenders, [], f"voidcredit must stay pure: {offenders}")


if __name__ == "__main__":
    unittest.main()
