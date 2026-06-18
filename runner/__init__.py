"""Voidrunner — the voidbase compute-donor client.

A standalone client a contributor installs on the machine that has the GPU. It
claims a job from voidbase, runs it locally, and reports the result back —
speaking ONLY the voidbase HTTP API, never touching the database.

The public surface is three discrete calls — claim → execute → report (plus
register / heartbeat / release helpers) — so a human `voidrunner loop` and an
autonomous agent can both drive it. See docs/VOIDRUNNER.md.

HARD RULE: nothing in this package may import the DB layer (db.conn, psycopg).
A donor's machine holds no DB creds. tests/test_runner_no_db_imports.py enforces it.
"""
from runner.core import (  # noqa: F401
    DEFAULT_API,
    claim,
    execute,
    heartbeat,
    local_box,
    register,
    release,
    report,
)

__all__ = [
    "DEFAULT_API", "register", "claim", "execute", "report", "release",
    "heartbeat", "local_box",
]
