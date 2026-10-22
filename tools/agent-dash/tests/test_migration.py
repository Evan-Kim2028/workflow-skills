"""Old-database migration guard: the previous release's database must open.

Regression test for the outage where CREATE INDEX idx_runs_slice ran
before the ALTER TABLE adding runs.slice_of, so every restart on the
live database failed with OperationalError while all fresh-DB tests
passed. tests/fixtures/schema_prev.sql freezes the previous release's
schema; this test opens it with the current Store, asserts every new
column and index appears, proves old rows survive, and reopens to
prove idempotence.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash.store import Store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema_prev.sql"


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def indexes(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


class MigrationTest(unittest.TestCase):
    def open_prev(self, tmp):
        path = Path(tmp) / "prev.sqlite"
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(FIXTURE.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()
        return Store(path)

    def test_previous_schema_opens_and_gains_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_prev(tmp)
            try:
                self.assertIn("slice_of", columns(store._conn, "runs"))
                self.assertIn("ws_split", columns(store._conn, "usage_events"))
                names = indexes(store._conn)
                self.assertIn("idx_runs_slice", names)
                self.assertIn("idx_usage_session", names)
            finally:
                store.close()

    def test_previous_rows_survive_and_new_columns_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_prev(tmp)
            try:
                row = store._conn.execute(
                    "SELECT run_id, label FROM runs WHERE run_id='prev-run-1'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["label"], "ws1-1 old row")
                usage = store._conn.execute(
                    "SELECT input_tokens FROM usage_events WHERE message_id='prev-m-1'"
                ).fetchone()
                self.assertIsNotNone(usage)
                self.assertEqual(usage["input_tokens"], 100)
                store.upsert_run(
                    {
                        "run_id": "new-run-1",
                        "source": "claude",
                        "status": "running",
                        "slice_of": "prev-run-1",
                    }
                )
                store.add_usage(
                    {
                        "ts": 1200.0,
                        "session_id": "prev-s-1",
                        "run_id": "new-run-1",
                        "model": "claude-opus-4-6",
                        "tool": "claude",
                        "message_id": "new-m-1",
                        "input": 5,
                        "output": 5,
                        "ws_split": "2",
                    }
                )
            finally:
                store.close()

    def test_second_open_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.open_prev(tmp)
            try:
                before_runs = columns(first._conn, "runs")
                before_usage = columns(first._conn, "usage_events")
            finally:
                first.close()
            db = next(Path(tmp).glob("*.sqlite"))
            second = Store(db)
            try:
                self.assertEqual(columns(second._conn, "runs"), before_runs)
                self.assertEqual(
                    columns(second._conn, "usage_events"), before_usage
                )
                row = second._conn.execute(
                    "SELECT run_id FROM runs WHERE run_id='prev-run-1'"
                ).fetchone()
                self.assertIsNotNone(row)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
