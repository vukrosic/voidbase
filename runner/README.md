# Voidrunner

The **compute-donor client** for voidbase. Install it on the machine with the
GPU; it claims a research job, runs it locally, and reports the result back —
speaking **only the voidbase HTTP API**. It never sees the database.

> Design + rationale: [../docs/VOIDRUNNER.md](../docs/VOIDRUNNER.md).

## What it does (and won't do)

- ✅ Claims one job at a time from the shared queue (atomic — two runners never
  get the same job), runs it on your GPU, reports `final_val_loss` etc.
- ✅ Heartbeats while a job runs, so a crash auto-requeues the job (you don't
  strand it).
- 🚫 **Never runs arbitrary commands.** A job is, by construction, the pinned
  `run_experiment.py` plus a self-contained config. Anything else is refused
  unrun. So "donate compute" can't mean "let voidbase run code on my box."
- 🚫 **Holds no DB credentials.** All it can do is the donor protocol over HTTPS.
- Your results land **unverified** — useful as leads, but they can never move the
  champion. That only happens through the maintainer confirm gate.

## Quick start

You need: a GPU box, Python with your research stack (torch etc.), and a clone of
the research repo that contains `run_experiment.py`.

```bash
# 1. get a token (once). The token is printed ONCE — save it.
export VOIDRUNNER_API=https://<voidbase-host>      # the operator gives you this
export VOIDRUNNER_TOKEN=$(python3 -m runner register --handle YOUR_NAME)

# 2. point it at the research repo on this box
export VOIDRUNNER_REPO=/path/to/llm-research-kit-scaling

# 3. dry-run first — claims a job, validates the config, runs nothing real
python3 -m runner once --dry

# 4. for real: claim + train + report one job
python3 -m runner once

# 5. or drain the queue forever
python3 -m runner loop
```

On localhost against the operator's own API you can omit `VOIDRUNNER_TOKEN` (the
API trusts loopback). Over the network a token is required.

## Configuration

All flags fall back to environment variables:

| Env | Flag | Meaning | Default |
|---|---|---|---|
| `VOIDRUNNER_API` | `--api` | voidbase API base URL | `http://127.0.0.1:8787` |
| `VOIDRUNNER_TOKEN` | `--token` | bearer token from `register` | (loopback bypass) |
| `VOIDRUNNER_REPO` | `--repo` | research repo with `run_experiment.py` | — (required) |
| `VOIDRUNNER_THREAD` | `--thread` | only claim jobs from this thread | any |
| `VOIDRUNNER_PYTHON` | — | python to run the experiment | `python` |
| `VOIDRUNNER_GPU_CLASS` | — | advertise/override this box's GPU class | auto (nvidia-smi) |
| `VOIDRUNNER_JOB_TIMEOUT` | — | seconds before a run is killed | `3600` |
| `VOIDRUNNER_LOG_DIR` | — | where full run stdout is saved | `./voidrunner-logs` |

## Using it from an agent / your own code

The CLI is a thin driver over three functions in `runner/core.py`. An autonomous
agent can call them directly for finer control:

```python
from runner import core

box   = core.local_box()                                   # describe this machine
res   = core.claim(API, TOKEN, box, thread="tiny1m3m")     # POST /claim
if res["job"]:
    out = core.execute(res["job"], REPO)                   # run locally on the GPU
    core.report(API, TOKEN, out, res["box_id"])            # POST /runs
```

`execute()` refuses any non-`run_experiment.py` command (raises `RefusedCommand`),
so the safety boundary holds whether a human or an agent is driving.

## Tests

```bash
python3 -m pytest tests/test_runner_core.py        # server-free: guard + no-DB-imports
python3 -m pytest tests/test_voidrunner_api.py     # needs a running API (VOIDRUNNER_TEST_API)
```

## Packaging

For now it runs as `python -m runner`. A `pipx install voidrunner` entry point
lands when the package is extracted to its own repo (once the API endpoints stop
changing — see the design doc).
