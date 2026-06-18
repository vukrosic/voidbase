"""Integration test for the Voidrunner write protocol (api/server.py).

Exercises the four endpoints a compute donor uses — /register, /claim, /runs,
/release — over real HTTP against a running API, and asserts the integrity
properties that make donated compute safe:

  * a claim is atomic and returns the box id the runner echoes back;
  * a reported run is born verification='unverified' (a donor can never move the
    champion), is attributed to the registering contributor, and carries its seed
    (so the comparison engine can pair it);
  * a bad bearer token is rejected (401);
  * /release returns a claimed job to the queue.

Self-cleaning: it seeds its own throwaway thread + queue items and deletes
everything (cascade) in tearDown, so it never pollutes the live registry.

Needs a live Postgres-backed API. Point it at one with VOIDRUNNER_TEST_API
(default http://127.0.0.1:8799); the test SKIPS if the server or DB is absent.
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = os.environ.get("VOIDRUNNER_TEST_API", "http://127.0.0.1:8799").rstrip("/")


def _post(path: str, body: dict, token: str | None = None):
    """POST json, returning (status_code, parsed_body)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _server_up() -> bool:
    try:
        urllib.request.urlopen(f"{API}/health", timeout=5).read()
        return True
    except Exception:  # noqa: BLE001
        return False


try:
    from db.conn import connect, database_url
    _HAVE_DB = bool(database_url())
except Exception:  # noqa: BLE001
    _HAVE_DB = False


@unittest.skipUnless(_HAVE_DB, "no DATABASE_URL configured")
@unittest.skipUnless(_server_up(), f"voidbase API not reachable at {API}")
class VoidrunnerApiTest(unittest.TestCase):
    def setUp(self):
        self.tag = uuid.uuid4().hex[:8]
        self.thread = f"vr-test-{self.tag}"
        self.job_id = f"vrtest-{self.tag}-a"
        self.job_id2 = f"vrtest-{self.tag}-b"
        self.handle = f"vrtest-{self.tag}"
        self.contributor_id = None
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "insert into threads (name, hypothesis, status) "
                "values (%s, 'voidrunner api test', 'active')", (self.thread,))
            for jid in (self.job_id, self.job_id2):
                cur.execute(
                    "insert into queue_items (id, thread_name, name, command, status, "
                    "config, content_hash) values "
                    "(%s, %s, %s, 'python run_experiment.py', 'needs-run', %s, %s)",
                    (jid, self.thread, jid, json.dumps({"seed": 7, "lever": jid}),
                     f"hash-{jid}"))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = connect()
        try:
            cur = conn.cursor()
            # Cascade: deleting the thread removes its queue_items, runs, eval_points.
            cur.execute("delete from threads where name = %s", (self.thread,))
            if self.contributor_id:
                cur.execute("delete from boxes where contributor_id = %s", (self.contributor_id,))
                cur.execute("delete from contributors where id = %s", (self.contributor_id,))
            conn.commit()
        finally:
            conn.close()

    def _box(self):
        return {"label": f"vr-test-box-{self.tag}", "gpu_class": "test",
                "fingerprint": f"vr-test-fp-{self.tag}"}

    def test_register_returns_token_once(self):
        code, body = _post("/register", {"handle": self.handle})
        self.assertEqual(code, 200, body)
        self.assertIn("token", body)
        self.assertTrue(body["token"])
        self.contributor_id = body["contributor_id"]
        # Re-registering the same handle is refused (claim-once, no silent rotate).
        code2, body2 = _post("/register", {"handle": self.handle})
        self.assertEqual(code2, 400, body2)

    def test_full_claim_report_cycle(self):
        _, reg = _post("/register", {"handle": self.handle})
        token, self.contributor_id = reg["token"], reg["contributor_id"]

        # Claim — should hand back one of our two throwaway jobs + the box id.
        code, claim = _post("/claim", {"box": self._box(), "thread": self.thread}, token=token)
        self.assertEqual(code, 200, claim)
        self.assertIsNotNone(claim.get("job"), claim)
        self.assertIn(claim["job"]["id"], (self.job_id, self.job_id2))
        self.assertEqual(claim["job"]["config"].get("seed"), 7)
        box_id = claim["box_id"]
        claimed = claim["job"]["id"]

        # Report a finished run.
        code, rep = _post("/runs", {
            "queue_item_id": claimed, "box_id": box_id, "status": "done",
            "final_val_loss": 6.5, "config": claim["job"]["config"],
            "content_hash": claim["job"]["content_hash"],
            "eval_points": [{"step": 100, "val_loss": 6.5}],
        }, token=token)
        self.assertEqual(code, 200, rep)
        self.assertEqual(rep["verification"], "unverified")
        self.assertEqual(rep["seed"], 7)
        run_id = rep["run_id"]

        # Verify the DB landed the integrity properties.
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("select contributor_id, box_id, seed, verification, status "
                        "from runs where id = %s", (run_id,))
            row = cur.fetchone()
            self.assertIsNotNone(row, "run row missing")
            cid, rbox, seed, verification, status = row
            self.assertEqual(str(cid), str(self.contributor_id))
            self.assertEqual(str(rbox), str(box_id))
            self.assertEqual(seed, 7)
            self.assertEqual(verification, "unverified")
            self.assertEqual(status, "done")
            cur.execute("select status from queue_items where id = %s", (claimed,))
            self.assertEqual(cur.fetchone()[0], "done")
            cur.execute("select count(*) from eval_points where run_id = %s", (run_id,))
            self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_invalid_token_rejected(self):
        code, body = _post("/claim", {"box": self._box()}, token="not-a-real-token")
        self.assertEqual(code, 401, body)

    def test_cannot_report_to_a_box_you_dont_own(self):
        # A donor must not be able to attribute a run to someone else's box —
        # per-box baselines depend on box_id being honest.
        _, reg = _post("/register", {"handle": self.handle})
        token, self.contributor_id = reg["token"], reg["contributor_id"]
        _, claim = _post("/claim", {"box": self._box(), "thread": self.thread}, token=token)
        code, rep = _post("/runs", {
            "queue_item_id": claim["job"]["id"],
            "box_id": str(uuid.uuid4()),  # a box this contributor does not own
            "status": "done", "final_val_loss": 6.5,
        }, token=token)
        self.assertEqual(code, 400, rep)
        self.assertIn("box", rep.get("error", "").lower())

    def test_release_returns_job_to_queue(self):
        _, reg = _post("/register", {"handle": self.handle})
        token, self.contributor_id = reg["token"], reg["contributor_id"]
        _, claim = _post("/claim", {"box": self._box(), "thread": self.thread}, token=token)
        claimed = claim["job"]["id"]
        code, rel = _post("/release", {"queue_item_id": claimed}, token=token)
        self.assertEqual(code, 200, rel)
        self.assertEqual(rel["released"], claimed)
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("select status, claimed_by_box from queue_items where id = %s",
                        (claimed,))
            status, box = cur.fetchone()
            self.assertEqual(status, "needs-run")
            self.assertIsNone(box)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
