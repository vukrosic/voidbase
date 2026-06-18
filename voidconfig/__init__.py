"""Voidconfig — the config-row shape + dedup-key policy (pure, no I/O).

The single owner of two facts every writer of a queue row must agree on: the
seed-independent `content_hash` (the dedup key) and the resolved config shape
run_experiment.py consumes. Imported by the API's POST /queue_items (authoritative
hash + validation) and Voidmind, and re-exported by scripts/feeder so the
auto-feeder and the API can't drift. Same pure-library discipline as voidcheck and
voidcredit.
"""
from voidconfig.core import (  # noqa: F401
    DEFAULT_DATASET_PATH,
    content_hash,
    queue_item_id,
    queue_item_name,
    resolve_config,
    validate_config,
)

__all__ = [
    "content_hash",
    "resolve_config",
    "validate_config",
    "queue_item_id",
    "queue_item_name",
    "DEFAULT_DATASET_PATH",
]
