"""Integration test for the Voidmind write protocol (api/server.py).

Exercises the two endpoints a token donor uses — POST /ideas and POST
/queue_items — over real HTTP against a running API, and asserts the properties
that make open idea-donation safe:

  * an enqueued job is born 'needs-run' + unclaimed (low-trust proposal, never a
    result that could move the champion);
  * the SERVER owns the dedup key — re-posting the same resolved config returns
    {deduped: true} with no second row, and a config already present as a run is
    likewise deduped;
  * a malformed config is rejected (400) before it can poison the queue;
  * an idea is attributed to the registering contributor;
  * a bad bearer token is rejected (401).

Self-cleaning: seeds its own throwaway thread, deletes everything (cascade) in
tearDown. Needs a live Postgres-backed API (VOIDMIND_TEST_API, default
http://127.0.0.1:8799); SKIPS if the server or DB is absent.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voidconfig  # noqa: E402

API = os.environ.get("VOIDMIND_TEST_API", "http://127.0.0.1:8799").rstrip("/")


def _post(path: str, body: dict, token: str | None = None):
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
class VoidmindApiTest(unittest.TestCase):
    def setUp(self):
        self.tag = uuid.uuid4().hex[:8]
        self.thread = f"vm-test-{self.tag}"
        self.handle = f"vmtest-{self.tag}"
        self.contributor_id = None
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "insert into threads (name, hypothesis, status) "
                "values (%s, 'voidmind api test', 'active')", (self.thread,))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("delete from threads where name = %s", (self.thread,))
            if self.contributor_id:
                cur.execute("delete from ideas where proposed_by = %s", (self.contributor_id,))
                cur.execute("delete from contributors where id = %s", (self.contributor_id,))
            conn.commit()
        finally:
            conn.close()

    def _register(self):
        _, reg = _post("/register", {"handle": self.handle})
        self.contributor_id = reg["contributor_id"]
        return reg["token"]

    def _config(self, lever: str, fields: dict):
        return voidconfig.resolve_config(
            "configs.llm_config.Tiny1M3MAlibiConfig", {}, fields, 42, lever)

    def test_enqueue_is_low_trust_and_attributed(self):
        token = self._register()
        cfg = self._config("vm-lever", {"use_vm_test": True})
        code, out = _post("/queue_items", {"thread": self.thread, "config": cfg}, token=token)
        self.assertEqual(code, 200, out)
        self.assertFalse(out.get("deduped"), out)
        self.assertEqual(out["status"], "needs-run")
        qid = out["queue_item_id"]
        # The server's hash must equal what voidconfig computes locally.
        self.assertEqual(out["content_hash"],
                         voidconfig.content_hash(cfg["env"], cfg["fields"]))
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("select status, claimed_by_box, thread_name from queue_items "
                        "where id = %s", (qid,))
            status, box, thr = cur.fetchone()
            self.assertEqual(status, "needs-run")
            self.assertIsNone(box)            # unclaimed
            self.assertEqual(thr, self.thread)
        finally:
            conn.close()

    def test_server_dedups_repeat_config(self):
        token = self._register()
        cfg = self._config("dup-lever", {"use_dup": True})
        code1, out1 = _post("/queue_items", {"thread": self.thread, "config": cfg}, token=token)
        self.assertEqual(code1, 200, out1)
        self.assertFalse(out1.get("deduped"))
        # Same resolved config again → deduped, no second row.
        code2, out2 = _post("/queue_items", {"thread": self.thread, "config": cfg}, token=token)
        self.assertEqual(code2, 200, out2)
        self.assertTrue(out2.get("deduped"), out2)
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("select count(*) from queue_items where thread_name = %s "
                        "and content_hash = %s",
                        (self.thread, out1["content_hash"]))
            self.assertEqual(cur.fetchone()[0], 1)  # exactly one, not two
        finally:
            conn.close()

    def test_malformed_config_rejected(self):
        token = self._register()
        code, out = _post("/queue_items",
                          {"thread": self.thread, "config": {"env": {}, "fields": {}}},
                          token=token)
        self.assertEqual(code, 400, out)  # no config_class

    def test_enqueue_unknown_thread_rejected(self):
        token = self._register()
        cfg = self._config("x", {"a": True})
        code, out = _post("/queue_items",
                          {"thread": f"no-such-{self.tag}", "config": cfg}, token=token)
        self.assertEqual(code, 400, out)
        self.assertIn("thread", out.get("error", "").lower())

    def test_post_idea_attributed(self):
        token = self._register()
        code, out = _post("/ideas",
                          {"title": "a structural idea", "explanation": "why"}, token=token)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["status"], "proposed")
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("select proposed_by from ideas where id = %s", (out["id"],))
            self.assertEqual(str(cur.fetchone()[0]), str(self.contributor_id))
        finally:
            conn.close()

    def test_invalid_token_rejected(self):
        cfg = self._config("x", {"a": True})
        code, out = _post("/queue_items", {"thread": self.thread, "config": cfg},
                          token="not-a-real-token")
        self.assertEqual(code, 401, out)


if __name__ == "__main__":
    unittest.main()
