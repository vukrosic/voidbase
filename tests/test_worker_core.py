"""Pure-logic tests for scripts/worker.py — no DB, no SSH, no GPU.

Covers the infra-vs-training failure classifier that decides whether a non-OK run
is RE-QUEUED (a connection drop — retry it) or recorded as a genuine `failed`
experiment (a real training crash). A misclassification either poisons the search
with spurious failures or silently drops real ones.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.worker import is_transient_infra_failure  # noqa: E402


class TransientInfraFailureTest(unittest.TestCase):
    def test_ssh_returncode_255_is_infra(self):
        # ssh's own error code for any connection problem.
        self.assertTrue(is_transient_infra_failure(255, ""))

    def test_remote_close_markers_are_infra(self):
        for out in (
            "Connection to 1.208.108.242 closed by remote host.",
            "client_loop: send disconnect: Broken pipe",
            "kex_exchange_identification: read: Connection reset",
            "Connection timed out",
        ):
            self.assertTrue(is_transient_infra_failure(1, out), out)

    def test_real_training_crash_is_not_infra(self):
        # A Python traceback with a clean exit-ish code is a genuine failure — the
        # experiment ran and broke; it must be recorded, not retried forever.
        out = ("device: CUDA\nTraceback (most recent call last):\n"
               "  RuntimeError: shape '[2, 64]' is invalid for input of size 100")
        self.assertFalse(is_transient_infra_failure(1, out))

    def test_successful_output_is_not_infra(self):
        self.assertFalse(is_transient_infra_failure(0, "Final Val Loss: 6.1581"))

    def test_empty_non255_is_not_infra(self):
        self.assertFalse(is_transient_infra_failure(1, ""))


if __name__ == "__main__":
    unittest.main()
