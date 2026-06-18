#!/usr/bin/env python3
"""voidbase local API — stdlib HTTP server over the registry.

The thin read/write API that localhost voidspark calls. This module is ONLY the
HTTP dispatcher: it parses the request, authenticates writes, and routes to the
endpoint builders. The work lives in three sibling modules so none becomes a
god-file:

  * backend.py — backend-agnostic DB plumbing (rows / _pg_rows / _pg_exec) and
    the resolved runtime config (PG_URL, BACKEND, REQUIRE_AUTH, …). Picks Postgres
    (Neon) when DATABASE_URL is set, else the legacy SQLite store — same JSON
    shapes either way, so the front end never changes on cutover.
  * reads.py   — every GET endpoint builder (+ the composite /dashboard cache).
  * writes.py  — every mutating endpoint + the bearer-token auth that gates them.

  python3 api/server.py            # serves on http://localhost:8787
  VOIDBASE_PORT=9000 python3 api/server.py

Endpoints (GET unless noted, JSON):
  /health        liveness + row counts + per-box health + which backend is live
  /runs          training runs (verification + has_eval)
  /threads       hypotheses
  /comparisons   paired deltas (is_paired: real on PG, false on SQLite)
  /champions     champion history
  /ideas         backlog
  /queue         job queue
  /eval?run_id=  per-step learning curve for one run
  /leaderboard   contributors ranked by the credit policy (voidcredit; PG-only)
  /contributor?handle=  one contributor's card (voidcredit; PG-only)
  /lineage?run=  thread→queue_item→run→champion chain (voidcredit; PG-only)
  /gate?scope=   confirm-gate status (champion + candidate field + the blocker)
  /dashboard?scope=  composite (health+champions+gate+runs+comparisons+activity)

  POST /threads        author / update a research thread (dashboard)
  POST /box_heartbeat  {box_id, gpu_class?} liveness ping from a worker (PG-only)
  POST /register       mint a contributor + bearer token (no auth; PG-only)
  POST /claim /runs /release   Voidrunner compute-donor protocol (token; PG-only)
  POST /ideas /queue_items     Voidmind token-donor protocol (token; PG-only)

NOTE on query-param ids: run ids contain '+' (e.g. 'use_a+use_b-...'). In a query
string '+' is form-decoded to a space, so a RAW url 404s — callers MUST percent-
encode the value ('+' -> '%2B'). The voidspark proxy does this (encodeURIComponent);
a hand-rolled curl/script to /lineage?run= or /eval?run_id= must too.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from backend import BACKEND, DB_PATH, PG_URL, REQUIRE_AUTH, rows
from reads import (
    activity, comparisons, contributor, dashboard, eval_points, gate, health,
    leaderboard, lineage, runs, thread_goal, threads, threads_public,
    warm_dashboard,
)
from writes import (
    automation_contributor_id, box_heartbeat, claim_job, claim_thread,
    contributor_for_token, create_idea, enqueue_item, register, release_job,
    release_thread, report_run, upsert_thread,
)


# Plain-collection GET routes: path -> zero-arg builder. The query-param routes
# (/eval, /gate, /dashboard, …) are dispatched explicitly in do_GET below.
ROUTES = {
    "/health": health,
    "/activity": activity,
    "/runs": runs,
    "/threads": threads,
    "/comparisons": comparisons,
    "/champions": lambda: rows("select * from champions order by promoted_at desc"),
    "/ideas": lambda: rows("select * from ideas order by created_at desc"),
    "/queue": lambda: rows("select * from queue_items order by created_at desc"),
    "/leaderboard": leaderboard,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self):
        """Resolve the writer's contributor id for the token-gated endpoints
        (claim / runs / release). Returns the id, or sends a 401 and returns None.

          * `Authorization: Bearer <token>` → the matching contributor.
          * No token, from localhost → the 'automation' contributor (the dev
            bypass that keeps worker.py / the dashboard working unchanged).
          * No token, from a remote client → 401.

        The localhost bypass is what lets the operator's own tools skip auth while
        a real donor over the network must present a token."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token:
            cid = contributor_for_token(token)
            if not cid:
                self._send(401, {"error": "invalid bearer token"})
                return None
            return cid
        # No token: the loopback bypass, unless this is a public deployment that
        # has turned it off (REQUIRE_AUTH) — see the REQUIRE_AUTH comment for why
        # a proxied deployment must, or it hands the bypass to every client.
        if not REQUIRE_AUTH and self.client_address[0] in ("127.0.0.1", "::1", "localhost"):
            return automation_contributor_id()
        self._send(401, {"error": "missing bearer token"})
        return None

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """The platform's writes. Two tiers:

          * Operator/dashboard (localhost, no auth, unchanged):
            - author / update a research thread   (/threads, default)
            - claim / release a thread            (/threads, action=claim|release)
            - record a box heartbeat              (/box_heartbeat)
          * Voidrunner compute-donor protocol (bearer token; localhost bypass):
            - mint a contributor + token          (/register, no auth)
            - claim the next job                  (/claim)
            - report a finished run               (/runs)
            - release a claimed job               (/release)
          * Voidmind token-donor protocol (bearer token; localhost bypass):
            - propose a candidate idea            (/ideas)
            - enqueue a runnable experiment       (/queue_items)

        Dispatch is by URL path; a client may instead POST the base URL with a
        {resource: '...'} field in the body (the worker sends {resource:
        'box_heartbeat', ...})."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"bad json: {e}"})
            return
        resource = path.lstrip("/") or str(body.get("resource") or "")
        # Endpoints that require a contributor identity (token, or localhost bypass).
        #   claim/runs/release — Voidrunner (compute);  ideas/queue_items — Voidmind (proposals)
        authed = {"claim", "runs", "release", "ideas", "queue_items"}
        writable = {"threads", "box_heartbeat", "register"} | authed
        if resource not in writable:
            self._send(404, {"error": "not found", "writable": sorted(writable)})
            return
        if not PG_URL:
            self._send(501, {"error": f"{resource} writes require the Postgres backend"})
            return
        try:
            if resource in authed:
                cid = self._authenticate()
                if cid is None:
                    return  # 401 already sent
                handler = {"claim": claim_job, "runs": report_run,
                           "release": release_job, "ideas": create_idea,
                           "queue_items": enqueue_item}[resource]
                self._send(200, handler(body, cid))
            elif resource == "register":
                self._send(200, register(body))
            elif resource == "box_heartbeat":
                self._send(200, box_heartbeat(body))
            else:  # threads — author/update, or claim/release by action
                action = (body.get("action") or "").strip().lower()
                handler = {"claim": claim_thread, "release": release_thread}.get(
                    action, upsert_thread)
                self._send(200, handler(body))
        except ValueError as e:  # bad/insufficient input from the client
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/health"
        q = parse_qs(parsed.query)
        try:
            if path == "/eval":
                self._send(200, eval_points(q.get("run_id", [""])[0]))
                return
            if path == "/threads/public":
                status = q.get("status", ["active"])[0] or None
                unclaimed = q.get("unclaimed", ["false"])[0].lower() in ("1", "true", "yes")
                self._send(200, threads_public(status=status, unclaimed=unclaimed))
                return
            if path == "/threads/goal":
                self._send(200, thread_goal(q.get("name", [""])[0]))
                return
            if path == "/contributor":
                self._send(200, contributor(q.get("handle", [""])[0]))
                return
            if path == "/lineage":
                self._send(200, lineage(q.get("run", [""])[0]))
                return
            if path == "/gate":
                self._send(200, gate(q.get("scope", [""])[0]))
                return
            if path == "/dashboard":
                self._send(200, dashboard(q.get("scope", [""])[0]))
                return
            handler = ROUTES.get(path)
            if handler is None:
                routes = sorted([*ROUTES, "/eval?run_id=",
                                 "/threads/public?status=&unclaimed=", "/threads/goal?name=",
                                 "/contributor?handle=", "/lineage?run=", "/gate?scope=",
                                 "/dashboard?scope="])
                self._send(404, {"error": "not found", "routes": routes})
                return
            self._send(200, handler())
        except ValueError as e:  # e.g. unknown thread name — client-correctable
            self._send(404, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, *args) -> None:  # quiet
        pass


def main() -> None:
    port = int(os.environ.get("VOIDBASE_PORT", "8787"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    target = "neon postgres" if PG_URL else f"sqlite {DB_PATH}"
    print(f"voidbase api on http://127.0.0.1:{port}  (backend={BACKEND}, {target})")
    # Warm the default-scope dashboard cache off-thread so the first real request
    # is served from cache instead of paying the cold ~10-13s composite query.
    if PG_URL:
        warm_dashboard("tiny1m3m")
    srv.serve_forever()


if __name__ == "__main__":
    main()
