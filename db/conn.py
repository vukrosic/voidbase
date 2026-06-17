"""voidbase DB connection helper — the single place that resolves DATABASE_URL.

Resolution order (first hit wins):
  1. DATABASE_URL in the environment
  2. DATABASE_URL in voidbase/.env (gitignored; written by neonctl)

Everything that talks to Neon (the API server, the schema runner, the
importer) goes through here so there's exactly one source of truth for the
connection string and nobody hard-codes it.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def database_url(pooled: bool = False) -> str | None:
    """The Neon connection string, or None if not configured.

    pooled=True prefers the PgBouncer endpoint (DATABASE_URL_POOLED) — use it
    for the API server's many short web requests; the direct URL is better for
    migrations/bulk loads.
    """
    keys = ("DATABASE_URL_POOLED", "DATABASE_URL") if pooled else ("DATABASE_URL",)
    env_file = _load_env_file()
    for k in keys:
        v = os.environ.get(k) or env_file.get(k)
        if v:
            return v
    # fall back to the non-pooled url if pooled was asked for but absent
    return os.environ.get("DATABASE_URL") or env_file.get("DATABASE_URL")


def connect(pooled: bool = False):
    """Open a psycopg connection to Neon. Raises if psycopg/URL are missing."""
    import psycopg  # imported lazily so SQLite-only paths don't need it

    url = database_url(pooled=pooled)
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set — run: "
            "neonctl connection-string --project-id <id> > voidbase/.env"
        )
    return psycopg.connect(url)
