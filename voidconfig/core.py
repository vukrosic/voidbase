"""voidconfig/core.py — the config-row shape + dedup key (pure; stdlib only).

A voidbase experiment is *config-as-data*: a self-contained JSON row (champion
base already merged in) that any box can run with zero local state. Two facts
about that row must be agreed on by everything that writes one — the feeder, the
hand-enqueue tool, the API's POST /queue_items, and Voidmind — or the platform
silently breaks:

  * the **content_hash** (the seed-independent dedup key): if two writers hash the
    same config differently, "has anyone tried this?" stops working and the queue
    fills with re-runs.
  * the **resolved shape** ({config_class, env, fields, seed, dataset_path,
    lever}): if a writer emits the wrong shape, run_experiment.py can't run it.

This library is the single owner of both, so they're pinned in ONE place. Like
voidcheck/voidcredit it is a PURE policy library — no DB, no network, just
json+hashlib — imported by the trusted API edge and re-exported by feeder so
nothing drifts. (Resolves the "config schema ownership" open question in
docs/VOIDMIND.md.)

Must stay I/O-free (enforced by a test).
"""
from __future__ import annotations

import hashlib
import json

DEFAULT_DATASET_PATH = "processed_data/pretrain_1B"


def content_hash(env: dict, fields: dict) -> str:
    """Stable dedup key over the resolved config (seed-independent).

    Hashes ONLY {env, fields} — not seed — because re-running the same config on a
    different seed is a *pairing*, not a new experiment, so both must land in the
    same dedup bucket. Byte-identical to scripts/feeder.content_hash (a test pins
    them equal); changing this rehashes the whole corpus, so don't, casually."""
    blob = json.dumps({"env": env, "fields": fields}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def resolve_config(config_class: str, env: dict | None, fields: dict | None,
                   seed, lever: str,
                   dataset_path: str = DEFAULT_DATASET_PATH) -> dict:
    """Build a self-contained config row in the exact shape run_experiment.py
    consumes (matches feeder.make_experiment / enqueue's `resolved`). The row
    carries the FULL resolved config so a worker box needs zero champion state."""
    return {
        "config_class": config_class,
        "env": dict(env or {}),
        "fields": dict(fields or {}),
        "seed": seed,
        "dataset_path": dataset_path,
        "lever": lever,
    }


def validate_config(config) -> dict:
    """Reject a malformed config row before it reaches the queue. Returns the
    config unchanged when valid; raises ValueError with a precise reason when not.

    The API calls this on POST /queue_items so a donor's idea-loop can never park a
    row the worker will choke on — a junk config is a client error (400), not a
    poisoned queue item discovered an hour later on a GPU."""
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    cc = config.get("config_class")
    if not isinstance(cc, str) or not cc.strip():
        raise ValueError("config.config_class is required (non-empty string)")
    for key in ("env", "fields"):
        if not isinstance(config.get(key, {}), dict):
            raise ValueError(f"config.{key} must be an object")
    seed = config.get("seed")
    if seed is not None and not isinstance(seed, int):
        raise ValueError("config.seed must be an integer or null")
    return config


def _slug(text: str, limit: int = 40) -> str:
    """A filesystem/id-safe slice of a free-text lever label."""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (text or "").strip().lower()]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:limit] or "item"


def queue_item_id(prefix: str, lever: str, chash: str) -> str:
    """A stable, readable queue id: '<prefix>-<lever-slug>-<hash8>'. The hash tail
    keeps it unique per resolved config (mirrors feeder's 'auto-<lever>-<hash8>')."""
    return f"{prefix}-{_slug(lever)}-{chash[:8]}"


def queue_item_name(lever: str, chash: str) -> str:
    """The queue row's display name. Carries a short hash tail so two DIFFERENT
    configs that happen to share a lever label don't collide on the
    (thread_name, name) unique constraint — LLM-proposed levers aren't naturally
    unique the way feeder's flag names are."""
    return f"{lever} [{chash[:6]}]"
