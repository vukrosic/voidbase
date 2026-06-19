"""Tests for the api/backend.py Postgres connection pool — no live DB.

The pool is the platform's multi-writer scalability fix: concurrent requests must
run on DISTINCT connections (not serialize on one), the pool must stay BOUNDED
(so a thread swarm can't exhaust Neon), and a dropped connection must self-heal
with one reconnect. These are proven here against a fake psycopg.connect, so they
run anywhere with no network.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import psycopg  # noqa: E402 — real package (3.x); we patch .connect, reuse .OperationalError
import backend  # noqa: E402 — api/backend.py


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, params))
        # Simulate a connection that drops on its first use (reconnect path).
        if self.conn.fail_first and not self.conn._failed_once:
            self.conn._failed_once = True
            raise psycopg.OperationalError("simulated dropped connection")
        # Concurrency probe: if every query is forced to rendezvous at a barrier,
        # the call only completes when `parties` of them run AT ONCE — a serialized
        # path can never assemble the barrier and times out.
        if self.conn.barrier is not None:
            self.conn.barrier.wait()
        if sql.strip().lower().startswith("select"):
            self.description = [("x",)]
            self._rows = [{"x": 1}]
        else:
            self.description = None
            self._rows = []

    def executemany(self, sql, seq):
        self.conn.many.append((sql, list(seq)))

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, barrier=None, fail_first=False):
        self.closed = False
        self.executed: list = []
        self.many: list = []
        self.barrier = barrier
        self.fail_first = fail_first
        self._failed_once = False

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class PoolTestBase(unittest.TestCase):
    POOL_SIZE = 4

    def setUp(self):
        self._orig = (backend.PG_URL, backend._POOL_SIZE, backend._POOL_TIMEOUT,
                      backend._pool, psycopg.connect)
        backend.PG_URL = "postgresql://fake/db"
        backend._POOL_SIZE = self.POOL_SIZE
        backend._POOL_TIMEOUT = 5
        backend._pool = None  # force a fresh pool sized to POOL_SIZE
        self.created: list[FakeConn] = []

    def tearDown(self):
        (backend.PG_URL, backend._POOL_SIZE, backend._POOL_TIMEOUT,
         backend._pool, psycopg.connect) = self._orig

    def _install(self, factory):
        """Point psycopg.connect at a factory that records every conn it makes."""
        def fake_connect(url, autocommit=False):
            c = factory()
            self.created.append(c)
            return c
        psycopg.connect = fake_connect


class PoolConcurrencyTest(PoolTestBase):
    def test_requests_run_concurrently_on_distinct_connections(self):
        # A barrier that only releases when all POOL_SIZE queries are in flight at
        # once. The old single-connection-under-lock design would deadlock here.
        barrier = threading.Barrier(self.POOL_SIZE, timeout=5)
        self._install(lambda: FakeConn(barrier=barrier))

        errors: list = []

        def worker():
            try:
                backend._pg_rows("select 1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(self.POOL_SIZE)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [],
                         f"queries serialized (barrier never assembled): {errors}")
        # One distinct connection per concurrent slot.
        self.assertEqual(len(self.created), self.POOL_SIZE)


class PoolBoundsTest(PoolTestBase):
    def test_pool_never_opens_more_than_its_size(self):
        self._install(FakeConn)
        for _ in range(50):
            self.assertEqual(backend._pg_rows("select 1"), [{"x": 1}])
        # 50 sequential queries reuse the slots' connections — never more than the
        # bound, however much traffic flows.
        self.assertLessEqual(len(self.created), self.POOL_SIZE)

    def test_exhausted_pool_raises_backpressure_not_hang(self):
        # Drain every slot, then a borrow must fail fast with a clear error.
        backend._POOL_TIMEOUT = 0.1
        pool = backend._get_pool()
        held = [pool.get() for _ in range(self.POOL_SIZE)]
        try:
            with self.assertRaises(RuntimeError) as ctx:
                backend._borrow()
            self.assertIn("pool exhausted", str(ctx.exception))
        finally:
            for c in held:
                pool.put(c)


class PoolReconnectTest(PoolTestBase):
    def test_reconnects_once_on_dropped_connection(self):
        seq = iter([FakeConn(fail_first=True), FakeConn()])
        self._install(lambda: next(seq))

        out = backend._pg_rows("select 1")

        self.assertEqual(out, [{"x": 1}])              # succeeded after retry
        self.assertEqual(len(self.created), 2)         # opened a fresh connection
        self.assertTrue(self.created[0].closed)        # dropped one was closed


class ExecuteManyTest(PoolTestBase):
    def test_batches_all_rows_in_one_call(self):
        self._install(FakeConn)
        params = [(f"r{i}", i, None, 1.0, None, None, None, None) for i in range(200)]
        backend._pg_executemany("insert into eval_points values (...)", params)
        self.assertEqual(len(self.created), 1)         # one connection, one trip
        self.assertEqual(len(self.created[0].many), 1)
        self.assertEqual(self.created[0].many[0][1], params)

    def test_empty_sequence_is_a_noop(self):
        self._install(FakeConn)
        backend._pg_executemany("insert ...", [])
        self.assertEqual(self.created, [])             # never even borrowed a slot


if __name__ == "__main__":
    unittest.main()
