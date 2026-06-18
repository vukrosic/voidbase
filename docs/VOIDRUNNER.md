# Voidrunner — the compute-donor client (build plan)

> Status: **v0 built + tested (phases 1–4)**, 2026-06-18. Lives at
> `voidbase/runner/`; HTTP-only so it extracts to its own repo cleanly once the
> API seam is stable. Sibling spoke: [VOIDMIND.md](VOIDMIND.md).
>
> **Not yet deployed to the live API.** The operator's running server (port 8787)
> still runs the pre-Voidrunner code; restart it to pick up `/register /claim
> /runs /release`. All four endpoints are verified against Neon by
> `tests/test_voidrunner_api.py` (+ `tests/test_runner_core.py`, server-free).

## One line

Voidrunner is the **"donate compute"** client. A contributor installs it on the
machine that has the GPU; it **claims a job from voidbase, runs it locally, and
reports the result back** — speaking **only HTTP**, never touching the DB.

```
voidbase API  ◀── claim ──  Voidrunner  ── run run_experiment.py locally ──▶ GPU
              ── job ──▶                 ── report runs + eval_points ──▶ voidbase
```

## What makes it different from `worker.py` (why it's a new thing)

`worker.py` is the **operator's dispatcher**: it holds the Postgres write creds
(`db.conn.connect()`), claims via direct SQL, and SSHes the job to a *rented*
box. A public donor can do none of that. Voidrunner inverts every one of those:

| | `worker.py` (operator) | Voidrunner (donor) |
|---|---|---|
| DB access | direct Postgres creds | **none** — HTTP API only |
| Claim | direct `CLAIM_SQL` | `POST /claim` (server runs the SQL) |
| Where it runs | SSH to a remote box | **the local machine it's installed on** |
| Identity | the `automation` maintainer | the donor's own contributor + token |
| What it may run | any command | **only the pinned `run_experiment.py`** |

`worker.py` stays as-is — it's your internal dispatcher. Voidrunner is the thing
a stranger `pip install`s.

## Hard rules (the trust boundary)

1. **No DB creds, ever.** `runner/` must not import `db.conn` or any Postgres
   code. Its only outbound writes go through the voidbase HTTP API. (Enforced by
   convention + a test that greps the package for `psycopg`/`db.conn`.)
2. **No arbitrary shell.** A queue job today is `python run_experiment.py` + a
   self-contained `config` (jsonb via `EXPERIMENT_CONFIG`). Voidrunner executes
   **only** the pinned `run_experiment.py` in a donor-reviewed repo at a pinned
   commit. Any job whose command isn't that is **refused**, not run. This is what
   makes "let strangers run jobs" not mean "RCE on strangers' machines."
3. **Born unverified.** A reported run is always `verification='unverified'`. The
   champion still moves only through the confirm gate. A malicious donor can
   submit garbage results; they can never move the champion. Worst case is a
   junk row that loses its paired comparison — the same property the public
   trust model already relies on.

## The core: three discrete calls (also the AI-tool surface)

The runner core is **three pure-ish functions**, not one loop. This is
deliberate: a CLI wraps them as `once`/`loop`, and an **AI agent / MCP tool** can
call them as separate steps (claim a job, decide, run, report) — Voidrunner is
usable by a human donor *and* by an autonomous agent.

```python
# runner/core.py  — zero DB imports; only an http client + subprocess
claim(api, token, box)        -> job | None      # POST /claim
execute(job, repo, opts)      -> result          # local run_experiment.py
report(api, token, result)    -> None            # POST /runs
# helpers
register(api, handle)         -> token           # POST /register (one-time)
heartbeat(api, box_id)        -> None            # POST /box_heartbeat (exists)
release(api, token, job_id)   -> None            # POST /jobs/release (dry/abort)
```

- `claim` — send `{box:{label,gpu_class,fingerprint}, gpu_class_filter?}`; server
  ensures the donor's box row, runs the atomic `FOR UPDATE SKIP LOCKED` claim,
  returns `{id,thread,name,command,config,content_hash}` or `{job:null}`.
