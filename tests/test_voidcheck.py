"""Tests for voidcheck — the result-integrity library.

These guard the rules past bugs slipped through: a delta is only signal when it's
paired (same seed AND box), the screen gate rejects sub-band margins, and a
candidate only AGREEs with all seeds present, the mean past the band, and every
seed individually favouring it. Pure functions, so no DB / network needed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voidcheck as vc  # noqa: E402


def _jobs(cand, base, seeds=vc.SEEDS):
    jobs = []
    for seed, val in zip(seeds, cand):
        jobs.append({"arm": "cand", "seed": seed, "val": val})
    for seed, val in zip(seeds, base):
        jobs.append({"arm": "base", "seed": seed, "val": val})
    return jobs


class IsPairedTest(unittest.TestCase):
    def test_same_seed_same_box_is_paired(self):
        self.assertTrue(vc.is_paired(42, "boxA", 42, "boxA"))

    def test_uuid_object_matches_its_text_form(self):
        import uuid
        u = uuid.uuid4()
        self.assertTrue(vc.is_paired(7, u, 7, str(u)))

    def test_different_seed_not_paired(self):
        self.assertFalse(vc.is_paired(42, "boxA", 123, "boxA"))

    def test_different_box_not_paired(self):
        self.assertFalse(vc.is_paired(42, "boxA", 42, "boxB"))

    def test_any_null_not_paired(self):
        # The fake-NULL bug: a missing seed or box must never read as paired.
        self.assertFalse(vc.is_paired(None, "boxA", None, "boxA"))
        self.assertFalse(vc.is_paired(42, None, 42, None))
        self.assertFalse(vc.is_paired(42, "boxA", None, "boxA"))
        self.assertFalse(vc.is_paired(42, "boxA", 42, None))


class BeatsScreenTest(unittest.TestCase):
    # Expressed RELATIVE to vc.SCREEN_BAND so the test can never drift from the
    # single source of truth: a margin comfortably over/under the band, whatever
    # the band currently is.
    def test_clear_win_passes(self):
        champ = 6.10
        self.assertTrue(vc.beats_screen(champ - vc.SCREEN_BAND * 5, champ))

    def test_sub_band_margin_fails(self):
        champ = 6.10
        self.assertFalse(vc.beats_screen(champ - vc.SCREEN_BAND * 0.5, champ))

    def test_just_over_band_passes(self):
        champ = 6.10
        self.assertTrue(vc.beats_screen(champ - vc.SCREEN_BAND * 1.5, champ))

    def test_equal_fails(self):
        self.assertFalse(vc.beats_screen(6.10, 6.10))

    def test_worse_fails(self):
        self.assertFalse(vc.beats_screen(6.20, 6.10))

    def test_null_fails(self):
        self.assertFalse(vc.beats_screen(None, 6.10))
        self.assertFalse(vc.beats_screen(6.0, None))


class IsImplausibleWinTest(unittest.TestCase):
    def test_nonsense_low_loss_is_implausible(self):
        # The live case: a 0.4388 run against a 6.172 champion — ~14x better,
        # a broken/forged metric, must not auto-trigger a confirm.
        self.assertTrue(vc.is_implausible_win(0.4388, 6.172))

    def test_genuine_win_is_plausible(self):
        # A real, even large, improvement clears it with margin (26% better).
        self.assertFalse(vc.is_implausible_win(4.549, 6.172))
        self.assertFalse(vc.is_implausible_win(5.015, 6.172))

    def test_exactly_half_is_the_boundary(self):
        # default factor 0.5: strictly-below is implausible, at-or-above is fine.
        self.assertTrue(vc.is_implausible_win(3.0, 6.172))     # < 3.086
        self.assertFalse(vc.is_implausible_win(3.086, 6.172))  # == boundary

    def test_non_positive_loss_is_definitionally_broken(self):
        self.assertTrue(vc.is_implausible_win(0.0, 6.0))
        self.assertTrue(vc.is_implausible_win(-1.0, 6.0))

    def test_cannot_assess_does_not_flag(self):
        # Missing values, or a degenerate non-positive champion -> don't reject.
        self.assertFalse(vc.is_implausible_win(None, 6.0))
        self.assertFalse(vc.is_implausible_win(3.0, None))
        self.assertFalse(vc.is_implausible_win(1.0, 0.0))

    def test_factor_is_tunable(self):
        # A stricter floor flags a smaller improvement; a looser one permits it.
        self.assertTrue(vc.is_implausible_win(5.0, 6.172, max_drop_factor=0.9))
        self.assertFalse(vc.is_implausible_win(0.4, 6.172, max_drop_factor=0.05))


class PairedVerdictTest(unittest.TestCase):
    def test_candidate_better_at_all_seeds_agrees(self):
        v = vc.paired_verdict(_jobs([6.00, 6.01, 5.99], [6.10, 6.12, 6.08]), 0.001)
        self.assertTrue(v["agrees"])
        self.assertEqual(v["n_pairs"], 3)
        expected = (6.00 + 6.01 + 5.99) / 3 - (6.10 + 6.12 + 6.08) / 3
        self.assertAlmostEqual(v["delta"], expected, places=9)

    def test_mixed_sign_fails_sign_consistency(self):
        # Better on the mean but loses one seed -> not confirmed (the lucky-seed guard).
        v = vc.paired_verdict(_jobs([6.00, 6.20, 5.90], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertLess(v["delta"], 0)

    def test_candidate_worse_rejects(self):
        v = vc.paired_verdict(_jobs([6.20, 6.21, 6.19], [6.10, 6.12, 6.08]), 0.001)
        self.assertFalse(v["agrees"])
        self.assertGreater(v["delta"], 0)

    def test_incomplete_pairs_never_agree(self):
        # Only 2 of 3 seeds produced a paired val -> not complete -> reject even if
        # both favour the candidate.
        jobs = _jobs([6.00, 6.01], [6.10, 6.12], seeds=(42, 123))
        v = vc.paired_verdict(jobs, 0.001)
        self.assertEqual(v["n_pairs"], 2)
        self.assertFalse(v["agrees"])

    def test_all_failed_runs_reject(self):
        jobs = _jobs([None, None, None], [None, None, None])
        v = vc.paired_verdict(jobs, 0.001)
        self.assertEqual(v["n_pairs"], 0)
        self.assertFalse(v["agrees"])
        self.assertIsNone(v["delta"])

    def test_within_band_does_not_agree(self):
        # Favours candidate at all seeds but the mean margin is inside the band.
        v = vc.paired_verdict(_jobs([6.0995, 6.0995, 6.0995],
                                    [6.1000, 6.1000, 6.1000]), 0.001)
        self.assertFalse(v["agrees"])  # 0.0005 margin < 0.001 band


class PureLibraryTest(unittest.TestCase):
    """voidcheck must stay pure: no DB / network / filesystem imports, so it can be
    vendored anywhere a result needs checking."""

    FORBIDDEN_ROOTS = ("psycopg", "db", "socket", "urllib", "requests", "subprocess")

    def test_no_io_imports(self):
        import ast
        offenders = []
        for py in (ROOT / "voidcheck").glob("*.py"):
            tree = ast.parse(py.read_text())
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            offenders += [f"{py.name}: {b}" for b in self.FORBIDDEN_ROOTS if b in roots]
        self.assertEqual(offenders, [], f"voidcheck must stay pure: {offenders}")


if __name__ == "__main__":
    unittest.main()
