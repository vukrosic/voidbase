"""runner/cli.py — the `voidrunner` command line.

A compute donor's whole workflow:

    voidrunner register --handle alice         # once: get a bearer token
    export VOIDRUNNER_TOKEN=...                 # save the token it prints
    export VOIDRUNNER_REPO=/path/to/research    # the repo with run_experiment.py
    voidrunner once                            # claim + run + report one job
    voidrunner loop                            # drain the queue, forever
    voidrunner once --dry                      # claim + validate config, report nothing

Config is read from flags first, then the environment:
    VOIDRUNNER_API        voidbase API base url (default http://127.0.0.1:8787)
    VOIDRUNNER_TOKEN      bearer token from `register` (omit on localhost)
    VOIDRUNNER_REPO       path to the research repo containing run_experiment.py
    VOIDRUNNER_PYTHON     python to run the experiment with (default "python")
    VOIDRUNNER_GPU_CLASS  advertise/override this box's GPU class
    VOIDRUNNER_THREAD     only claim jobs from this research thread
    VOIDRUNNER_JOB_TIMEOUT seconds before a run is killed (default 3600)
    VOIDRUNNER_LOG_DIR    where full run stdout is saved (default ./voidrunner-logs)

This module is a thin driver over runner.core — all the protocol lives there, so
an agent that wants finer control can import the three calls directly instead.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from runner import core


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or default


def log(msg: str) -> None:
    print(f"[voidrunner {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def _config_from_args(args) -> dict:
    api = args.api or _env("VOIDRUNNER_API") or core.DEFAULT_API
    return {
        "api": api,
        "token": getattr(args, "token", None) or _env("VOIDRUNNER_TOKEN"),
        "repo": getattr(args, "repo", None) or _env("VOIDRUNNER_REPO"),
        "python": _env("VOIDRUNNER_PYTHON", "python"),
        "gpu_class": _env("VOIDRUNNER_GPU_CLASS"),
        "thread": getattr(args, "thread", None) or _env("VOIDRUNNER_THREAD"),
        "timeout": int(_env("VOIDRUNNER_JOB_TIMEOUT", "3600")),
        "log_dir": Path(_env("VOIDRUNNER_LOG_DIR", "voidrunner-logs")),
    }


def cmd_register(args) -> int:
    api = args.api or _env("VOIDRUNNER_API") or core.DEFAULT_API
    out = core.register(api, args.handle)
    log(f"registered '{out['handle']}' (contributor {out['contributor_id']})")
    print(out["token"])  # stdout = the token, so `export VOIDRUNNER_TOKEN=$(...)`
    log("^ this is your bearer token. Save it now — it is shown ONCE.")
    log("   export VOIDRUNNER_TOKEN=<that token>")
    return 0


def _run_one(cfg: dict, dry: bool) -> bool:
    """Claim → execute (with heartbeat) → report/release one job. Returns False
    when the queue had nothing to claim."""
    if not cfg["repo"]:
        log("VOIDRUNNER_REPO is not set — point it at the research repo that "
            "contains run_experiment.py. Refusing to run.")
        raise SystemExit(2)

    box = core.local_box(gpu_class=cfg["gpu_class"])
    claimed = core.claim(cfg["api"], cfg["token"], box,
                         thread=cfg["thread"], gpu_class_filter=cfg["gpu_class"])
    box_id = claimed["box_id"]
    job = claimed.get("job")
    if not job:
        return False
    log(f"claimed {job['id']} (thread={job.get('thread')})")

    with core._Heartbeat(cfg["api"], box_id):
        try:
            result = core.execute(job, cfg["repo"], python=cfg["python"],
                                  timeout=cfg["timeout"], dry=dry)
        except core.RefusedCommand as e:
            # A job we won't run is handed straight back so another box (or the
            # operator's trusted worker) can take it — never silently dropped.
            log(f"REFUSED: {e}")
            core.release(cfg["api"], cfg["token"], job["id"])
            return True

    # Persist full stdout locally so the donor can debug a failure (voidbase keeps
    # only the parsed metrics). Best-effort.
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
        (cfg["log_dir"] / f"{job['id']}.log").write_text(
            f"# ok={result['ok']} rc={result['returncode']} dry={dry}\n"
            f"# cmd: {result['command']}\n\n{result.get('stdout', '')}")
    except Exception:  # noqa: BLE001
        pass

    if dry:
        # A dry run only validates the config; it must leave no run row. Hand the
        # job back so the real run can still happen.
        log(f"DRY {job['id']} -> {'OK' if result['ok'] else 'FAIL'}; releasing")
        core.release(cfg["api"], cfg["token"], job["id"])
        return True

    rep = core.report(cfg["api"], cfg["token"], result, box_id)
    log(f"reported {rep['run_id']} -> {'done' if result['ok'] else 'failed'} "
        f"(val_loss={result['final_val_loss']} verification={rep['verification']})")
    return True


def cmd_once(args) -> int:
    cfg = _config_from_args(args)
    ran = _run_one(cfg, dry=args.dry)
    log("ran one job" if ran else "queue empty — nothing to claim")
    return 0


def cmd_loop(args) -> int:
    cfg = _config_from_args(args)
    log(f"draining queue (idle poll {args.interval}s, ctrl-c to stop)")
    while True:
        try:
            if not _run_one(cfg, dry=args.dry):
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except core.ApiError as e:
            log(f"api error ({e.status}); retrying in {args.interval}s")
            time.sleep(args.interval)
        except Exception as e:  # noqa: BLE001
            log(f"loop error ({type(e).__name__}: {e}); retrying in {args.interval}s")
            time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voidrunner",
                                 description="voidbase compute-donor client")
    ap.add_argument("--api", help="voidbase API base url (or VOIDRUNNER_API)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="mint a contributor + bearer token")
    p_reg.add_argument("--handle", required=True, help="your contributor handle")
    p_reg.set_defaults(func=cmd_register)

    for name, fn, helptext in (("once", cmd_once, "claim + run + report one job"),
                               ("loop", cmd_loop, "drain the queue forever")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--token", help="bearer token (or VOIDRUNNER_TOKEN)")
        p.add_argument("--repo", help="research repo path (or VOIDRUNNER_REPO)")
        p.add_argument("--thread", help="only claim jobs from this thread")
        p.add_argument("--dry", action="store_true",
                       help="validate the config on the box; report nothing")
        if name == "loop":
            p.add_argument("--interval", type=int, default=10,
                           help="idle poll seconds")
        p.set_defaults(func=fn)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
