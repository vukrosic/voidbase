"""Tests for the Voidmind client core — server-free (no network, no DB).

Drives the loop with a scripted proposer and a fake transport so we can assert the
mechanism without a live API:
  * a proposal's field/env deltas merge onto the champion base correctly;
  * run_once posts an idea + a queue_item per proposal, carries the lever, and
    dedups repeats WITHIN a pass;
  * the LLM-reply parser tolerates prose/fences and bad JSON;
  * the package imports no DB layer (it runs on a donor's box).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voidconfig  # noqa: E402
from voidmind import core  # noqa: E402
from voidmind.propose import (  # noqa: E402
    _build_prompt, _extract_json_array, static_proposer,
)


BASE = {
    "config_class": "configs.llm_config.Tiny1M3MAlibiConfig",
    "env": {"BASE_ENV": "1"},
    "fields": {"champ_flag": True},
    "seed": 42,
}


class ResolveProposalTest(unittest.TestCase):
    def test_deltas_merge_onto_base(self):
        p = {"lever": "rope-xpos", "fields": {"use_xpos": True},
             "env": {"EXTRA": "9"}, "explanation": "why"}
        c = core.resolve_proposal(p, BASE)
        self.assertEqual(c["config_class"], BASE["config_class"])
        self.assertEqual(c["fields"], {"champ_flag": True, "use_xpos": True})
        self.assertEqual(c["env"], {"BASE_ENV": "1", "EXTRA": "9"})
        self.assertEqual(c["seed"], 42)
        self.assertEqual(c["lever"], "rope-xpos")

    def test_proposal_seed_overrides_base(self):
        c = core.resolve_proposal({"lever": "l", "seed": 123}, BASE)
        self.assertEqual(c["seed"], 123)

    def test_requires_config_class(self):
        with self.assertRaises(ValueError):
            core.resolve_proposal({"lever": "l", "fields": {"x": True}}, None)

    def test_config_class_from_proposal_when_no_base(self):
        c = core.resolve_proposal(
            {"lever": "l", "config_class": "C", "fields": {"x": True}}, None)
        self.assertEqual(c["config_class"], "C")


class _FakeTransport:
    """Captures the HTTP calls run_once makes, answering reads with canned data."""

    def __init__(self, tried_runs=None):
        self.posts = []
        self.tried_runs = tried_runs or []

    def __call__(self, api, path, *, method, body=None, token=None, timeout=30):
        if method == "GET":
            if path.startswith("/threads/goal"):
                return {"name": "t", "goal_prompt": "make it better"}
            if path == "/runs":
                return self.tried_runs
            return []
        self.posts.append((path, body, token))
        if path == "/queue_items":
            ch = voidconfig.content_hash(body["config"]["env"], body["config"]["fields"])
            return {"deduped": False, "queue_item_id": f"mind-x-{ch[:8]}",
                    "content_hash": ch}
        if path == "/ideas":
            return {"id": "idea-1", "title": body["title"]}
        return {}


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self._orig = core._request

    def tearDown(self):
        core._request = self._orig

    def test_posts_idea_and_queue_per_proposal(self):
        fake = _FakeTransport()
        core._request = fake
        proposer = static_proposer([
            {"lever": "a", "fields": {"use_a": True}, "explanation": "ea"},
            {"lever": "b", "fields": {"use_b": True}, "explanation": "eb"},
        ])
        results = core.run_once("http://x", "tok", "t", BASE, proposer, limit=5)
        paths = [p for p, _, _ in fake.posts]
        self.assertEqual(paths.count("/ideas"), 2)
        self.assertEqual(paths.count("/queue_items"), 2)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["lever"] for r in results}, {"a", "b"})
        # The queue_item bodies carry a fully-resolved config + the right thread.
        for path, body, tok in fake.posts:
            self.assertEqual(tok, "tok")
            if path == "/queue_items":
                self.assertEqual(body["thread"], "t")
                self.assertIn("config_class", body["config"])

    def test_local_dedup_within_pass(self):
        fake = _FakeTransport()
        core._request = fake
        # Two proposals that resolve to the SAME config (same fields/env) → one enqueue.
        proposer = static_proposer([
            {"lever": "a", "fields": {"use_a": True}},
            {"lever": "a-again", "fields": {"use_a": True}},
        ])
        results = core.run_once("http://x", "tok", "t", BASE, proposer)
        self.assertEqual(len([p for p, _, _ in fake.posts if p == "/queue_items"]), 1)
        self.assertEqual(len(results), 1)

    def test_dry_run_writes_nothing(self):
        fake = _FakeTransport()
        core._request = fake
        proposer = static_proposer([{"lever": "a", "fields": {"use_a": True}}])
        results = core.run_once("http://x", "tok", "t", BASE, proposer, dry=True)
        self.assertEqual(fake.posts, [])
        self.assertTrue(results[0]["dry"])
        self.assertIn("content_hash", results[0])

    def test_limit_caps_enqueues(self):
        fake = _FakeTransport()
        core._request = fake
        proposer = static_proposer(
            [{"lever": f"l{i}", "fields": {f"f{i}": True}} for i in range(10)])
        results = core.run_once("http://x", "tok", "t", BASE, proposer, limit=3)
        self.assertEqual(len(results), 3)

    def test_bad_proposal_reported_not_crashing(self):
        fake = _FakeTransport()
        core._request = fake
        # No base, no config_class → resolve_proposal raises, captured as an error.
        proposer = static_proposer([{"lever": "x", "fields": {"a": True}}])
        results = core.run_once("http://x", "tok", "t", None, proposer)
        self.assertIn("error", results[0])
        self.assertEqual([p for p, _, _ in fake.posts], [])


class RankContendersTest(unittest.TestCase):
    """The pure fitness-landscape distiller: best runs by val_loss, signed margin,
    confirm-machinery + failed rows filtered out."""

    RUNS = [
        {"name": "lever-good", "final_val_loss": 6.150, "status": "done",
         "verification": "unverified"},
        {"name": "lever-best", "final_val_loss": 6.140, "status": "done",
         "verification": "confirmed"},
        {"name": "lever-bad", "final_val_loss": 6.300, "status": "done",
         "verification": "unverified"},
        {"name": "lever-broke", "final_val_loss": None, "status": "failed",
         "verification": "unverified"},
        {"name": "confirm-foo-cand-s7", "final_val_loss": 6.10, "status": "done",
         "verification": "unverified"},
    ]

    def test_ranks_by_val_loss_with_margin(self):
        out = core.rank_contenders(self.RUNS, champion_val=6.172, top=8)
        names = [c["name"] for c in out]
        # best (lowest val) first; failed + confirm-* dropped.
        self.assertEqual(names, ["lever-best", "lever-good", "lever-bad"])
        self.assertAlmostEqual(out[0]["margin"], 0.032, places=4)  # beats champion
        self.assertAlmostEqual(out[2]["margin"], -0.128, places=4)  # worse

    def test_top_caps_length(self):
        out = core.rank_contenders(self.RUNS, champion_val=6.172, top=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "lever-best")

    def test_no_champion_val_leaves_margin_none(self):
        out = core.rank_contenders(self.RUNS, champion_val=None)
        self.assertTrue(all(c["margin"] is None for c in out))


class _OutcomeTransport:
    """A read-only fake that answers /gate + /champions so build_context can
    assemble the full outcome signal without a live API."""

    def __init__(self):
        self.runs = [
            {"name": "use_x", "thread_name": "t", "final_val_loss": 6.16,
             "status": "done", "verification": "unverified"},
            {"name": "use_y", "thread_name": "t", "final_val_loss": 6.30,
             "status": "done", "verification": "unverified"},
        ]

    def __call__(self, api, path, *, method, body=None, token=None, timeout=30):
        if path.startswith("/threads/goal"):
            return {"name": "t", "goal_prompt": "lower val_loss"}
        if path == "/runs":
            return self.runs
        if path.startswith("/gate"):
            return {
                "scope": "t",
                "champion": {"run_id": "champ-1", "val_loss": 6.172},
                "clears": [{"name": "use_combo", "val_loss": 6.15, "margin": 0.022}],
                "near_miss": None,
                "recent_verdicts": [
                    {"name": "use_bad", "run_id": "r9", "agrees": False, "delta": 0.002},
                    {"name": "use_x", "run_id": "r8", "agrees": True, "delta": -0.01},
                ],
            }
        if path == "/champions":
            return [
                {"scope": "t", "val_loss": 6.24, "reason": "alibi slopes. detail",
                 "promoted_at": "2026-06-15"},
                {"scope": "t", "val_loss": 6.17, "reason": "momentum. detail",
                 "promoted_at": "2026-06-17"},
                {"scope": "other", "val_loss": 9.0, "reason": "noise",
                 "promoted_at": "2026-06-10"},
            ]
        return []


class BuildContextOutcomeTest(unittest.TestCase):
    def setUp(self):
        self._orig = core._request
        core._request = _OutcomeTransport()

    def tearDown(self):
        core._request = self._orig

    def test_context_carries_fitness_landscape(self):
        ctx = core.build_context("http://x", "t")
        self.assertEqual(ctx["champion"]["val_loss"], 6.172)
        self.assertEqual([c["name"] for c in ctx["frontier"]], ["use_combo"])
        # contenders ranked best-first, with margins vs the champion
        self.assertEqual([c["name"] for c in ctx["contenders"]], ["use_x", "use_y"])
        self.assertAlmostEqual(ctx["contenders"][0]["margin"], 0.012, places=4)
        # lineage scoped to this thread + oldest-first
        self.assertEqual([round(c["val_loss"], 2) for c in ctx["lineage"]], [6.24, 6.17])
        self.assertEqual(len(ctx["verdicts"]), 2)

    def test_prompt_renders_landscape_sections(self):
        ctx = core.build_context("http://x", "t")
        ctx["base"] = BASE
        prompt = _build_prompt(ctx, n=3)
        self.assertIn("Confirmed promotion arc", prompt)
        self.assertIn("Frontier", prompt)
        self.assertIn("Ranked contenders", prompt)
        self.assertIn("Recently REJECTED", prompt)
        self.assertIn("use_bad", prompt)        # the rejected lever is named
        # the rejected LINE names only the disagreeing lever, not the confirmed one
        rejected_line = next(ln for ln in prompt.splitlines() if "do not "
                             "re-propose" in ln)
        self.assertIn("use_bad", rejected_line)
        self.assertNotIn("use_x", rejected_line)


class BuildContextDegradesTest(unittest.TestCase):
    """A backend without the gate (older API) must still yield a usable context —
    the outcome fields just come back empty, the loop keeps working."""

    def setUp(self):
        self._orig = core._request
        core._request = _FakeTransport(tried_runs=[
            {"name": "use_a", "thread_name": "t", "final_val_loss": 6.1,
             "status": "done"}])

    def tearDown(self):
        core._request = self._orig

    def test_no_gate_no_champions_degrades_cleanly(self):
        ctx = core.build_context("http://x", "t")
        self.assertIsNone(ctx["champion"])
        self.assertEqual(ctx["frontier"], [])
        self.assertEqual(ctx["lineage"], [])
        self.assertEqual(ctx["verdicts"], [])
        # contenders still rank from runs, margin None without a champion val
        self.assertEqual([c["name"] for c in ctx["contenders"]], ["use_a"])
        self.assertIsNone(ctx["contenders"][0]["margin"])


class ExtractJsonArrayTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(_extract_json_array('[{"lever": "a"}]'), [{"lever": "a"}])

    def test_prose_and_fence_tolerated(self):
        text = 'Sure!\n```json\n[{"lever": "a"}, {"lever": "b"}]\n```\nDone.'
        self.assertEqual(len(_extract_json_array(text)), 2)

    def test_garbage_returns_empty(self):
        self.assertEqual(_extract_json_array("no json here"), [])
        self.assertEqual(_extract_json_array("[not valid json"), [])
        self.assertEqual(_extract_json_array(""), [])


class NoDbImportsTest(unittest.TestCase):
    """Voidmind runs on a donor's box — it must hold no DB layer, only HTTP."""
    FORBIDDEN_ROOTS = ("psycopg", "db")

    def test_no_db_imports(self):
        import ast
        offenders = []
        for py in (ROOT / "voidmind").glob("*.py"):
            tree = ast.parse(py.read_text())
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            offenders += [f"{py.name}: {b}" for b in self.FORBIDDEN_ROOTS if b in roots]
        self.assertEqual(offenders, [], f"voidmind must not import a DB layer: {offenders}")


if __name__ == "__main__":
    unittest.main()
