"""Tests for voidconfig — the config-row shape + dedup-key policy (pure).

Guards the two things every queue-row writer must agree on:
  * content_hash is stable, seed-independent, and BYTE-IDENTICAL to the feeder's
    (the drift guard — if these ever diverge, dedup silently breaks);
  * validate_config rejects malformed rows so the queue can't be poisoned;
and that the package stays pure (no I/O imports).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voidconfig  # noqa: E402


class ContentHashTest(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a = voidconfig.content_hash({"B": "2", "A": "1"}, {"y": True, "x": False})
        b = voidconfig.content_hash({"A": "1", "B": "2"}, {"x": False, "y": True})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_seed_independent(self):
        # The hash is over env+fields only — re-running on another seed is a
        # pairing, not a new experiment, so it must hash the same.
        env, fields = {"E": "1"}, {"use_thing": True}
        h = voidconfig.content_hash(env, fields)
        # Building two resolved configs that differ ONLY by seed → same hash.
        c1 = voidconfig.resolve_config("C", env, fields, 42, "lever")
        c2 = voidconfig.resolve_config("C", env, fields, 123, "lever")
        self.assertEqual(
            voidconfig.content_hash(c1["env"], c1["fields"]),
            voidconfig.content_hash(c2["env"], c2["fields"]))
        self.assertEqual(h, voidconfig.content_hash(c1["env"], c1["fields"]))

    def test_different_fields_different_hash(self):
        self.assertNotEqual(
            voidconfig.content_hash({}, {"a": True}),
            voidconfig.content_hash({}, {"a": False}))

    def test_matches_feeder_exactly(self):
        """The drift guard: voidconfig must hash identically to scripts/feeder,
        which re-exports it. If someone re-inlines feeder's hash and it drifts,
        dedup across the auto-feeder and the API breaks silently — this fails."""
        from scripts import feeder
        cases = [
            ({}, {}),
            ({"FOO": "bar"}, {"use_x": True, "n": 3}),
            ({"A": "1", "B": "2"}, {"z": False, "a": True, "m": "k"}),
        ]
        for env, fields in cases:
            self.assertEqual(
                voidconfig.content_hash(env, fields),
                feeder.content_hash(env, fields),
                f"hash drift on env={env} fields={fields}")


class ResolveAndValidateTest(unittest.TestCase):
    def test_resolve_shape(self):
        c = voidconfig.resolve_config("configs.X", {"E": "1"}, {"f": True}, 42, "lev")
        self.assertEqual(set(c), {"config_class", "env", "fields", "seed",
                                  "dataset_path", "lever"})
        self.assertEqual(c["config_class"], "configs.X")
        self.assertEqual(c["seed"], 42)
        self.assertEqual(c["lever"], "lev")
        self.assertEqual(c["dataset_path"], voidconfig.DEFAULT_DATASET_PATH)

    def test_validate_accepts_good(self):
        c = voidconfig.resolve_config("C", {}, {"f": True}, 7, "l")
        self.assertIs(voidconfig.validate_config(c), c)

    def test_validate_rejects_junk(self):
        bad = [
            "not a dict",
            {"env": {}, "fields": {}},                       # no config_class
            {"config_class": "", "env": {}, "fields": {}},    # empty config_class
            {"config_class": "C", "env": [], "fields": {}},   # env not a dict
            {"config_class": "C", "env": {}, "fields": 3},    # fields not a dict
            {"config_class": "C", "env": {}, "fields": {}, "seed": "42"},  # seed str
        ]
        for b in bad:
            with self.assertRaises(ValueError):
                voidconfig.validate_config(b)

    def test_seed_may_be_null(self):
        voidconfig.validate_config(
            {"config_class": "C", "env": {}, "fields": {}, "seed": None})


class IdNamingTest(unittest.TestCase):
    def test_id_and_name_carry_hash(self):
        chash = "a" * 32
        qid = voidconfig.queue_item_id("mind", "Rope XPos!", chash)
        self.assertTrue(qid.startswith("mind-"))
        self.assertIn(chash[:8], qid)
        self.assertNotIn(" ", qid)  # slugged
        name = voidconfig.queue_item_name("rope-xpos", chash)
        self.assertIn(chash[:6], name)


class PureLibraryTest(unittest.TestCase):
    FORBIDDEN_ROOTS = ("psycopg", "db", "socket", "urllib", "requests", "subprocess")

    def test_no_io_imports(self):
        import ast
        offenders = []
        for py in (ROOT / "voidconfig").glob("*.py"):
            tree = ast.parse(py.read_text())
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            offenders += [f"{py.name}: {b}" for b in self.FORBIDDEN_ROOTS if b in roots]
        self.assertEqual(offenders, [], f"voidconfig must stay pure: {offenders}")


if __name__ == "__main__":
    unittest.main()
