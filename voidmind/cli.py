"""voidmind/cli.py — run the token-donor idea loop from the command line.

  voidmind register --handle alice            # mint a token (save it!)
  voidmind once                               # one proposal pass over a thread
  voidmind loop                               # keep proposing every --interval
  voidmind once --dry                         # show what WOULD enqueue, write nothing

Config via flags or env:
  VOIDMIND_API         voidbase API base url            (default http://127.0.0.1:8787)
  VOIDMIND_TOKEN       bearer token from `register`
  VOIDMIND_THREAD      thread to propose against        (else: highest-priority open one)
  VOIDMIND_BASE        path to the champion base config (champion.json or resolved json)
  VOIDMIND_LLM_KEY     the donor's OWN LLM api key       (spends the donor's tokens)
  VOIDMIND_MODEL       model id                          (default claude-sonnet-4-6)
  VOIDMIND_LLM_BASE_URL  LLM api base                    (default https://api.anthropic.com)
  VOIDMIND_LIMIT       max enqueues per pass             (default 5)
  VOIDMIND_PRIORITY    queue priority for proposals      (default 0)
  VOIDMIND_INTERVAL    seconds between loop passes        (default 900)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voidmind import core  # noqa: E402
from voidmind.propose import DEFAULT_MODEL, llm_proposer  # noqa: E402


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def load_base(path: str | None) -> dict | None:
    """Load the champion base config the proposer builds deltas on. Accepts either
    an already-resolved config ({config_class, env, fields, seed}) or a research
    repo champion.json ({config_class, env, flags:[...], config_overrides:{...},
    seed}) — the latter is mapped the same way scripts/feeder.champion_base does
    (flags → {flag: true}, then config_overrides), so a donor points it straight at
    their champion.json. Returns None if no path (proposals must then be
    self-contained)."""
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    if "fields" in data:  # already a resolved base
        return {
            "config_class": data.get("config_class"),
            "env": dict(data.get("env") or {}),
            "fields": dict(data.get("fields") or {}),
            "seed": data.get("seed", 42),
            "dataset_path": data.get("dataset_path", core.voidconfig.DEFAULT_DATASET_PATH),
        }
    fields = {f: True for f in data.get("flags", [])}
    fields.update(data.get("config_overrides") or {})
    return {
        "config_class": data.get("config_class", "configs.llm_config.Tiny1M3MAlibiConfig"),
        "env": dict(data.get("env") or {}),
        "fields": fields,
        "seed": data.get("seed", 42),
    }


def pick_thread(api: str, explicit: str | None) -> str:
    """The thread to propose against: the explicit one, else the highest-priority
    open thread the API reports."""
    if explicit:
        return explicit
    threads = core.open_threads(api, status="active")
    if not threads:
        raise SystemExit("no open threads to propose against (set --thread)")
    return threads[0]["name"]


def _proposer_from_env(args):
    key = args.llm_key or _env("VOIDMIND_LLM_KEY")
    if not key:
        raise SystemExit(
            "no LLM key: set VOIDMIND_LLM_KEY (the donor's own) or --llm-key")
    return llm_proposer(key=key, model=args.model,
                        base_url=args.llm_base_url, n=args.limit)


def cmd_register(args) -> int:
    out = core.register(args.api, args.handle)
    print(json.dumps(out, indent=2))
    print("\nSAVE THIS TOKEN — it is shown once. Export it as VOIDMIND_TOKEN.",
          file=sys.stderr)
    return 0


def cmd_once(args) -> int:
    base = load_base(args.base)
    thread = pick_thread(args.api, args.thread)
    proposer = _proposer_from_env(args)
    results = core.run_once(args.api, args.token, thread, base, proposer,
                            limit=args.limit, priority=args.priority, dry=args.dry)
    print(json.dumps({"thread": thread, "results": results}, indent=2, default=str))
    enq = sum(1 for r in results if r.get("queue_item_id") and not r.get("deduped"))
    dd = sum(1 for r in results if r.get("deduped"))
    print(f"[voidmind] thread={thread} enqueued={enq} deduped={dd} "
          f"{'(DRY)' if args.dry else ''}", file=sys.stderr)
    return 0


def cmd_loop(args) -> int:
    base = load_base(args.base)
    proposer = _proposer_from_env(args)
    while True:
        try:
            thread = pick_thread(args.api, args.thread)
            results = core.run_once(args.api, args.token, thread, base, proposer,
                                    limit=args.limit, priority=args.priority)
            enq = sum(1 for r in results if r.get("queue_item_id") and not r.get("deduped"))
            print(f"[voidmind] thread={thread} enqueued={enq}", file=sys.stderr)
        except core.ApiError as e:
            print(f"[voidmind] api error: {e}", file=sys.stderr)
        except SystemExit as e:
            print(f"[voidmind] {e}", file=sys.stderr)
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voidmind", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=_env("VOIDMIND_API", core.DEFAULT_API))
    ap.add_argument("--token", default=_env("VOIDMIND_TOKEN"))
    ap.add_argument("--thread", default=_env("VOIDMIND_THREAD"))
    ap.add_argument("--base", default=_env("VOIDMIND_BASE"))
    ap.add_argument("--llm-key", default=None)
    ap.add_argument("--model", default=_env("VOIDMIND_MODEL", DEFAULT_MODEL))
    ap.add_argument("--llm-base-url",
                    default=_env("VOIDMIND_LLM_BASE_URL", "https://api.anthropic.com"))
    ap.add_argument("--limit", type=int, default=int(_env("VOIDMIND_LIMIT", "5")))
    ap.add_argument("--priority", type=int, default=int(_env("VOIDMIND_PRIORITY", "0")))
    ap.add_argument("--interval", type=int, default=int(_env("VOIDMIND_INTERVAL", "900")))

    sub = ap.add_subparsers(dest="cmd", required=True)
    p_reg = sub.add_parser("register", help="mint a contributor + token")
    p_reg.add_argument("--handle", required=True)
    p_reg.set_defaults(func=cmd_register)

    p_once = sub.add_parser("once", help="one proposal pass")
    p_once.add_argument("--dry", action="store_true", help="show, write nothing")
    p_once.set_defaults(func=cmd_once)

    p_loop = sub.add_parser("loop", help="keep proposing every --interval seconds")
    p_loop.set_defaults(func=cmd_loop)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
