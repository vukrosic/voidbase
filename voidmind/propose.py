"""voidmind/propose.py — the proposer seam (where donor tokens are spent).

A *proposer* is any `Callable[[context], list[Proposal]]`. The core loop
(voidmind/core.run_once) gathers the context (goal + history + base config) and
hands it to a proposer to get candidate experiments. Two ship here:

  * `llm_proposer(...)` — calls the donor's LLM (Anthropic Messages API by
    default; any OpenAI-incompatible base_url with the same shape works) on the
    donor's own key, and parses a JSON array of proposals. This is the real
    token-donation path.
  * `static_proposer(list)` — returns a fixed list. For tests and for scripting a
    known set of experiments without spending any tokens.

A donor who wants a different model/vendor just writes their own callable — that's
the whole point of the seam. The core never imports a vendor SDK.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# The brief the LLM answers. It is told the goal, the current champion config, the
# fitness landscape (what won, what came close, what was rejected, and the compounding
# arc that produced the champion), and must return STRICT JSON — a list of field/env
# deltas, each a structural mechanism (RULE 0: novel architecture, never an
# optimizer/LR/batch sweep). The core resolves these onto the base and dedups.
_SYSTEM = (
    "You are a research proposer for an automated LLM-architecture search. You are "
    "given a goal, the current champion config, and the FITNESS LANDSCAPE so far: "
    "the confirmed promotion arc (which mechanisms compounded to reach the champion), "
    "the current frontier (candidates already beating the champion), the ranked "
    "contenders with their margins (near-misses worth combining), and recently "
    "rejected levers. "
    "Propose NEW structural mechanism experiments — changes to model architecture "
    "(attention, residual, normalization, mixing, gating), NOT optimizer / "
    "learning-rate / weight-decay / batch-size / schedule sweeps. "
    "Reason from the landscape: EXTEND the confirmed arc, COMBINE the strongest "
    "contenders into a stacked mechanism, or open a genuinely new structural "
    "direction when the near-misses have plateaued. Do NOT re-propose a rejected or "
    "already-tried lever, and do not propose a trivially-additive flag that the "
    "margins suggest is noise. Prefer mechanisms that could plausibly clear the "
    "screen band, not 0.001-level tweaks. "
    "Reply with ONLY a JSON array, no prose. Each element: "
    '{"lever": "<short-kebab-label>", "fields": {<config field overrides>}, '
    '"env": {<optional env overrides>}, '
    '"explanation": "<one sentence: which landscape signal motivates this>"}. '
    "Keep field names plausible for the champion config shown."
)


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of an LLM reply, tolerating stray prose or a
    ```json fence. Returns [] if nothing parses (a bad reply wastes a pass, never
    crashes the loop)."""
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        out = json.loads(text[start:end + 1])
        return out if isinstance(out, list) else []
    except json.JSONDecodeError:
        return []


def _fmt_margin(m) -> str:
    """A signed margin vs champion (positive = better). '?' when unknown."""
    if m is None:
        return "?"
    return f"{'+' if m >= 0 else ''}{m:.4f}"


def _build_prompt(context: dict, n: int) -> str:
    base = context.get("base") or {}
    tried = context.get("tried_levers") or []
    parts = [
        f"Goal:\n{context.get('goal_prompt') or '(no goal prompt provided)'}",
        f"\nCurrent champion config (propose deltas on this):\n"
        f"{json.dumps({'config_class': base.get('config_class'), 'fields': base.get('fields'), 'env': base.get('env'), 'seed': base.get('seed')}, indent=2)}",
    ]

    # --- the fitness landscape: what the search has actually learned ---
    champ = context.get("champion")
    if isinstance(champ, dict) and champ.get("val_loss") is not None:
        parts.append(f"\nChampion val_loss: {champ['val_loss']:.4f} "
                     "(every margin below is champion - candidate; positive = better).")

    lineage = context.get("lineage") or []
    if lineage:
        arc = []
        for c in lineage:
            reason = (c.get("reason") or "").split(". ")[0].strip()
            vl = c.get("val_loss")
            arc.append(f"  {vl:.4f}  {reason}" if isinstance(vl, (int, float))
                       else f"  {reason}")
        parts.append("\nConfirmed promotion arc (the mechanisms that COMPOUNDED to "
                     "reach the champion — extend this):\n" + "\n".join(arc))

    frontier = context.get("frontier") or []
    if frontier:
        fr = [f"  {c.get('name')}  ({_fmt_margin(c.get('margin'))})" for c in frontier]
        parts.append("\nFrontier — already BEATING the champion past the screen band "
                     "(build on these):\n" + "\n".join(fr))

    contenders = context.get("contenders") or []
    if contenders:
        cs = [f"  {c.get('name')}  val {c.get('val_loss'):.4f}  ({_fmt_margin(c.get('margin'))})"
              for c in contenders]
        parts.append("\nRanked contenders (best runs so far — the near-misses worth "
                     "COMBINING into a stacked mechanism):\n" + "\n".join(cs))

    verdicts = context.get("verdicts") or []
    rejected = [v.get("name") or v.get("run_id") for v in verdicts if not v.get("agrees")]
    if rejected:
        parts.append("\nRecently REJECTED (paired confirm disagreed — do not "
                     f"re-propose): {', '.join(str(r) for r in rejected)}")

    if tried:
        parts.append(f"\nAll levers already tried (do not repeat): {', '.join(tried)}")

    parts.append(f"\nPropose {n} new structural experiments as a JSON array. "
                 "Justify each from a specific signal above.")
    return "\n".join(parts)


def _call_anthropic(prompt: str, *, key: str, model: str, base_url: str,
                    max_tokens: int, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read() or b"{}")
    # Anthropic Messages: {"content": [{"type": "text", "text": "..."}], ...}
    chunks = [c.get("text", "") for c in payload.get("content", [])
              if c.get("type") == "text"]
    return "".join(chunks)


def llm_proposer(*, key: str, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL, n: int = 5,
                 max_tokens: int = 2048, timeout: int = 120):
    """Build a proposer backed by the donor's LLM. Spends the donor's tokens (their
    `key`), never voidbase's. Returns a callable the core loop drives.

    The call is best-effort: a network/API error or an unparseable reply yields an
    empty proposal list (the pass simply enqueues nothing) rather than crashing the
    loop — a token-donor running unattended must degrade, not die."""
    if not key:
        raise ValueError("llm_proposer requires an API key (the donor's own)")

    def propose(context: dict) -> list:
        prompt = _build_prompt(context, n)
        try:
            text = _call_anthropic(prompt, key=key, model=model, base_url=base_url,
                                   max_tokens=max_tokens, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return []
        return _extract_json_array(text)

    return propose


def static_proposer(proposals: list):
    """A proposer that returns a fixed list regardless of context — for tests and
    for scripting a known experiment set with zero token spend."""
    def propose(_context: dict) -> list:
        return list(proposals)
    return propose
