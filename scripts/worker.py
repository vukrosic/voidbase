#!/usr/bin/env python3
"""worker.py — Neon-coordinated experiment worker (the mass-automation engine).

Any machine runs this. It ATOMICALLY claims a job from the shared Neon queue —
no two workers ever get the same job, which is the whole reason coordination
lives in Postgres and not GitHub issues — runs it in the research repo, and
reports the result back to Neon. Stateless: identity is just (contributor, box)
rows keyed by hostname, so you scale to N GPUs by running N copies. This is also
the seed of the `voidbase-runner` a compute donor will one day `pip install`.

The claim is one statement, collision-proof under any concurrency:

    update queue_items set status='claimed', claimed_by_box=$me, lease=now()+30m
    where id = ( select id from queue_items
                 where status='needs-run' or lease_expired
                 order by priority desc, created_at
                 for update skip locked limit 1 )
    returning ...

FOR UPDATE SKIP LOCKED is the textbook Postgres job-queue lock: a second worker
firing at the same instant skips the locked row and takes the next one — nobody
blocks, nobody double-runs. Expired leases are auto-reclaimed so a dead worker
never strands a job.

  python3 scripts/worker.py claim-test   # PROVE atomic claims never collide (no GPU)
  python3 scripts/worker.py once          # claim + run + report one job, exit
  python3 scripts/worker.py loop          # drain the queue forever
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("PGCONNECT_TIMEOUT", "10")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect, env_value  # noqa: E402

DEFAULT_REPO = Path("/Users/vukrosic/my-life/llm-research-kit-scaling")
LEASE_SECONDS = int(os.environ.get("VOIDBASE_LEASE_SECONDS", "1800"))
JOB_TIMEOUT = int(os.environ.get("VOIDBASE_JOB_TIMEOUT", "900"))
# How many SSH/box-drop re-queues a single job gets before the worker gives up and
# records it `failed`. Bounds the infra-retry loop (queue_items.attempts, migration
# 0010) so a dead box can't cycle a job forever. 3 = two free retries past the first.
MAX_INFRA_ATTEMPTS = int(os.environ.get("VOIDBASE_MAX_INFRA_ATTEMPTS", "3"))
THREAD_FALLBACK = "tiny1m3m"

# --- GPU box: experiments TRAIN on the rented box via SSH, never locally ------
# CRITICAL: Neon write-creds (DATABASE_URL/.env) stay on THIS machine. The worker
# runs here (where the creds live) and executes the experiment ON THE BOX over
# SSH, capturing stdout. We never copy the connection string to rented infra.
#
# The SSH target is NEVER hard-coded — it is read from the environment or the
# gitignored voidbase/.env (see db.conn.env_value), so no box address or port
# ever lands in a commit. Set VOIDBASE_BOX_HOST / _PORT / _USER (see README and
# .env.example). The host is required for `once`/`loop`; main() fails fast with a
# clear message if it is unset.
BOX_SSH_HOST = env_value("VOIDBASE_BOX_HOST")
BOX_SSH_PORT = env_value("VOIDBASE_BOX_PORT", "22")
BOX_SSH_USER = env_value("VOIDBASE_BOX_USER", "root")
BOX_REPO = env_value("VOIDBASE_BOX_REPO", "/root/universe-lm")
BOX_PYTHON = env_value("VOIDBASE_BOX_PYTHON", "/venv/main/bin/python")
# Box-prep env exported before every training command. sm_86 (RTX 3060) crashes
# mid-run unless torch.compile/dynamo is OFF — remote-box.json documents this and
# the old daemon path set it; the config-row path MUST too (a missing
# TORCHDYNAMO_DISABLE was the headmix run's 7.14 "failed" crash). Harmless on
# other GPUs. Override per-box with VOIDBASE_BOX_ENV.
BOX_ENV = env_value("VOIDBASE_BOX_ENV", "TORCHDYNAMO_DISABLE=1")
# Dataset cache: a persistent dir ON THE BOX where the ~15GB dataset is downloaded
# once and reused, instead of re-fetched every run. When VOIDBASE_DATASET_CACHE is
# set we export HF_HOME / HF_DATASETS_CACHE pointing at it before the training
# command, so HuggingFace skips the download when the data is already present. The
# dir must live on the box's PERSISTENT volume (survives a box restart). Unset =>
# unchanged behavior. See README "Dataset cache".
DATASET_CACHE = env_value("VOIDBASE_DATASET_CACHE")

# --- heartbeat: prove this box is alive so the reaper doesn't requeue its job --
# The worker pings the voidbase API every HEARTBEAT_SECONDS while a job runs. If
# THIS process (the dispatcher driving the box) dies, the pings stop and the
# reaper requeues the stranded job after the heartbeat timeout — exactly the
# self-healing the loop needs. Best-effort: a ping failure never touches the run.
API_URL = env_value("VOIDBASE_API_URL", "http://127.0.0.1:8787")
HEARTBEAT_SECONDS = int(env_value("VOIDBASE_HEARTBEAT_SECONDS", "30"))
# Where the full box stdout/stderr of every run is saved. Neon stores only the
# parsed val + done/failed; a crashing lever (e.g. headmix) needs the actual
# traceback to debug. One file per job, overwritten on retry.
RUN_LOG_DIR = Path(os.environ.get("VOIDBASE_RUN_LOG_DIR",
                                  str(Path(__file__).resolve().parent.parent / "run-logs")))


def log(msg: str) -> None:
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# --- heartbeat ---------------------------------------------------------------

def post_heartbeat(box_id: str) -> None:
    """Best-effort liveness ping to the voidbase API (POST box_heartbeat). Never
    raises — a missing API server or a network blip must not kill a multi-minute
    training run; the reaper handles a genuinely dead box, not a flaky ping."""
    try:
        # box_id is a DB uuid object — coerce to str or json.dumps raises. Keep the
        # whole thing inside the try so the "never raises" contract actually holds:
        # a serialization slip must not kill a multi-minute training run either.
        payload = json.dumps({"resource": "box_heartbeat", "box_id": str(box_id)}).encode()
        req = urllib.request.Request(
            f"{API_URL.rstrip('/')}/box_heartbeat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        log(f"heartbeat failed ({type(e).__name__}: {e}) — continuing")


class Heartbeat:
    """Pings the API every HEARTBEAT_SECONDS in a background thread for the
    lifetime of one run. Start before the box command, stop in a finally. The
    first beat fires immediately so a job is marked live the instant it starts."""

    def __init__(self, box_id: str) -> None:
        self.box_id = box_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        post_heartbeat(self.box_id)  # immediate first beat
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            post_heartbeat(self.box_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


# --- identity: who is this worker -------------------------------------------

def ensure_identity(conn) -> str:
    """Return this worker's box id, creating the singleton automation
    contributor + a box row keyed by the REMOTE GPU it drives. Idempotent.

    The box row must identify the GPU the experiment trains on, NOT this Mac —
    otherwise two workers pinned to two different boxes (via VOIDBASE_BOX_HOST/
    PORT) collapse onto one box_id and the dashboard can't tell the GPUs apart.
    Fingerprint = host:port of the rented box; the Mac is just the dispatcher."""
    remote = f"{BOX_SSH_HOST}:{BOX_SSH_PORT}"
    cur = conn.cursor()
    cur.execute(
        """insert into contributors (handle, role)
           values ('automation', 'maintainer')
           on conflict (handle) do update set handle = excluded.handle
           returning id""",
    )
    contributor_id = cur.fetchone()[0]
    cur.execute(
        """insert into boxes (contributor_id, label, gpu_class, fingerprint)
           values (%s, %s, 'any', %s)
           on conflict (contributor_id, fingerprint)
             do update set label = excluded.label
           returning id""",
        (contributor_id, f"box:{remote}", remote),
    )
    box_id = cur.fetchone()[0]
    conn.commit()
    log(f"dispatcher {socket.gethostname()} -> GPU box {remote}")
    return box_id


# --- the keystone: atomic claim ---------------------------------------------

CLAIM_SQL = """
update queue_items q
set status = 'claimed',
    claimed_by_box = %(box)s,
    claimed_at = now(),
    lease_expires_at = now() + make_interval(secs => %(lease)s)
