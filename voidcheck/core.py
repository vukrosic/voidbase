"""voidcheck/core.py — the integrity primitives (pure; stdlib only).

Everything here is a function of its arguments with no side effects, so it is
trivially testable and safe to vendor. The values below are the platform's
validated trust policy (operator, 2026-06-17); they live here, once.
"""
from __future__ import annotations

import statistics as _st

# The three seeds a paired confirm runs on BOTH arms. Same seeds on candidate and
# champion-baseline so the only thing that differs is the lever under test
# (matches prior art's validated 42/123/7).
SEEDS = (42, 123, 7)

# Screen band: a candidate must beat the PINNED champion by more than this on the
# cheap single-seed screen before any GPU is spent on a 6-run confirm. THIS is the
# single source of truth for the band — the daemon's --screen-band default, the
# docs, and every report derive from it; never hard-code the number elsewhere.
# Tightened 0.02 -> 0.01 (2026-06-18) so smaller-but-real improvements reach the
# more-sensitive paired 3-seed confirm instead of being rejected as screen noise.
SCREEN_BAND = 0.01

# Confirm band: the paired 3-seed mean must beat the freshly re-run champion by
# more than this epsilon AND favour the candidate at all 3 seeds to AGREE. The
# paired same-batch design + 3/3 sign agreement is the noise floor, not a band.
CONFIRM_BAND = 0.001

# Plausibility floor: the screen rewards a LOWER val_loss, but it has no lower
# bound — so a broken run that prints a nonsense-low loss (a crash mis-parsed as a
# metric, a truncated eval, or a forged report from an untrusted donor box) looks
# like the BIGGEST win and becomes the #1 confirm candidate, burning a 6-run
# paired confirm on garbage. A single structural experiment does not more than
# halve val_loss in this regime; a candidate below this fraction of the champion
# is "too good to be true" and must not auto-consume confirm GPU. Conservative on
# purpose — a genuine win is single-digit-percent, so the real candidates clear it
# with huge margin; only the physically-impossible get flagged.
MAX_DROP_FACTOR = 0.5


def is_paired(seed, box_id, baseline_seed, baseline_box_id) -> bool:
    """Mirror of the comparisons.is_paired GENERATED column: a delta is trustworthy
    signal ONLY when treatment and baseline share the same seed AND the same box,
    with all four values present.

    This is the single most important rule on the platform — going multi-writer on
    heterogeneous GPUs makes seed/box noise worse, so an unpaired delta is noise,
    not a result. Having it here (not only as a DB column) lets any client or
    auditor check pairing without the database. box ids compare by string so a
    uuid object and its text form match."""
    if seed is None or box_id is None or baseline_seed is None or baseline_box_id is None:
        return False
    return seed == baseline_seed and str(box_id) == str(baseline_box_id)


def beats_screen(candidate_val, champion_val, band: float = SCREEN_BAND) -> bool:
    """The cheap screen gate: a candidate clears it iff it beats the champion by
    MORE than `band` (lower val_loss is better). Equality or a smaller margin does
    not clear it — that margin is inside the noise the band exists to reject."""
    if candidate_val is None or champion_val is None:
        return False
    return (champion_val - candidate_val) > band


def is_implausible_win(candidate_val, champion_val,
                       max_drop_factor: float = MAX_DROP_FACTOR) -> bool:
    """True iff a candidate's screen value is "too good to be true" and should NOT
    auto-trigger a paired confirm — a broken/forged metric, not a real result.

    A val_loss at or below zero is definitionally broken (cross-entropy is > 0). A
    value below `max_drop_factor` × the champion is an improvement too large to
    believe from one experiment (default: more than halving the loss). When either
    value is missing, or the champion is itself non-positive, we cannot assess
    plausibility and DON'T flag — the screen's own None-handling and the human stay
    responsible there. This guards the confirm queue against the new attack surface
    opened by untrusted-donor `/runs` reports without touching the win definition."""
    if candidate_val is None or champion_val is None:
        return False
    if candidate_val <= 0:
        return True
    if champion_val <= 0:
        return False
    return candidate_val < champion_val * max_drop_factor


# The fields a third party needs to RE-RUN a result and expect the same number.
# config + seed fix the experiment, git_commit pins the training code, command is
# how it was invoked. env (lib/CUDA stack) is recommended, not required — its
# absence downgrades the verdict to a warning rather than failing it outright,
# because a run from the exact same commit+config is reproducible-in-principle
# even when we didn't capture the stack it happened to run on.
_BUNDLE_REQUIRED = ("config", "seed", "git_commit", "command")


