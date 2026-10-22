#!/usr/bin/env python3
"""Dump the current fresh-DB schema for the migration-guard fixture.

Creates a throwaway Store, dumps its CREATE TABLE / CREATE INDEX
statements (no data), and writes tests/fixtures/schema_prev.sql with
a header. Re-run at each release to refresh the "previous" baseline
that tests/test_migration.py opens with the then-current code.

Usage: python3 scripts/schema_snapshot.py [--out PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "schema_prev.sql"

HEADER = """-- Frozen schema snapshot (see scripts/schema_snapshot.py).
--
-- Regenerate at each release; tests/test_migration.py opens this with
-- the current Store and asserts every new column and index is added.
"""


def dump_schema() -> str:
    sys.path.insert(0, str(ROOT))
    from dash.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "dash.sqlite")
        try:
            rows = store._conn.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
                " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
            ).fetchall()
            statements = [row["sql"].rstrip().rstrip(";") + ";" for row in rows]
        finally:
            store.close()
    return "\n".join(statements) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    body = dump_schema()
    Path(args.out).write_text(HEADER + body, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
