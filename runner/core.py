"""runner/core.py — the Voidrunner client core (stdlib only, zero DB imports).

Three discrete calls are the whole protocol:

    box   = local_box()                         # who/what this machine is
    claim = claim(api, token, box, ...)         # POST /claim  -> a job + box_id
    result = execute(claim["job"], repo)        # run it locally on the GPU
    report(api, token, result, box_id)          # POST /runs   -> a run row

plus register() (one-time token mint), heartbeat() (liveness while running), and
release() (hand a claimed job back). Each is a thin function over the voidbase
HTTP API — no global state — so a CLI loop and an AI agent can both call them.

Why stdlib-only: this runs on a donor's box. The fewer dependencies and the less
it can reach (no DB driver, no creds), the safer it is to `pip install` and run.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_API = "http://127.0.0.1:8787"


# --- HTTP to the voidbase API ------------------------------------------------

class ApiError(RuntimeError):
    """A non-2xx response from the voidbase API. Carries the status + payload."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload
        super().__init__(f"voidbase API {status}: {payload.get('error', payload)}")


def _post(api: str, path: str, body: dict, token: str | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            payload = {"error": e.reason}
        raise ApiError(e.code, payload) from None


# --- identity: what this machine is -----------------------------------------

def _detect_gpu() -> tuple[str | None, str | None]:
    """(gpu_name, gpu_uuid) from nvidia-smi, or (None, None) if no NVIDIA GPU.
    Best-effort — a missing nvidia-smi (CPU box, Mac, Apple Silicon) is not an
    error; the box simply advertises no gpu_class."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None, None
        first = out.stdout.strip().splitlines()[0]
        name, _, gid = first.partition(",")
        return name.strip() or None, gid.strip() or None
    except Exception:  # noqa: BLE001
        return None, None


def repo_git(repo: str | Path) -> dict:
    """The git triple of the research repo this run trained from: {git_commit,
    git_branch, git_dirty}. This pins the TRAINING code (the repo holding
    run_experiment.py), not the runner — that commit is what a champion's
    reproducibility bundle needs. Best-effort: a non-git dir or missing git yields
    all-null, never an error (the bundle just reads back 'commit unknown')."""
    def _git(*args) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None if out.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None
    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # dirty only when git itself answered (porcelain returns '' for a clean tree);
    # leave it null when we couldn't read git at all, so 'unknown' != 'clean'.
    porcelain = _git("status", "--porcelain")
    status_ok = _git("rev-parse", "--is-inside-work-tree") == "true"
    dirty = bool(porcelain) if status_ok else None
    return {"git_commit": commit, "git_branch": branch, "git_dirty": dirty}


def probe_env(python: str = "python") -> dict:
    """The runtime fingerprint the job trained on: {python, platform, torch, cuda,
    gpu}. Probed with the SAME interpreter that runs the training (`python`), so it
    reflects the training venv's torch/CUDA, not the runner's. Best-effort: any
    failure returns {} and the bundle records 'stack unknown'."""
    probe = (
        "import json,platform\n"
        "d={'python':platform.python_version(),'platform':platform.platform()}\n"
        "try:\n"
        "    import torch\n"
        "    d['torch']=torch.__version__\n"
        "    d['cuda']=getattr(torch.version,'cuda',None)\n"
        "    if torch.cuda.is_available(): d['gpu']=torch.cuda.get_device_name(0)\n"
        "except Exception: pass\n"
        "print(json.dumps(d))\n")
    try:
        out = subprocess.run([python, "-c", probe],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        pass
    return {}


def local_box(label: str | None = None, gpu_class: str | None = None) -> dict:
    """Describe THIS machine as a voidbase box: {label, gpu_class, fingerprint}.

    fingerprint is stable per machine+GPU (so re-runs reuse one box row, and two
    GPUs on one host stay distinct): the GPU uuid when present, else the hostname.
    gpu_class defaults to the detected GPU name (what /claim filters on)."""
    host = socket.gethostname()
    gpu_name, gpu_uuid = _detect_gpu()
    fingerprint = gpu_uuid or host
    return {
        "label": label or f"{host}",
        "gpu_class": gpu_class or gpu_name,
        "fingerprint": fingerprint,
    }


# --- the protocol: register / claim / report / release / heartbeat ----------

def register(api: str, handle: str) -> dict:
    """Mint a contributor + bearer token. The token is returned ONCE — save it.
    Returns {contributor_id, handle, token}."""
    return _post(api, "/register", {"handle": handle})


def claim(api: str, token: str | None, box: dict,
          thread: str | None = None, gpu_class_filter: str | None = None) -> dict:
    """Atomically claim the next runnable job for this box. Returns
    {"box_id": ..., "job": {...} | None}. `thread` scopes to one research thread;
    `gpu_class_filter` restricts to jobs this GPU can run."""
    body: dict = {"box": box}
    if thread:
        body["thread"] = thread
    if gpu_class_filter:
        body["gpu_class_filter"] = gpu_class_filter
    return _post(api, "/claim", body, token=token)


def release(api: str, token: str | None, queue_item_id: str) -> dict:
    """Return a claimed job to the queue (needs-run). For a --dry validation that
    must leave no run row, or a graceful stop."""
    return _post(api, "/release", {"queue_item_id": queue_item_id}, token=token)


def heartbeat(api: str, box_id: str) -> None:
    """Best-effort liveness ping — never raises. The reaper requeues a job whose
    box stops beating, so a crashed runner self-heals."""
    try:
        _post(api, "/box_heartbeat", {"box_id": box_id}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


class _Heartbeat:
    """Pings the box alive every `interval`s for the life of one run. Start before
    the job, stop in a finally. First beat fires immediately."""

    def __init__(self, api: str, box_id: str, interval: int = 30):
        self.api, self.box_id, self.interval = api, box_id, interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def __enter__(self):
        heartbeat(self.api, self.box_id)
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            heartbeat(self.api, self.box_id)

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2)


def report(api: str, token: str | None, result: dict, box_id: str,
           eval_points: list[dict] | None = None) -> dict:
    """Report a finished run to voidbase (born verification='unverified'). `result`
    is what execute() returns. Returns {run_id, verification, seed, ...}."""
    body = {
        "queue_item_id": result["queue_item_id"],
        "box_id": box_id,
        "status": "done" if result.get("ok") else "failed",
        "command": result.get("command"),
        "config": result.get("config"),
        "content_hash": result.get("content_hash"),
        "seed": result.get("seed"),
        "final_val_loss": result.get("final_val_loss"),
        "final_train_loss": result.get("final_train_loss"),
        "final_val_accuracy": result.get("final_val_accuracy"),
        # Reproducibility bundle (best-effort; null on older results / non-git repos).
        "git_commit": result.get("git_commit"),
        "git_branch": result.get("git_branch"),
        "git_dirty": result.get("git_dirty"),
        "env": result.get("env"),
    }
    if eval_points:
        body["eval_points"] = eval_points
    return _post(api, "/runs", body, token=token)


# --- execute a job locally ---------------------------------------------------

# stdout parsers — what the trainer prints at the end of a run. Kept in sync with
# scripts/worker.py (copied, NOT imported: the runner must not depend on scripts/).
_VAL_RE = re.compile(r"Final Val Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_VAL_FALLBACK_RE = re.compile(r"val[_ ]?loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_TRAIN_RE = re.compile(r"Final Train Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_ACC_RE = re.compile(r"Final Val Accuracy[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)

# The ONLY script a job is allowed to run. A queue command is, by construction,
# `python run_experiment.py` + a self-contained config (EXPERIMENT_CONFIG). The
# runner refuses anything else, so claiming a job can never mean RCE on a donor's
# box. This is the trust boundary that makes the client safe to hand to strangers.
ALLOWED_SCRIPT = "run_experiment.py"


def _last(rx: re.Pattern, text: str):
    m = rx.findall(text or "")
    return float(m[-1]) if m else None


class RefusedCommand(RuntimeError):
    """The job's command is not the pinned run_experiment.py — refused unrun."""


def execute(job: dict, repo: str | Path, python: str = "python",
            timeout: int = 3600, dry: bool = False,
            extra_env: dict | None = None) -> dict:
    """Run one job LOCALLY and return a result dict ready for report().

    Refuses (RefusedCommand) any job whose command isn't the pinned
    run_experiment.py — the trust boundary. The self-contained config is passed
    via EXPERIMENT_CONFIG (never interpolated into a shell string). Captures
    stdout, parses the final metrics, and returns {ok, queue_item_id, seed,
    config, content_hash, final_*}. A dry run sets --dry and reports nothing.
    """
    import os

    command = job.get("command") or f"{python} {ALLOWED_SCRIPT}"
    if ALLOWED_SCRIPT not in command:
        raise RefusedCommand(
            f"refusing job {job.get('id')}: command {command!r} is not the pinned "
            f"{ALLOWED_SCRIPT} — Voidrunner only runs config-driven experiments")

    repo = Path(repo)
    if not (repo / ALLOWED_SCRIPT).exists():
        raise FileNotFoundError(
            f"{ALLOWED_SCRIPT} not found in repo {repo} — set the runner's repo to "
            f"the research repo that contains it")

    config = job.get("config")
    seed = config.get("seed") if isinstance(config, dict) else None

    # Build argv WITHOUT a shell: [python, run_experiment.py, ...flags]. The
    # config travels in the environment, never on the command line.
    argv = [python, ALLOWED_SCRIPT]
    if dry:
        argv.append("--dry")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    if config is not None:
        env["EXPERIMENT_CONFIG"] = json.dumps(config)

    try:
        proc = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True,
                              timeout=timeout, env=env)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ok = proc.returncode == 0 and (not dry or "DRY_OK" in out)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out, ok, rc = f"timeout after {timeout}s", False, -1
    except Exception as e:  # noqa: BLE001
        out, ok, rc = f"runner error: {e}", False, -1

    # Capture the reproducibility bundle alongside the metrics: the research repo's
    # git triple and the training venv's runtime stack, so report() can pin exactly
    # what produced this number. Skipped on a dry run (it reports nothing).
    git = {} if dry else repo_git(repo)
    env = {} if dry else probe_env(python)

    return {
        "queue_item_id": job.get("id"),
        "ok": ok,
        "returncode": rc,
        "dry": dry,
        "command": command,
        "config": config,
        "content_hash": job.get("content_hash"),
        "seed": seed,
        "final_val_loss": _last(_VAL_RE, out) or _last(_VAL_FALLBACK_RE, out),
        "final_train_loss": _last(_TRAIN_RE, out),
        "final_val_accuracy": _last(_ACC_RE, out),
        "git_commit": git.get("git_commit"),
        "git_branch": git.get("git_branch"),
        "git_dirty": git.get("git_dirty"),
        "env": env or None,
        "stdout": out,
    }


def run_id_hint(job: dict) -> str:
    """A short local id for logging a run before the server assigns the real one."""
    return f"{job.get('id', 'job')}--{uuid.uuid4().hex[:8]}"
