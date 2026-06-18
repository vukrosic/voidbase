# Voidmind — donate AI tokens

Voidmind is the **token-donation** client for voidbase. It reads open research
threads, asks **your own LLM** for candidate experiments, and enqueues them as
runnable jobs. Voidmind fills the queue with ideas; **Voidrunner** drains it with
compute.

```
Voidmind (your tokens) ──ideas + queue_items──▶ voidbase ──jobs──▶ Voidrunner (GPUs)
```

It is a **write-client spoke**: it speaks only the voidbase HTTP API with a bearer
token and never touches the database. Its writes are low-trust *proposals* — an
idea or queued job can never move the champion (that still goes through the
maintainer confirm gate), so running Voidmind unattended is safe.

## Install / run

Pure stdlib + the in-repo `voidconfig` library — nothing to `pip install`.

```bash
# 1. mint a token (save it — shown once)
python -m voidmind register --handle your-handle

export VOIDMIND_TOKEN=...            # the token from step 1
export VOIDMIND_LLM_KEY=...          # YOUR Anthropic key — spends YOUR tokens
export VOIDMIND_BASE=/path/to/llm-research-kit-scaling/autoresearch/champion.json

# 2. one pass (dry-run first to see what it would enqueue)
python -m voidmind once --dry
python -m voidmind once

# 3. keep proposing
python -m voidmind loop
```

If you omit `--thread`, Voidmind proposes against the highest-priority open thread
the API reports (`GET /threads/public`).

## How it works

1. `build_context` reads the thread goal (`/threads/goal`) and recent runs
   (`/runs`) → "what's the goal, what's already been tried".
2. A **proposer** turns that context into candidate experiments. The default
   (`voidmind.propose.llm_proposer`) calls your LLM on your key and parses a JSON
   array of `{lever, fields, env, explanation}` deltas.
3. Each proposal is merged onto your champion **base** into a self-contained config
   via **`voidconfig`** — the same library the feeder and the API use, so a
   proposed row lands in the *exact same dedup space* as an auto-fed one.
4. `POST /ideas` records the idea; `POST /queue_items` enqueues the job. The
   **server** computes the authoritative `content_hash` and dedups — a re-proposal
   returns `{deduped: true}` instead of a second copy.

## Swapping the proposer (the seam)

A proposer is any `Callable[[context], list[Proposal]]`. To use a different model
or vendor, write your own and pass it to `voidmind.run_once` — the core imports no
vendor SDK:

```python
from voidmind import run_once

def my_proposer(ctx):
    # ctx = {thread, goal_prompt, recent_runs, tried_levers, base}
    return [{"lever": "rope-xpos", "fields": {"use_xpos": True},
             "explanation": "xpos for length extrapolation"}]

run_once(api, token, thread="tiny1m3m", base=base, proposer=my_proposer)
```

`voidmind.propose.static_proposer([...])` returns a fixed list — handy for
scripting a known experiment set with zero token spend.

## Safety

- **Your keys stay yours.** The LLM call uses `VOIDMIND_LLM_KEY`; voidbase never
  sees it.
- **No DB creds.** HTTP + bearer token only (enforced: the package imports no DB
  layer).
- **Low-trust by construction.** Proposals are born unclaimed/unverified; the worst
  a bad idea-loop can do is enqueue junk that never wins a paired comparison.

See [`docs/VOIDMIND.md`](../docs/VOIDMIND.md) for the design and
[`docs/SPOKES.md`](../docs/SPOKES.md) for how it fits the platform.