from (
    select id
    from queue_items
    where status = 'needs-run'
       or (status in ('claimed', 'running')
           and lease_expires_at is not null
           and lease_expires_at < now())          -- reclaim a dead worker's job
    order by priority desc, created_at asc
    for update skip locked                          -- collision-proof
    limit 1
) pick
where q.id = pick.id
returning q.id, q.thread_name, q.name, q.command, q.config, q.content_hash;
"""


def claim_next(conn, box_id: str):
    """Atomically lease the next runnable job, or None. Safe under any number
    of concurrent workers — that's the SKIP LOCKED guarantee."""
    cur = conn.cursor()
    cur.execute(CLAIM_SQL, {"box": box_id, "lease": LEASE_SECONDS})
    row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return {"id": row[0], "thread": row[1] or THREAD_FALLBACK, "name": row[2],
            "command": row[3], "config": row[4], "content_hash": row[5]}


def mark_running(conn, qid: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "update queue_items set status='running', started_at=now() where id=%s", (qid,))
    conn.commit()


_VAL_RE = re.compile(r"Final Val Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_TRAIN_RE = re.compile(r"Final Train Loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
_ACC_RE = re.compile(r"Final Val Accuracy[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)
# Fallback for the bare "val loss N.NNN" shape some legacy runs print.
_VAL_FALLBACK_RE = re.compile(r"val[_ ]?loss[\s:=]+([0-9]+\.[0-9]+)", re.IGNORECASE)


def _last(rx, text: str):
    matches = rx.findall(text or "")
    return float(matches[-1]) if matches else None


def parse_val_loss(text: str):
    """The final val loss the trainer printed ('Final Val Loss:  6.2219')."""
    return _last(_VAL_RE, text) or _last(_VAL_FALLBACK_RE, text)


def parse_train_loss(text: str):
    return _last(_TRAIN_RE, text)


def parse_val_accuracy(text: str):
    return _last(_ACC_RE, text)


def report(conn, job: dict, box_id: str, ok: bool, val_loss, command: str, tail: str,
           train_loss=None, val_accuracy=None, repro: dict | None = None) -> None:
    """Write a run row (born unverified — the champion only moves through the
    confirm gate) and close out the queue item. Carries the config + content_hash
    so the result is dedupable and reproducible. One transaction.

    `repro` (from probe_box_repro) pins the box's git triple + runtime env, so an
    operator-promoted champion gets the same reproducibility bundle as a donor's —
    best-effort and nullable, so a failed probe never blocks the report.

    A DRY run NEVER lands here — it only validates the resolved config on the box
    and must not pollute `runs` (a null-val dry row would dedup-block the real
    run). See run_one: dry returns before reporting, mirroring claim-test."""
    run_id = f"{job['id']}--{uuid.uuid4().hex[:8]}"
    config = job.get("config")
    repro = repro or {}
    env = repro.get("env")
    cur = conn.cursor()
    cur.execute(
        """insert into runs (id, queue_item_id, thread_name, name, box_id,
                             command, config, content_hash, status,
                             final_val_loss, final_train_loss, final_val_accuracy,
                             git_commit, git_branch, git_dirty, env,
                             finished_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, now())""",
        (run_id, job["id"], job["thread"], job["name"], box_id,
         command, json.dumps(config) if config is not None else None,
         job.get("content_hash"), "done" if ok else "failed",
         val_loss, train_loss, val_accuracy,
         repro.get("git_commit"), repro.get("git_branch"), repro.get("git_dirty"),
         json.dumps(env) if env is not None else None),
    )
    cur.execute(
        "update queue_items set status=%s, finished_at=now() where id=%s",
        ("done" if ok else "failed", job["id"]),
    )
    conn.commit()
    log(f"reported {job['id']} -> {'done' if ok else 'failed'} "
        f"(val_loss={val_loss} train_loss={train_loss} val_acc={val_accuracy})")


# --- run one job -------------------------------------------------------------

DRY = bool(os.environ.get("VOIDBASE_DRY"))


def build_remote_cmd(job: dict, dry: bool) -> str:
    """The shell command that TRAINS this job ON THE BOX. A config-row job feeds
    its self-contained config to the generic run_experiment.py via EXPERIMENT_CONFIG
    (shell-quoted so the remote shell receives the JSON intact); legacy
    command-only jobs run their command verbatim in the repo. `--dry` builds and
    validates the config on the box without touching the GPU."""
    cmd = job.get("command") or f"{shlex.quote(BOX_PYTHON)} run_experiment.py"
    if "run_experiment.py" in cmd:
        # normalise to the box's venv python (the queue stores a bare "python")
        cmd = re.sub(r"^python\b", shlex.quote(BOX_PYTHON), cmd)
    if dry and "run_experiment.py" in cmd and "--dry" not in cmd:
        cmd += " --dry"
    prefix = f"cd {shlex.quote(BOX_REPO)} && {BOX_ENV} "
    if DATASET_CACHE:
        cache = shlex.quote(DATASET_CACHE)
        # Point HuggingFace at the persistent cache so the dataset downloads once
        # and is reused on every subsequent run (skips the ~15GB re-fetch).
        prefix += f"HF_HOME={cache} HF_DATASETS_CACHE={cache} "
    config = job.get("config")
    if config is not None:
        prefix += f"EXPERIMENT_CONFIG={shlex.quote(json.dumps(config))} "
    return prefix + cmd


def run_on_box(remote_cmd: str):
    """SSH the command to the GPU box and capture its stdout/stderr. Neon creds
    never leave this machine — only the resolved experiment config crosses to the
    box. Returns (returncode, combined_output)."""
    ssh = [
        "ssh", "-p", str(BOX_SSH_PORT),
        "-o", "ConnectTimeout=20",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        # Keepalives so a multi-minute training run doesn't get dropped by an idle
        # NAT/firewall or a brief network blip: ping every 30s, tolerate ~5 min of
        # silence (10 misses) before giving up. A dropped run is wasted GPU + a
        # spurious "failed" (a real qk_layernorm run was lost this way).
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=10",
        f"{BOX_SSH_USER}@{BOX_SSH_HOST}", remote_cmd,
    ]
    proc = subprocess.run(ssh, capture_output=True, text=True, timeout=JOB_TIMEOUT)
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


# A tiny script that prints the box's reproducibility fingerprint as one
# `VOIDBASE_REPRO={json}` line: the BOX_REPO git triple + the training venv's
# python/torch/CUDA/gpu. Run with BOX_PYTHON after a `cd BOX_REPO`, so git sees the
# repo and torch reflects the same interpreter the job trained with.
_REPRO_PROBE_PY = (
    "import json,platform,subprocess\n"
    "def g(*a):\n"
    "    try:\n"
    "        o=subprocess.run(['git',*a],capture_output=True,text=True,timeout=10)\n"
    "        return (o.stdout.strip() or None) if o.returncode==0 else None\n"
    "    except Exception: return None\n"
    "env={'python':platform.python_version(),'platform':platform.platform()}\n"
    "try:\n"
    "    import torch\n"
    "    env['torch']=torch.__version__\n"
    "    env['cuda']=getattr(torch.version,'cuda',None)\n"
    "    if torch.cuda.is_available(): env['gpu']=torch.cuda.get_device_name(0)\n"
    "except Exception: pass\n"
    "inside=g('rev-parse','--is-inside-work-tree')=='true'\n"
    "print('VOIDBASE_REPRO='+json.dumps({'git_commit':g('rev-parse','HEAD'),"
    "'git_branch':g('rev-parse','--abbrev-ref','HEAD'),"
    "'git_dirty':(bool(g('status','--porcelain')) if inside else None),'env':env}))\n")


def parse_repro(out: str) -> dict:
    """Pull the {git_commit, git_branch, git_dirty, env} dict out of a probe's
    output (the line after the VOIDBASE_REPRO= marker). Pure; {} if absent or
    malformed, so a probe miss never breaks the report."""
    for line in (out or "").splitlines():
        if line.startswith("VOIDBASE_REPRO="):
            try:
                return json.loads(line[len("VOIDBASE_REPRO="):])
            except Exception:  # noqa: BLE001
                return {}
    return {}


def probe_box_repro() -> dict:
    """SSH a short probe to the box and return its reproducibility fingerprint.
    Best-effort: one fast extra SSH against a multi-minute training run, and any
    failure (probe error, no torch, non-git repo) returns {} — the run is still
    reported, just with a less complete bundle."""
    cmd = (f"cd {shlex.quote(BOX_REPO)} && "
           f"{shlex.quote(BOX_PYTHON)} -c {shlex.quote(_REPRO_PROBE_PY)}")
    try:
        rc, out = run_on_box(cmd)
        return parse_repro(out) if rc == 0 else {}
    except Exception:  # noqa: BLE001
        return {}


# Markers that a run FAILED because the SSH/box connection dropped — an infra
# blip, NOT a training failure. Such a job never got a fair shot, so it must be
# RE-QUEUED (retried), not recorded as a failed experiment (which would poison the
# search's signal and dedup-block a real retry). A genuine training crash instead
# exits 0/1 with a Python traceback and is a real `failed`.
_INFRA_FAIL_MARKERS = (
    "closed by remote host", "Connection to", "kex_exchange",
    "Connection reset", "Connection timed out", "Broken pipe",
    "Connection closed", "client_loop",
)


def is_transient_infra_failure(rc: int, out: str) -> bool:
    """True when a non-OK run looks like an SSH/box drop rather than a training
    failure. rc 255 is ssh's own connection-error code; the markers catch the same
    in the captured output. Pure — unit-tested."""
    if rc == 255:
        return True
    return any(m in (out or "") for m in _INFRA_FAIL_MARKERS)


def run_one(repo: Path, box_id: str) -> bool:
    # Claim + mark-running on a short-lived connection, then CLOSE it: a box job
    # trains for minutes and Neon drops an idle connection, so holding one across
    # the job would lose the result at report time (learned the hard way). Each DB
    # touch opens its own connection — stateless and drop-proof; ~0.5s of connect
    # overhead is nothing against a multi-minute training run.
    conn = connect()
    try:
        job = claim_next(conn, box_id)
        if not job:
            return False
        log(f"claimed {job['id']}: {job['command']}")
        mark_running(conn, job["id"])
    finally:
        conn.close()

    remote_cmd = build_remote_cmd(job, DRY)
    log(f"box <- {remote_cmd[:160]}")
    # Ping every ~30s for the life of the run so the reaper can tell this box is
    # alive. If this dispatcher dies, the pings stop and the job is requeued.
    heartbeat = Heartbeat(box_id)
    heartbeat.start()
    try:
        rc, out = run_on_box(remote_cmd)            # no DB connection held here
        ok = rc == 0 and (not DRY or "DRY_OK" in out)
    except subprocess.TimeoutExpired:
        log(f"{job['id']} TIMEOUT after {JOB_TIMEOUT}s")
        rc, out, ok = -1, "timeout", False
    except Exception as e:  # noqa: BLE001
        log(f"{job['id']} crashed: {e}")
        rc, out, ok = -1, str(e), False
    finally:
        heartbeat.stop()

    # Persist the FULL box output so a failed run is debuggable (Neon keeps only
    # the parsed val). Best-effort: never let logging break the report.
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_LOG_DIR / f"{job['id']}.log").write_text(
            f"# rc={rc} ok={ok}\n# cmd: {remote_cmd}\n\n{out}")
    except Exception:  # noqa: BLE001
        pass

    conn = connect()                                # fresh connection to report
    try:
        if DRY:
            # A dry run only validates the resolved config on the box; it must
            # NOT write a runs row (a null-val row would dedup-block the real run).
            log(f"DRY {job['id']} -> {'OK' if ok else 'FAIL'} (rc={rc}); no runs row written")
            cur = conn.cursor()
            cur.execute("update queue_items set status='needs-run', started_at=null,"
                        "claimed_by_box=null, claimed_at=null, lease_expires_at=null "
                        "where id=%s", (job["id"],))
            conn.commit()
            return True
        # An SSH/box drop is an infra failure, not a verdict: the experiment never
        # produced a result, so re-queue it to retry instead of poisoning the search
        # with a spurious `failed` (and dedup-blocking the real run). Only when no
        # val_loss was parsed — if the run actually finished and THEN the connection
        # dropped, we still have the result and should record it.
        if (not ok and parse_val_loss(out) is None
                and is_transient_infra_failure(rc, out)):
            cur = conn.cursor()
            # Count the infra failure; give up after the cap so a genuinely dead box
            # (accepts SSH then drops every run) can't cycle a job needs-run -> drop
            # -> needs-run forever. Under the cap = re-queue (transient blip); at the
            # cap = record `failed` for real (fall through to report) and free the slot.
            cur.execute("update queue_items set attempts = attempts + 1 "
                        "where id=%s returning attempts", (job["id"],))
            row = cur.fetchone()
            attempts = row[0] if row else MAX_INFRA_ATTEMPTS
            if attempts < MAX_INFRA_ATTEMPTS:
                cur.execute("update queue_items set status='needs-run', started_at=null, "
                            "claimed_by_box=null, claimed_at=null, lease_expires_at=null "
                            "where id=%s", (job["id"],))
                conn.commit()
                log(f"{job['id']} infra failure (rc={rc}, attempt "
                    f"{attempts}/{MAX_INFRA_ATTEMPTS}) — RE-QUEUED")
                return True
            conn.commit()
            log(f"{job['id']} infra failure (rc={rc}) hit the "
                f"{MAX_INFRA_ATTEMPTS}-attempt cap — recording FAILED (box likely down)")
            # fall through to report() with ok=False → a real `failed` row
        # Probe the box's reproducibility fingerprint (git + runtime stack) so the
        # reported run carries a re-runnable bundle. Best-effort, off the DB
        # connection — a probe miss just yields a less complete bundle.
        repro = probe_box_repro()
        report(conn, job, box_id, ok,
               parse_val_loss(out), remote_cmd, out[-2000:],
               train_loss=parse_train_loss(out), val_accuracy=parse_val_accuracy(out),
               repro=repro)
    finally:
        conn.close()
    return True


# --- proof: concurrent claims never collide (no GPU needed) ------------------

def claim_test(conn, n_jobs: int = 6, n_workers: int = 8) -> int:
    """Insert N synthetic needs-run jobs, fire M workers at the queue
    simultaneously, and assert every claim is distinct. Cleans up after."""
    import threading

    box_id = ensure_identity(conn)
    cur = conn.cursor()
    cur.execute(
        "insert into threads (name, hypothesis, status) values (%s,'claim-test','active') "
        "on conflict (name) do nothing", (THREAD_FALLBACK,))
    tag = f"_claimtest_{uuid.uuid4().hex[:6]}"
    ids = [f"{tag}_{i}" for i in range(n_jobs)]
    for jid in ids:
        cur.execute(
            "insert into queue_items (id, thread_name, name, command, status) "
            "values (%s,%s,%s,'true','needs-run')", (jid, THREAD_FALLBACK, jid))
    conn.commit()
    log(f"seeded {n_jobs} synthetic jobs; firing {n_workers} concurrent workers")

    claimed: list[str] = []
    lock = threading.Lock()

    def grab():
        c = connect()
        try:
            bid = ensure_identity(c)
            while True:
                j = claim_next(c, bid)
                if not j or not j["id"].startswith(tag):
                    if j:  # claimed a real job by accident — put it back
                        cc = c.cursor()
                        cc.execute("update queue_items set status='needs-run',"
                                   "claimed_by_box=null,lease_expires_at=null where id=%s", (j["id"],))
                        c.commit()
                    break
                with lock:
                    claimed.append(j["id"])
        finally:
            c.close()

    threads = [threading.Thread(target=grab) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cur.execute("delete from queue_items where id = any(%s)", (ids,))
    conn.commit()

    dupes = len(claimed) - len(set(claimed))
    missed = set(ids) - set(claimed)
    log(f"claimed {len(claimed)} job(s); distinct={len(set(claimed))}; dupes={dupes}; missed={len(missed)}")
    if dupes == 0 and not missed:
        log("PASS — every job claimed exactly once under concurrency")
        return 0
    log(f"FAIL — dupes={dupes} missed={sorted(missed)}")
    return 1


# --- entry -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["once", "loop", "claim-test"], nargs="?", default="once")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--interval", type=int, default=10, help="loop: idle poll seconds")
    args = ap.parse_args()
    repo = Path(args.repo)

    if args.mode == "claim-test":
        conn = connect()
        try:
            return claim_test(conn)
        finally:
            conn.close()

    # The SSH target is never hard-coded — running a job needs it set in the env
    # or voidbase/.env. Fail fast with the exact var name instead of SSHing to
    # "None". (claim-test above never touches a box, so it doesn't need this.)
    if not BOX_SSH_HOST:
        log("VOIDBASE_BOX_HOST is not set — put the box address in the environment "
            "or voidbase/.env (see README / .env.example). Refusing to run a job.")
        return 2

    # Identity once on its own connection; run_one then manages per-op connections.
    conn = connect()
    try:
        box_id = ensure_identity(conn)
    finally:
        conn.close()
    log(f"box {box_id} on {socket.gethostname()}, repo {repo}")

    if args.mode == "once":
        ran = run_one(repo, box_id)
        log("ran one job" if ran else "queue empty — nothing to claim")
    else:
        log(f"draining queue (idle poll {args.interval}s, ctrl-c to stop)")
        while True:
            try:
                if not run_one(repo, box_id):
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                log("stopped")
                break
            except Exception as e:  # noqa: BLE001
                # A transient Neon connection drop (Neon closes idle conns) or an SSH
                # blip must NEVER kill an unattended two-GPU drain — log and retry.
                # The job stays 'running'/'claimed' and is reclaimed via lease expiry.
                log(f"loop error ({type(e).__name__}: {e}); retrying in {args.interval}s")
                time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
