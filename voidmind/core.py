"""voidmind/core.py — the Voidmind client core (stdlib + voidconfig; zero DB).

Voidmind is the token-donation half of the platform: it reads open research
threads, asks the *donor's own* LLM for candidate experiments, and enqueues them
as runnable jobs. Voidrunner (compute) then drains what Voidmind (tokens) fills.

The whole protocol is a handful of thin HTTP calls plus one injected seam:

    ctx       = build_context(api, thread)          # goal + what's been tried
    proposals = proposer(ctx)                        # the donor's LLM (or a stub)
    results   = run_once(api, token, thread, base, proposer)   # dedup + POST

`proposer` is any `Callable[[ctx], list[Proposal]]` — the LLM call lives behind
that seam (see voidmind/propose.py), so the core carries no vendor SDK and a test
can drive the loop with a scripted proposer and no network.

Why stdlib + voidconfig only: this runs on a donor's box. It holds no DB creds
(HTTP + bearer token, like Voidrunner) and shares the ONE config-hash owner
(voidconfig) with the feeder and the API, so a proposed row lands in the exact
same dedup space as an auto-fed one — never a silent re-run.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

import voidconfig

DEFAULT_API = "http://127.0.0.1:8787"

# A Proposal is a plain dict the proposer returns; all keys optional except a
# lever label. `fields`/`env` are DELTAS merged onto the base champion config.
#   {"lever": str, "fields": dict, "env": dict, "seed": int?,
#    "config_class": str?, "explanation": str?}
Proposal = dict


# --- HTTP to the voidbase API ------------------------------------------------

class ApiError(RuntimeError):
    """A non-2xx response from the voidbase API. Carries status + payload."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload
        super().__init__(f"voidbase API {status}: {payload.get('error', payload)}")