- `execute` — verify `command` is the pinned `run_experiment.py`; run it locally
  with `EXPERIMENT_CONFIG=<config>`; parse `final_val_loss` etc. from stdout
  (reuse `worker.py`'s regexes); heartbeat in a background thread for the run's
  life; save full stdout to a local log.
- `report` — `POST /runs` with `{queue_item_id, box, status, final_val_loss,
  final_train_loss, final_val_accuracy, seed, content_hash, command, config,
  eval_points?, log_tail?}`. Server writes the `runs` row (born unverified) +
  `eval_points`, closes the queue item, in one transaction.

## New API endpoints (server side, in `api/server.py`)

All writes require a bearer token (except localhost dev — see Auth). GET stays
public and unchanged.

| Endpoint | Does |
|---|---|
| `POST /register` | create a contributor (handle), return an opaque token **once**. Handle is claim-once (re-register is refused, so a token can't be silently rotated). No auth (it's how you get a token). |
| `POST /claim` | the atomic claim, moved server-side from `worker.py`'s `CLAIM_SQL`. Optional `gpu_class_filter` (only jobs this GPU can run) **and `thread`** (scope to one research thread — also what makes tests hermetic). Returns `{box_id, job}` or `{box_id, job:null}`. |
| `POST /runs` | report a finished run: insert `runs` (contributor_id from token, box_id echoed from /claim and **verified to belong to the contributor**) + `eval_points`, close the queue item. Mirrors `worker.py:report()`. Pulls `seed` through from the config so the comparison engine can pair it. |
| `POST /release` | put a claimed job back to `needs-run` (dry-run validation, graceful Ctrl-C). Only the box-owning contributor may release. |
| `POST /box_heartbeat` | **already exists** — reuse as-is. |

## Auth design (minimal, built now)

Built now because it's cheap and it's the difference between "trusted boxes only"
and "publishable + donors get credit." Kept minimal so it's not a project.

- **Token = opaque random string**, shown to the donor **once** at `register`.
  The DB stores only its **SHA-256 hash** (`contributors.token_hash text unique`,
  one tiny migration). Plaintext token never persists server-side.
- **On each write:** `Authorization: Bearer <token>` → hash → look up
  `contributor_id`. Missing/unknown token on a write ⇒ `401`.
- **Localhost dev bypass:** a request from `127.0.0.1` with no token is treated as
  the existing `automation` contributor — so `worker.py`, the dashboard, and
  current daemons keep working with **zero changes**.
- **`VOIDBASE_REQUIRE_AUTH=1` for public serving.** The bypass keys off the client
  address, and behind a reverse proxy *every* request looks like `127.0.0.1` — so
  a public deployment MUST set this flag, which requires a valid token even from
  loopback and closes that hole. Default off (operator localhost unchanged).
- **Scope is coarse for v0:** any valid token may `claim`/`report`/write
  `ideas`. Fine-grained scopes and rate limits are a later layer (they belong
  with voidcredit), not a v0 blocker.

## Runner security model (donor's machine)

- Runs **only** the pinned `run_experiment.py` from a **donor-configured repo at
  a pinned commit** (the donor reviews that repo once, like any OSS they run).
- `content_hash` (commit + config + flags) is recomputed locally and sent; the
  server can later reject a mismatch vs. the queue item (voidcheck overlap).
- Hard `JOB_TIMEOUT`; full stdout saved locally for the donor to inspect.
- Outbound network needed: voidbase API + HuggingFace (dataset). Nothing inbound.

## Build phases

1. ✅ **API: auth + endpoints.** Migration `0009_contributor_token.sql`
   (`contributors.token_hash` + partial unique index). Added `/register`,
   `/claim`, `/runs`, `/release` to `api/server.py` with the bearer-token check,
   localhost bypass, and `VOIDBASE_REQUIRE_AUTH` for public mode. Verified by
   `tests/test_voidrunner_api.py` (claim atomicity, unverified birth, attribution,
   401 on bad token, cross-box-attribution rejected, release).
2. ✅ **`runner/` package.** `core.py` (the 3 calls, zero DB imports — enforced by
   an AST test), `cli.py` (`register`/`once`/`loop`, `--dry`), config via
   env/flags. Stdout parsers + heartbeat copied from `worker.py` (not imported —
   runner must not depend on `scripts/`).
3. ✅ **End-to-end.** `register → once --dry (released, no row) → once (reported)`
   against a stub repo: run lands `unverified`, seed carried, attributed to the
   registering contributor, queue item closed. (Manual smoke; fully cleaned up.)
4. ✅ **Harden + document.** AST no-DB-imports test, refusal of non-pinned
   commands (`RefusedCommand`), `runner/README.md` a stranger can follow.
   *(`pipx` entry point deferred to extraction — runs as `python -m runner`.)*
5. ⬜ **Extract** to its own repo once the endpoints stop changing.

### Deploy / follow-ups

- **Restart the live API (8787)** to expose the new endpoints — left undone on
  purpose (it's a running process the loop depends on).
- **Supabase schema drift:** `token_hash` was added to `db/schema.sql` (the
  production schema the API uses) but NOT `supabase/migrations/0001_init.sql`.
  When Supabase auth is adopted, `token_hash` maps onto `auth.users` instead — so
  the supabase variant is deliberately left alone for now.
- **`registry/` legacy tests** (`tests/test_registry.py`) fail independently of
  this work (a SQLite column-count bug in the store being retired at cutover).

## Open questions (decide as we hit them)

- **Repo distribution to donors.** Does the donor `git clone` the research repo
  themselves, or does Voidrunner fetch+pin it? Lean: Voidrunner clones a
  configured repo at the `content_hash` commit, so the job is reproducible and
  the donor reviews one URL.
- **GPU-class matching.** How does a donor advertise what their GPU can run, and
  how does `/claim` filter? v0: a free-text `gpu_class` + an optional filter;
  formalize later.
- **Token issuance UX.** `POST /register` for v0; later this is GitHub-OAuth via
  voidspark so a donor gets a token from the website, not a curl.
