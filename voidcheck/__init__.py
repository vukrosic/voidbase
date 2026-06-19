"""Voidcheck — the voidbase result-integrity library (pure, no I/O).

The one place the platform's trust rules live as testable functions, so the API,
the confirm daemon, a compute-donor client, and any third-party auditor all judge
a result the SAME way — and so the rules that past bugs slipped through
(lucky-seed-42, the fake-NULL pairing, the confirm control-arm) get property
tests instead of being smeared across SQL generated columns and a daemon.

Three primitives, all pure functions over plain values — no DB, no network:

  * is_paired(...)        — mirror of comparisons.is_paired: a delta is signal
                            ONLY when treatment and baseline share seed AND box.
  * beats_screen(...)     — the cheap single-seed screen gate.
  * is_implausible_win(...) — the "too good to be true" floor that keeps a broken
                            or forged nonsense-low loss off the confirm queue.
  * paired_verdict(...)   — the paired 3-seed sign-consistent AGREE rule that
                            promotes/rejects a candidate.
  * repro_bundle(...)     — assemble a run's reproducibility bundle (config, seed,
                            git, env, gpu) and judge whether it is re-runnable.

This package must stay dependency-free and I/O-free (enforced by a test), so it
can be vendored anywhere a result needs checking.
"""
from voidcheck.core import (  # noqa: F401
    CONFIRM_BAND,
    MAX_DROP_FACTOR,
    SCREEN_BAND,
    SEEDS,
    beats_screen,
    is_implausible_win,
    is_paired,
    paired_verdict,
    repro_bundle,
)

__all__ = [
    "SEEDS", "SCREEN_BAND", "CONFIRM_BAND", "MAX_DROP_FACTOR",
    "is_paired", "beats_screen", "is_implausible_win", "paired_verdict",
    "repro_bundle",
]