def _request(api: str, path: str, *, method: str, body: dict | None = None,
             token: str | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            payload = {"error": e.reason}
        raise ApiError(e.code, payload) from None


def _get(api: str, path: str, params: dict | None = None, timeout: int = 30) -> dict:
    if params:
        path = f"{path}?{urlencode(params)}"
    return _request(api, path, method="GET", timeout=timeout)


def _post(api: str, path: str, body: dict, token: str | None = None,
          timeout: int = 30) -> dict:
    return _request(api, path, method="POST", body=body, token=token, timeout=timeout)


# --- the protocol: register / read / propose / enqueue ----------------------

def register(api: str, handle: str) -> dict:
    """Mint a contributor + bearer token (returned ONCE). A Voidmind token grants
    ideas/queue_items write — the same /register Voidrunner uses."""
    return _post(api, "/register", {"handle": handle})


def open_threads(api: str, status: str = "active", unclaimed: bool = False) -> list:
    """Open research threads to propose against (GET /threads/public)."""
    out = _get(api, "/threads/public",
               {"status": status, "unclaimed": "true" if unclaimed else "false"})
    return out if isinstance(out, list) else []


def thread_goal(api: str, name: str) -> dict:
    """The full goal prompt for one thread (GET /threads/goal)."""
    return _get(api, "/threads/goal", {"name": name})


def recent_runs(api: str, thread: str | None = None, limit: int = 50) -> list:
    """Recent runs, newest first, optionally scoped to one thread — the 'what's
    been tried' history the proposer sees so it doesn't re-propose a dead end."""
    out = _get(api, "/runs")
    runs = out if isinstance(out, list) else []
    if thread:
        runs = [r for r in runs if r.get("thread_name") == thread]
    return runs[:limit]


def gate_status(api: str, scope: str, timeout: int = 30) -> dict:
    """The confirm-gate snapshot for a scope (GET /gate): the champion, the
    band-clearing candidates, and the recent confirmed/rejected verdicts. This is
    the AUTHORITATIVE fitness signal (the server judges the band via voidcheck), so
    a proposer that builds on it can't disagree with what the gate will actually
    promote. Best-effort: an older API or a non-dict reply yields {} and the
    proposer simply sees no gate signal (the loop still runs)."""
    try:
        out = _get(api, "/gate", {"scope": scope}, timeout=timeout)
    except ApiError:
        return {}
    return out if isinstance(out, dict) else {}


def champion_lineage(api: str, scope: str, timeout: int = 30) -> list:
    """The champion promotion arc for a scope (GET /champions, filtered + oldest-
    first): each confirmed promotion's val_loss and the mechanism `reason`. This is
    the COMPOUNDING story — what stacked to get here — which a proposer should
    extend, not the flat list of every lever ever tried. Best-effort → []."""
    try:
        out = _get(api, "/champions", timeout=timeout)
    except ApiError:
        return []
    rows = out if isinstance(out, list) else []
    arc = [r for r in rows if r.get("scope") == scope]
    arc.sort(key=lambda r: r.get("promoted_at") or "")
    return arc


def rank_contenders(runs: list, champion_val: float | None, *, top: int = 8) -> list:
    """The fitness landscape, distilled: the best non-confirm runs by val_loss, each
    tagged with its signed margin vs the champion (positive = beat the champion) and
    its verification. Pure — no IO. This is what turns 'levers tried' into 'levers
    that worked, ranked', the single biggest signal a proposer needs to combine the
    near-misses into the next winner instead of guessing blind.

    Skips failed runs (no signal), runs with no val_loss, and the `confirm-*` paired
    machinery rows (those are re-runs of an existing candidate, not new levers)."""
    scored = []
    for r in runs:
        v = r.get("final_val_loss")
        name = r.get("name")
        if v is None or not name or r.get("status") == "failed":
            continue
        if name.startswith("confirm-"):
            continue
        scored.append({
            "name": name,
            "val_loss": v,
            "margin": round(champion_val - v, 4) if champion_val is not None else None,
            "verification": r.get("verification"),
        })
    scored.sort(key=lambda x: x["val_loss"])
    return scored[:top]


def build_context(api: str, thread: str, *, history_limit: int = 50) -> dict:
    """Gather everything a proposer needs to suggest the next experiments for one
    thread. Beyond the goal + raw history, this assembles the OUTCOME SIGNAL — the
    fitness landscape — so the proposer extends what's working rather than proposing
    blind:

      * champion   — the current best (run_id + val_loss) every delta is measured against
      * lineage    — the confirmed promotion arc + mechanism reasons (the compounding story)
      * frontier   — gate-cleared candidates: real >band improvements to build on
      * contenders — the best runs ranked by val_loss, with signed margins (near-misses
                     worth combining)
      * verdicts   — recent confirmed/rejected paired outcomes (what's proven / dead)

    Pure read — no writes. Every outcome field is best-effort: a thread/API without
    the gate degrades to the original goal+history context."""
    goal = {}
    try:
        goal = thread_goal(api, thread)
    except ApiError:
        pass  # a thread may have no goal_prompt; the proposer can still work
    runs = recent_runs(api, thread, limit=history_limit)
    # Levers a proposer could actually re-propose: drop the `confirm-*` paired-seed
    # machinery rows (re-runs of an existing candidate, not levers), which would
    # otherwise bloat the prompt with a dozen near-identical names.
    tried = sorted({r["name"] for r in runs
                    if r.get("name") and not r["name"].startswith("confirm-")})
    # The thread name IS the scope in this registry (runs.thread_name == scope).
    gate = gate_status(api, thread)
    champion = gate.get("champion") if isinstance(gate, dict) else None
    champ_val = champion.get("val_loss") if isinstance(champion, dict) else None
    return {
        "thread": thread,
        "goal_prompt": goal.get("goal_prompt"),
        "recent_runs": runs,
        "tried_levers": tried,
        # --- outcome signal (the fitness landscape) ---
        "champion": champion,
        "lineage": champion_lineage(api, thread),
        "frontier": gate.get("clears") or [],
        "contenders": rank_contenders(runs, champ_val),
        "verdicts": gate.get("recent_verdicts") or [],
    }


def post_idea(api: str, token: str | None, title: str,
              explanation: str | None = None, notes: str | None = None) -> dict:
    """Record a candidate idea in the backlog (POST /ideas). A note, not runnable —
    the runnable artifact is the queue_item from enqueue()."""
    body = {"title": title}
    if explanation:
        body["explanation"] = explanation
    if notes:
        body["notes"] = notes
    return _post(api, "/ideas", body, token=token)


def enqueue(api: str, token: str | None, thread: str, config: dict,
            priority: int = 0, gpu_class: str = "any") -> dict:
    """Enqueue a resolved config as a runnable job (POST /queue_items). The SERVER
    computes the authoritative content_hash and dedups, so a re-proposal returns
    {deduped: true} instead of a second copy. Born needs-run + unverified."""
    body = {"thread": thread, "config": config, "priority": priority,
            "gpu_class": gpu_class}
    return _post(api, "/queue_items", body, token=token)


# --- resolve a proposal against the champion base ---------------------------

def resolve_proposal(proposal: Proposal, base: dict | None) -> dict:
    """Merge a proposal's field/env DELTAS onto the champion base into a full,
    self-contained config row (via voidconfig, the shared shape+hash owner).

    `base` is the current champion config the donor starts from
    ({config_class, env, fields, seed}); a proposal overrides any of it. The
    config_class must come from the proposal or the base — there's no default
    model to silently fall back to."""
    base = base or {}
    env = {**(base.get("env") or {}), **(proposal.get("env") or {})}
    fields = {**(base.get("fields") or {}), **(proposal.get("fields") or {})}
    seed = proposal.get("seed", base.get("seed", 42))
    config_class = proposal.get("config_class") or base.get("config_class")
    if not config_class:
        raise ValueError(
            "no config_class: set it on the base champion config or the proposal")
    lever = (proposal.get("lever") or "idea").strip() or "idea"
    dataset = base.get("dataset_path", voidconfig.DEFAULT_DATASET_PATH)
    return voidconfig.resolve_config(config_class, env, fields, seed, lever, dataset)


def run_once(api: str, token: str | None, thread: str, base: dict | None,
             proposer, *, limit: int = 5, priority: int = 0,
             post_ideas: bool = True, dry: bool = False) -> list[dict]:
    """One pass of the idea loop for a thread: gather context → ask the proposer →
    resolve, locally dedup, and (unless dry) POST each as an idea + queue_item.

    Returns one result dict per proposal: the enqueue response (carrying the
    server's deduped flag), or {dry: true, ...} when dry. Local dedup only skips
    repeats WITHIN this pass; cross-session dedup is the server's authoritative
    job (it owns the hash), so the loop never has to read every prior config."""
    context = build_context(api, thread)
    context["base"] = base
    proposals = proposer(context) or []

    results: list[dict] = []
    seen_local: set[str] = set()
    for proposal in proposals:
        if len(results) >= limit:
            break
        try:
            config = resolve_proposal(proposal, base)
        except ValueError as e:
            results.append({"error": str(e), "proposal": proposal.get("lever")})
            continue
        chash = voidconfig.content_hash(config["env"], config["fields"])
        if chash in seen_local:
            continue
        seen_local.add(chash)
        if dry:
            results.append({"dry": True, "content_hash": chash,
                            "lever": config["lever"]})
            continue
        if post_ideas:
            try:
                post_idea(api, token, title=config["lever"],
                          explanation=proposal.get("explanation"),
                          notes=f"voidmind: thread={thread}")
            except ApiError:
                pass  # a duplicate idea id is harmless; the queue_item is the point
        # One bad enqueue (e.g. a transient API error) must not abort the batch —
        # an unattended donor loop captures it and keeps proposing the rest.
        try:
            enq = enqueue(api, token, thread, config, priority=priority)
        except ApiError as e:
            results.append({"error": str(e), "lever": config["lever"]})
            continue
        enq["lever"] = config["lever"]
        results.append(enq)
    return results