def repro_bundle(run: dict, box: dict | None = None) -> dict:
    """Assemble the reproducibility bundle for a run (pure; no I/O) and judge
    whether it is actually re-runnable.

    A confirmed champion is only trustworthy if someone else can reproduce it, so
    "reproducible" is an integrity property, not metadata — it lives here next to
    is_paired. Given a run row (and optionally its box, for the GPU class) it
    gathers everything needed to re-run — config, seed, command, content_hash, the
    git triple, the runtime env — and returns:

      reproducible : True iff every required field (config, seed, git_commit,
                     command) is present AND the git tree was clean. A dirty tree
                     means uncommitted changes the commit doesn't capture, so the
                     commit alone can't reproduce the run.
      missing      : the required fields that are absent (the blockers).
      warnings     : non-blocking gaps that weaken reproducibility — a dirty tree,
                     or an uncaptured runtime stack (numerics may drift).

    Everything is read defensively so a half-populated legacy row degrades to
    'not reproducible, here's what's missing' instead of raising."""
    run = run or {}
    config = run.get("config")
    git = {
        "commit": run.get("git_commit"),
        "branch": run.get("git_branch"),
        "dirty": run.get("git_dirty"),
    }
    env = run.get("env") or {}
    # GPU class: prefer the box's advertised class, fall back to the gpu the env
    # probe recorded (a donor's run may carry env.gpu but no joined box row).
    gpu_class = (box or {}).get("gpu_class") or env.get("gpu")

    present = {
        "config": bool(config),
        "seed": run.get("seed") is not None,
        "git_commit": bool(git["commit"]),
        "command": bool(run.get("command")),
    }
    missing = [f for f in _BUNDLE_REQUIRED if not present[f]]

    warnings = []
    if git["dirty"]:
        warnings.append(
            "git tree was dirty at run time — uncommitted changes are not captured, "
            "so the commit alone may not reproduce this run")
    if not env:
        warnings.append(
            "no runtime env captured — library/CUDA/GPU stack is unknown, so "
            "numerics may differ when re-run on another stack")

    reproducible = not missing and not git["dirty"]

    return {
        "run_id": run.get("id"),
        "reproducible": reproducible,
        "missing": missing,
        "warnings": warnings,
        "config": config,
        "seed": run.get("seed"),
        "command": run.get("command"),
        "content_hash": run.get("content_hash"),
        "git": git,
        "env": env,
        "gpu_class": gpu_class,
    }


def paired_verdict(jobs: list[dict], confirm_band: float = CONFIRM_BAND,
                   seeds=SEEDS) -> dict:
    """The paired-delta judgement (pure). `jobs` are the collected confirm runs,
    each {arm: 'cand'|'base', seed, val} (val may be None for a failed run).

    Paired delta = candidate mean − champion mean over the MATCHED seeds (negative
    = candidate improves). AGREE iff we have all matched pairs, the mean beats the
    band, AND every seed individually favours the candidate — sign-consistency is
    the noise floor that a lucky single seed can't fake. Returns
    {agrees, delta, cand_mean, n_pairs, notes}."""
    by_key = {(j["arm"], j["seed"]): j for j in jobs}
    pairs = []  # (seed, cand_val, base_val)
    for seed in seeds:
        c = by_key.get(("cand", seed))
        b = by_key.get(("base", seed))
        if c and b and c["val"] is not None and b["val"] is not None:
            pairs.append((seed, c["val"], b["val"]))

    if not pairs:
        return {"agrees": False, "delta": None, "cand_mean": None, "n_pairs": 0,
                "notes": ("confirm produced no paired vals — all runs failed or "
                          "crashed; cannot reproduce, rejecting.")}

    cand_mean = _st.mean(cv for _, cv, _ in pairs)
    base_mean = _st.mean(bv for _, _, bv in pairs)
    delta = cand_mean - base_mean
    all_favor = all(cv < bv for _, cv, bv in pairs)
    complete = len(pairs) == len(seeds)
    agrees = complete and all_favor and (delta < -confirm_band)
    rows = "; ".join(f"s{s}: {cv:.4f} vs {bv:.4f} (Δ{cv - bv:+.4f})"
                     for s, cv, bv in pairs)
    notes = (f"paired {len(pairs)}/{len(seeds)} seeds | cand mean {cand_mean:.4f} "
             f"vs champ {base_mean:.4f} | Δ {delta:+.4f} | "
             f"sign {sum(cv < bv for _, cv, bv in pairs)}/{len(pairs)} favour candidate | "
             f"band {confirm_band} | {rows}")
    return {"agrees": agrees, "delta": delta, "cand_mean": cand_mean,
            "n_pairs": len(pairs), "notes": notes}
