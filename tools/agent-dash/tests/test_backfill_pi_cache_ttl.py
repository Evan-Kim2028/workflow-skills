"""Tests for scripts/backfill_pi_cache_ttl.py on temp databases only.

Each test builds a fresh dash.sqlite through ``dash.store.Store`` (so the
schema matches the server's), inserts usage rows with raw SQL, and runs the
script's functions. No live state is read or written.
"""

import glob
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dash import pricing  # noqa: E402
from dash.store import Store  # noqa: E402

SPEC = importlib.util.spec_from_file_location("backfill_pi_cache_ttl", ROOT / "scripts" / "backfill_pi_cache_ttl.py")
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)

TS = 1_792_000_000.0
HAIKU_PREFIXED = "anthropic/claude-haiku-5-5"
HAIKU = "claude-haiku-5-5"
DEEPSEEK = "river/deepseek-ai/DeepSeek-V4.1-Flash"


def _row(tool, sid, mid, model, *, cw=0, five=None, one=None, est=None, ins=100, outs=200, cr=300):
    return (tool, sid, mid, TS, "run:%s" % sid, model, ins, outs, cr, cw, five, one, est)


INSERT = (
    "INSERT INTO usage_events(tool, session_id, message_id, ts, run_id, model, input_tokens, "
    "output_tokens, cache_read, cache_write, cache_write_5m, cache_write_1h, est_cost_usd) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dash.sqlite"
        Store(str(self.db)).close()
        con = sqlite3.connect(self.db)
        con.executemany(
            INSERT,
            [
                _row("pi", "s1", "m1", HAIKU_PREFIXED, cw=50_000),  # split NULL, writes > 0
                _row("pi", "s2", "m2", HAIKU, cw=0),  # no writes: priced, no split change
                _row("pi", "s3", "m3", HAIKU, cw=50_000, five=50_000, one=0),  # split present: kept
                _row("pi", "s4", "m4", DEEPSEEK, cw=0),  # not a Haiku row: never touched
                _row("claude", "s5", "m5", HAIKU, cw=50_000),  # Haiku under tool claude: not a target
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _est(self):
        con = sqlite3.connect(self.db)
        try:
            return {
                mid: (est, five, one)
                for mid, est, five, one in con.execute(
                    "SELECT message_id, est_cost_usd, cache_write_5m, cache_write_1h FROM usage_events"
                )
            }
        finally:
            con.close()

    def test_dry_run_writes_nothing_and_reports_the_targets(self):
        before = self._est()
        self.assertEqual(backfill.run(self.db, apply=False), 0)
        self.assertEqual(self._est(), before)

    def test_targets_select_only_pi_haiku_rows_without_a_cost(self):
        con = sqlite3.connect(self.db)
        try:
            rows = backfill.target_rows(con)
        finally:
            con.close()
        self.assertEqual(sorted(r["message_id"] for r in rows), ["m1", "m2", "m3"])
        by_id = {r["message_id"]: r for r in rows}
        self.assertTrue(by_id["m1"]["split_filled"])
        self.assertFalse(by_id["m2"]["split_filled"])
        self.assertFalse(by_id["m3"]["split_filled"])

    def test_apply_fills_the_split_and_prices_the_targets(self):
        self.assertEqual(backfill.run(self.db, apply=True), 0)
        after = self._est()
        self.assertEqual(after["m1"][1:], (50_000, 0))
        self.assertIsNotNone(after["m1"][0])
        # 100 in @0.1 + 200 out @0.5 + 300 read @0.01 + 50k writes @0.125 (prompt 50,400: base rates).
        self.assertAlmostEqual(after["m1"][0], 0.000010 + 0.000100 + 0.000003 + 0.00625, places=9)
        # No writes: priced with no split, so the split columns stay NULL.
        self.assertAlmostEqual(after["m2"][0], 0.00001 + 0.0001 + 0.000003, places=9)
        self.assertIsNone(after["m2"][1])
        self.assertEqual(after["m3"][1:], (50_000, 0))
        self.assertIsNotNone(after["m3"][0])
        # Rows outside the selector are untouched.
        self.assertIsNone(after["m4"][0])
        self.assertIsNone(after["m5"][0])

    def test_apply_matches_the_pricing_path_for_each_row(self):
        backfill.run(self.db, apply=True)
        after = self._est()
        table = pricing.catalog()
        expected = pricing.cost(HAIKU_PREFIXED, 100, 200, 300, 50_000, table, cache_write_5m=50_000, cache_write_1h=0)
        self.assertAlmostEqual(after["m1"][0], expected, places=9)
        self.assertAlmostEqual(after["m3"][0], expected, places=9)

    def test_second_apply_is_a_no_op(self):
        backfill.run(self.db, apply=True)
        once = self._est()
        self.assertEqual(backfill.run(self.db, apply=True), 0)
        self.assertEqual(self._est(), once)

    def test_undo_restores_the_prior_values(self):
        before = self._est()
        backfill.run(self.db, apply=True)
        undo_files = glob.glob(str(self.db.with_name("backfill-pi-cache-ttl-*.undo.json")))
        self.assertEqual(len(undo_files), 1)
        self.assertEqual(backfill.undo(self.db, Path(undo_files[0])), 0)
        self.assertEqual(self._est(), before)

    def test_zero_targets_with_unpriced_haiku_rows_exits_3_on_dry_run(self):
        # Only the tool-claude Haiku row is unpriced: the selector finds nothing, so the dry run flags it.
        con = sqlite3.connect(self.db)
        con.execute("UPDATE usage_events SET est_cost_usd=0.5 WHERE tool='pi'")
        con.commit()
        con.close()
        self.assertEqual(backfill.run(self.db, apply=False), backfill.EXIT_DRY_RUN_EMPTY)

    def test_zero_targets_and_no_unpriced_haiku_exits_0(self):
        backfill.run(self.db, apply=True)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE usage_events SET est_cost_usd=0.1 WHERE tool='claude'")
        con.commit()
        con.close()
        self.assertEqual(backfill.run(self.db, apply=False), 0)

    def test_missing_database_is_bad_input(self):
        self.assertEqual(backfill.run(Path(self._tmp.name) / "nope.sqlite", apply=False), backfill.EXIT_BAD_INPUT)

    def test_default_db_follows_the_state_dir(self):
        old = os.environ.get("AGENT_DASH_STATE_DIR")
        os.environ["AGENT_DASH_STATE_DIR"] = self._tmp.name
        try:
            self.assertEqual(backfill.default_db(), Path(self._tmp.name) / "dash.sqlite")
        finally:
            if old is None:
                os.environ.pop("AGENT_DASH_STATE_DIR", None)
            else:
                os.environ["AGENT_DASH_STATE_DIR"] = old


if __name__ == "__main__":
    unittest.main()
