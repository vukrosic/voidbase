"""Voidcredit — attribution & leaderboard policy (pure, no I/O).

Turns the rows the platform already has — runs (with contributor_id/box_id),
confirmations, champions — into credit and lineage: a ranked leaderboard, a
per-contributor card, and the thread→queue_item→run→champion chain for a run.

Like voidcheck, this is a PURE policy library: the *rules* of credit (how to
rank, what counts, how lineage is walked) live here with property tests, and the
API edge does the SQL and calls these. Credit is derived on read, never stored,
so it can't drift — the source of truth stays runs/confirmations/champions.

Must stay dependency-free and I/O-free (enforced by a test).
"""
from voidcredit.core import (  # noqa: F401
    contributor_card,
    rank_contributors,
    run_lineage,
)

__all__ = ["rank_contributors", "contributor_card", "run_lineage"]
