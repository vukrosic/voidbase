#!/usr/bin/env python3
"""Apply a .sql file to the voidbase Neon database — code-driven schema edits.

The whole point: schema changes happen through git + this runner, never by
hand in the Neon SQL editor. Workflow:

    edit db/schema.sql           # version-controlled DDL
    python3 db/apply.py          # apply it (idempotent — uses IF NOT EXISTS)
    python3 db/apply.py path.sql # apply some other migration file

It runs the whole file in one transaction (psycopg autocommit off), so a
failure rolls the file back instead of leaving a half-applied schema. After
applying, it prints the table inventory so you can see the result without
opening a browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.conn import connect  # noqa: E402

DEFAULT_SQL = Path(__file__).resolve().parent / "schema.sql"


def main() -> int:
    sql_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SQL
    if not sql_path.exists():
        print(f"no such sql file: {sql_path}", file=sys.stderr)
        return 1
    sql = sql_path.read_text()
    print(f"applying {sql_path}  ({len(sql)} chars)")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)  # whole file, one transaction
        conn.commit()
        # report what's there now
        with conn.cursor() as cur:
            cur.execute(
                "select table_name from information_schema.tables "
                "where table_schema='public' order by table_name"
            )
            tables = [r[0] for r in cur.fetchall()]
    print(f"ok — {len(tables)} tables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
