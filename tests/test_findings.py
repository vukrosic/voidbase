"""Pure-logic tests for scripts/findings.bucket_for — the evidence classifier.

The research summary's honesty depends on this: a paired verdict must outrank any
single-run heuristic, a nonsense-low val must be screened as implausible (not a
lead), and the band must separate a real lead from in-band noise.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.findings import bucket_for  # noqa: E402

CHAMP = 6.172
BAND = 0.01  # voidcheck.SCREEN_BAND at time of writing; the test passes it explicitly


class BucketForTest(unittest.TestCase):
    def test_paired_verdict_outranks_val(self):
        # even a great val is "rejected" if the paired confirm disagreed
        self.assertEqual(bucket_for(6.10, (False, -0.002), CHAMP, BAND), "rejected")
        self.assertEqual(bucket_for(6.15, (True, -0.011), CHAMP, BAND), "confirmed")

    def test_no_val_is_failed(self):
        self.assertEqual(bucket_for(None, None, CHAMP, BAND), "failed")

    def test_implausible_screened_not_lead(self):
        # use_conv_ffn 0.4388 vs champ 6.172 = >50% better → broken metric
        self.assertEqual(bucket_for(0.4388, None, CHAMP, BAND), "implausible")

    def test_lead_clears_band(self):
        self.assertEqual(bucket_for(6.1581, None, CHAMP, BAND), "lead")  # +0.0139

    def test_marginal_inside_band(self):
        self.assertEqual(bucket_for(6.1644, None, CHAMP, BAND), "marginal")  # +0.0076

    def test_neutral_when_not_better(self):
        self.assertEqual(bucket_for(6.1728, None, CHAMP, BAND), "neutral")  # -0.0008
        self.assertEqual(bucket_for(6.172, None, CHAMP, BAND), "neutral")   # ==

    def test_no_champion_falls_back_to_neutral(self):
        self.assertEqual(bucket_for(6.15, None, None, BAND), "neutral")


if __name__ == "__main__":
    unittest.main()
