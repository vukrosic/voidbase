"""Pure-logic tests for scripts/confirm_daemon.py.

The DB-touching paths need a live Postgres; these cover the parts that decide
the verdict — the paired delta, the AGREE rule, and the confirm queue-id
round-trip — without one.
"""
import unittest

from scripts.confirm_daemon import _confirm_qid, _parse_arm_seed, paired_verdict, SEEDS


def _jobs(cand, base):
    """Build the 6 collected confirm jobs from matched cand/base val lists."""
    jobs = []
    for seed, val in zip(SEEDS, cand):
        jobs.append({"arm": "cand", "seed": seed, "val": val, "box": None})
    for seed, val in zip(SEEDS, base):
        jobs.append({"arm": "base", "seed": seed, "val": val, "box": None})
    return jobs


class PairedVerdictTest(unittest.TestCase):
    def test_candidate_better_at_all_seeds_agrees(self):
        v = paired_verdict(_jobs([6.00, 6.01, 5.99], [6.10, 6.12, 6.08]), 0.001)
        self.assertTrue(v["agrees"])
        self.assertEqual(v["n_pairs"], 3)
        expected = (6.00 + 6.01 + 5.99) / 3 - (6.10 + 6.12 + 6.08) / 3
        self.assertAlmostEqual(v["delta"], expected, places=9)

    def test_mixed_sign_fails_sign_consistency(self):
        # Better on the mean but loses one seed -> not confirmed.
        v = paired_verdict(_jobs([6.00, 6.20, 5.90], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertLess(v["delta"], 0)

    def test_candidate_worse_rejects(self):
        v = paired_verdict(_jobs([6.20, 6.21, 6.19], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertGreater(v["delta"], 0)

    def test_incomplete_pairs_cannot_confirm(self):
        v = paired_verdict(_jobs([6.00, 6.01, None], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertEqual(v["n_pairs"], 2)

    def test_all_failed_no_delta(self):
        v = paired_verdict(_jobs([None, None, None], [None, None, None]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertIsNone(v["delta"])
        self.assertEqual(v["n_pairs"], 0)

    def test_within_band_rejects(self):
        v = paired_verdict(_jobs([6.0995, 6.1195, 6.0795], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])


class ConfirmQueueIdTest(unittest.TestCase):
    def test_six_unique_ids_round_trip(self):
        rid = "auto-use_rmsnorm+use_qk_norm-1a2b3c4d--9f8e7d6c"
        ids = [_confirm_qid(rid, arm, seed)
               for seed in SEEDS for arm in ("cand", "base")]
        self.assertEqual(len(set(ids)), 6)
        for seed in SEEDS:
            self.assertEqual(_parse_arm_seed(_confirm_qid(rid, "cand", seed), rid),
                             ("cand", seed))
            self.assertEqual(_parse_arm_seed(_confirm_qid(rid, "base", seed), rid),
                             ("base", seed))

    def test_other_run_id_does_not_parse(self):
        rid = "auto-use_rmsnorm-1a2b3c4d--9f8e7d6c"
        other = _confirm_qid("some-other-run", "cand", 42)
        self.assertIsNone(_parse_arm_seed(other, rid))


if __name__ == "__main__":
    unittest.main()
