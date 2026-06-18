"""voidcredit/core.py — the attribution policy (pure; stdlib only).

Every function takes plain rows (dicts/lists, as the API gets them from SQL) and
returns plain data — no DB, no network. That keeps the credit *policy* testable
and swappable without touching the integrity core or any write path.
"""
from __future__ import annotations


def _created_key(r: dict):
    """Stable sort key for 'most recent first' that tolerates datetime OR string
    created_at (the API passes datetimes; tests pass ISO strings) and None."""
    return str(r.get("created_at") or "")


def rank_contributors(stats: list[dict]) -> list[dict]:
    """Rank contributors by the v0 credit policy and attach a 1-based `rank`.

    `stats` is one aggregate row per contributor (the API computes these in SQL):
    {handle, role, runs_total, runs_confirmed, champion_runs, compute_seconds,
    tokens_donated}. Impact ranks first — holding a champion, then confirmed runs,
    then volume, then compute — because the platform's point is *confirmed* wins,
    not raw run count. Ties break on handle so the order is deterministic.

    v0 keeps the credit dimensions as separate columns rather than collapsing them
    to one score — there's no reason to weight compute vs. tokens vs. impact into a
    single number yet, and a fake single score would hide what actually happened."""
    def policy(s: dict):
        return (
            -(s.get("champion_runs") or 0),
            -(s.get("runs_confirmed") or 0),
            -(s.get("runs_total") or 0),
            -(s.get("compute_seconds") or 0),
            -(s.get("tokens_donated") or 0),
            (s.get("handle") or ""),
        )
    return [{**s, "rank": i} for i, s in enumerate(sorted(stats, key=policy), 1)]


def contributor_card(handle: str, runs: list[dict],
                     champion_run_ids=None, recent: int = 10) -> dict:
    """One contributor's story, derived from their run rows.

    `runs` are this contributor's runs (each {id, verification, final_val_loss,
    created_at, ...}); `champion_run_ids` is the set of run ids currently held as a
    champion. Returns totals, their best (lowest val_loss) run, how many of their
    runs are the current champion, and their most-recent runs. All derived — no
    stored credit to fall out of sync."""
    champion_run_ids = set(champion_run_ids or [])
    confirmed = [r for r in runs if r.get("verification") == "confirmed"]
    scored = [r for r in runs if r.get("final_val_loss") is not None]
    best = min(scored, key=lambda r: r["final_val_loss"], default=None)
    champ_runs = [r for r in runs if r.get("id") in champion_run_ids]
    return {
        "handle": handle,
        "runs_total": len(runs),
        "runs_confirmed": len(confirmed),
        "champion_runs": len(champ_runs),
        "best_run": ({"id": best["id"], "final_val_loss": best["final_val_loss"]}
                     if best else None),
        "recent_runs": sorted(runs, key=_created_key, reverse=True)[:recent],
    }


def run_lineage(run: dict, queue_item: dict | None = None,
                thread: dict | None = None, champions=None) -> dict:
    """The provenance chain for one run, walked from the rows the API gathered:
    thread → queue_item → run → champion.

    (The idea→queue_item edge isn't in the schema yet — `queue_items` has no
    idea_id — so the chain starts at the thread for now; when an idea_id lands it
    extends one link upward without changing this shape.)

    `champions` are the champion rows whose run_id == run.id (may be empty). A run
    is the CURRENT champion iff one such row has no superseded_at."""
    champions = champions or []
    held = [c for c in champions if c.get("run_id") == run.get("id")]
    is_current = any(c.get("superseded_at") is None for c in held)

    chain: list[dict] = []
    if thread:
        chain.append({"kind": "thread", "name": thread.get("name"),
                      "hypothesis": thread.get("hypothesis")})
    if queue_item:
        chain.append({"kind": "queue_item", "id": queue_item.get("id"),
                      "name": queue_item.get("name")})
    chain.append({"kind": "run", "id": run.get("id"),
                  "verification": run.get("verification"),
                  "final_val_loss": run.get("final_val_loss"),
                  "contributor_id": run.get("contributor_id")})
    for c in held:
        chain.append({"kind": "champion", "scope": c.get("scope"),
                      "promoted_at": c.get("promoted_at"),
                      "current": c.get("superseded_at") is None})
    return {"run_id": run.get("id"), "chain": chain,
            "was_champion": bool(held), "is_current_champion": is_current}
