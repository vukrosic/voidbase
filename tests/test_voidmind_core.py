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
from voidmind.propose import _extract_json_array, static_proposer  # noqa: E402


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
