"""Pure-logic test for feeder.winning_singles — no live DB.

Covers the `confirmed_only` seed filter for stack mode: it must pair the search's
compounding experiments only from singles that PASSED a paired confirm, not from
ones that merely screened below the champion once (which can be seed-luck — the
real gmlp_sgu screened in but was paired-REJECTED). A fake cursor returns canned
rows for the two queries winning_singles issues.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.feeder import winning_singles, OPTIMIZER_DENY  # noqa: E402


class OptimizerDenyTest(unittest.TestCase):
    """RULE 0: the structural search must exclude optimizer flags. These 7 leaked
    once because their names didn't contain an existing deny token (e.g. 'muon_'
    has a trailing underscore that 'moonlight_muon' lacks)."""

    LEAKED_OPTIMIZERS = (
        "use_galore", "use_looksam", "use_mars", "use_moonlight_muon",
        "use_soap", "use_swan", "use_tiger",
    )
    # structural flags that must NOT be caught by the (broadened) deny list
    STRUCTURAL_OK = (
        "use_swiglu_ffn", "use_canon_conv", "use_cross_block_score_share",
        "use_qk_layernorm", "use_value_residual", "use_parallel_block",
        "use_attn_logit_bias", "use_sub_ln", "use_nope", "use_mla",
    )

    def _denied(self, name):
        return any(tok in name for tok in OPTIMIZER_DENY)

    def test_leaked_optimizers_now_denied(self):
        for f in self.LEAKED_OPTIMIZERS:
            self.assertTrue(self._denied(f), f"{f} should be denied (it's an optimizer)")

    def test_structural_flags_not_over_denied(self):
        for f in self.STRUCTURAL_OK:
            self.assertFalse(self._denied(f), f"{f} is structural — must NOT be denied")


class _FakeCursor:
    """Answers winning_singles' two queries in order: (1) confirmed names [only
    when confirmed_only], (2) name -> min(val). The query text decides which."""

    def __init__(self, confirmed: list[str], vals: dict[str, float]):
        self._confirmed = [(n,) for n in confirmed]
        self._vals = [(n, v) for n, v in vals.items()]
        self._last = None

    def execute(self, sql, params=None):
        self._last = "confirmed" if "c.agrees=true" in sql else "vals"

    def fetchall(self):
        return self._confirmed if self._last == "confirmed" else self._vals


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


CHAMP = 6.172
# canon_conv + gmlp_sgu both beat the champion on val; only canon_conv is confirmed.
VALS = {"use_canon_conv": 6.1584, "use_gmlp_sgu": 6.1597, "use_swiglu_ffn": 6.1581}
CANDS = list(VALS)


class WinningSinglesSeedFromTest(unittest.TestCase):
    def test_beats_mode_includes_unconfirmed(self):
        conn = _FakeConn(_FakeCursor(confirmed=["use_canon_conv"], vals=VALS))
        out = winning_singles(conn, CHAMP, CANDS, top_k=8, min_margin=0.0,
                              confirmed_only=False)
        # all three beat the champion; best-first by margin
        self.assertEqual(set(out), {"use_canon_conv", "use_gmlp_sgu", "use_swiglu_ffn"})
        self.assertEqual(out[0], "use_swiglu_ffn")  # lowest val => biggest margin

    def test_confirmed_mode_keeps_only_confirmed(self):
        conn = _FakeConn(_FakeCursor(confirmed=["use_canon_conv"], vals=VALS))
        out = winning_singles(conn, CHAMP, CANDS, top_k=8, min_margin=0.0,
                              confirmed_only=True)
        self.assertEqual(out, ["use_canon_conv"])  # gmlp_sgu/swiglu not yet confirmed

    def test_confirmed_mode_pairs_once_two_confirmed(self):
        conn = _FakeConn(_FakeCursor(
            confirmed=["use_canon_conv", "use_swiglu_ffn"], vals=VALS))
        out = winning_singles(conn, CHAMP, CANDS, top_k=8, min_margin=0.0,
                              confirmed_only=True)
        self.assertEqual(set(out), {"use_canon_conv", "use_swiglu_ffn"})


if __name__ == "__main__":
    unittest.main()
