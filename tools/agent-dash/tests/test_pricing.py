import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.pricing import (
    catalog_sig,
    cost,
    family,
    health_block,
    load,
    reprice,
    reprice_null_costs,
    status,
)
from dash.store import Store

GROK = "grok-4.7[context=500k,reasoning_effort=high,fast=false]"
DEMO = [
    {
        "model": "demo",
        "input": 2,
        "output": 3,
        "cache_read": Decimal("0.5"),
        "cache_write_5m": 4,
        "cache_write_1h": 8,
        "reasoning": None,
        "input_includes_cache_read": False,
        "long_context": {
            "threshold": 100,
            "comparison": "gt",
            "counts": ["input", "cache_read", "cache_write"],
            "input": 9,
            "output": 10,
            "cache_read": 1,
            "cache_write_5m": 11,
            "cache_write_1h": 12,
        },
        "billing_mode": "api",
        "confidence": "published",
        "provider": "demo",
    }
]


class CostMathTest(unittest.TestCase):
    def test_token_classes_sum(self):
        row = dict(DEMO[0])
        row["long_context"] = None
        priced = cost(
            "demo",
            1_000_000,
            1_000_000,
            1_000_000,
            2_000_000,
            table=[row],
            cache_write_5m=1_000_000,
            cache_write_1h=1_000_000,
        )
        self.assertAlmostEqual(priced, 2 + 3 + 0.5 + 4 + 8)

    def test_cache_split_and_missing_split(self):
        split = cost(
            "demo",
            0,
            0,
            0,
            10,
            table=DEMO,
            cache_write_5m=4,
            cache_write_1h=6,
        )
        self.assertAlmostEqual(split, (4 * 4 + 6 * 8) / 1_000_000)
        self.assertIsNone(cost("demo", 0, 0, 0, 10, table=DEMO))
        self.assertIsNone(
            cost("demo", 0, 0, 0, 10, table=DEMO, cache_write_5m=4, cache_write_1h=5)
        )
        self.assertEqual(cost("demo", 0, 0, 0, 0, table=DEMO), 0.0)

    def test_equal_write_rates_price_the_total(self):
        row = dict(DEMO[0])
        row["cache_write_5m"] = 4
        row["cache_write_1h"] = 4
        row["long_context"] = None
        priced = cost("demo", 0, 0, 0, 1_000_000, table=[row])
        self.assertAlmostEqual(priced, 4)

    def test_long_context_threshold_is_per_request(self):
        under = cost("demo", 50, 0, 50, 0, table=DEMO)
        over = cost("demo", 50, 0, 51, 0, table=DEMO)
        self.assertAlmostEqual(under, float((50 * 2 + 50 * Decimal("0.5")) / 1_000_000))
        self.assertAlmostEqual(over, (50 * 9 + 51 * 1) / 1_000_000)
        self.assertAlmostEqual(cost("demo", 100, 0, 0, 0, table=DEMO), 200 / 1_000_000)
        self.assertAlmostEqual(cost("demo", 101, 0, 0, 0, table=DEMO), 909 / 1_000_000)

    def test_input_that_already_includes_cache_read(self):
        row = dict(DEMO[0])
        row["long_context"] = None
        row["input_includes_cache_read"] = True
        row["input"] = 10
        row["cache_read"] = 1
        row["cache_write_5m"] = 0
        row["cache_write_1h"] = 0
        priced = cost("demo", 1000, 0, 400, 0, table=[row])
        self.assertAlmostEqual(priced, (600 * 10 + 400 * 1) / 1_000_000)
        self.assertIsNone(cost("demo", 100, 0, 400, 0, table=[row]))

    def test_reasoning_only_when_billed_separately(self):
        self.assertEqual(cost("demo", 0, 0, 0, 0, table=DEMO, reasoning=50), 0.0)
        row = dict(DEMO[0])
        row["reasoning"] = 7
        row["long_context"] = None
        self.assertAlmostEqual(
            cost("demo", 0, 0, 0, 0, table=[row], reasoning=1_000_000),
            7,
        )
        self.assertIsNone(cost("demo", 0, 0, 0, 0, table=[row], reasoning=None))

    def test_unknown_is_null_not_zero(self):
        self.assertIsNone(cost("muse-wonder-full-1.0", 0, 0, 0, 0))
        self.assertIsNone(cost("<synthetic>", 10, 10, 10, 10))
        self.assertIsNone(cost("demo", None, 0, 0, 0, table=DEMO))
        self.assertIsNone(cost("", 1, 1, 1, 1, table=DEMO))
        self.assertIsNone(cost(None, 1, 1, 1, 1, table=DEMO))


class CatalogTest(unittest.TestCase):
    def test_published_rates(self):
        rows = {row["model"]: row for row in load()}
        opus = rows["claude-opus-5-5"]
        self.assertEqual(opus["input"], 4)
        self.assertEqual(opus["output"], 20)
        self.assertEqual(opus["cache_read"], Decimal("0.2"))
        self.assertEqual(opus["cache_write_5m"], 5)
        self.assertEqual(opus["cache_write_1h"], 8)
        self.assertIsNone(opus["reasoning"])
        self.assertEqual(opus["billing_mode"], "api")
        self.assertEqual(opus["confidence"], "published")
        self.assertIsNone(opus["long_context"])

        haiku = rows["claude-haiku-5-5"]
        self.assertEqual(haiku["input"], Decimal("0.1"))
        self.assertEqual(haiku["long_context"]["threshold"], 100000)
        self.assertEqual(haiku["long_context"]["comparison"], "gt")
        self.assertEqual(haiku["long_context"]["input"], Decimal("0.5"))
        self.assertEqual(haiku["long_context"]["cache_write_5m"], Decimal("0.625"))

        flash = rows["glm-5.3-flash"]
        self.assertEqual(flash["input"], Decimal("0.15"))
        self.assertEqual(flash["cache_read"], Decimal("0.03"))
        self.assertEqual(flash["output"], Decimal("0.5"))
        self.assertEqual(flash["cache_write_5m"], 0)
        self.assertFalse(flash["input_includes_cache_read"])

        grok = rows[GROK]
        self.assertEqual(grok["input"], 4)
        self.assertEqual(grok["output"], 12)
        self.assertEqual(grok["cache_read"], 1)
        self.assertIsNone(grok["cache_write_5m"])
        self.assertEqual(grok["billing_mode"], "plan-included")
        self.assertEqual(grok["confidence"], "inferred")
        xhigh = rows["grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]"]
        self.assertEqual(xhigh["input"], grok["input"])
        self.assertEqual(xhigh["output"], grok["output"])
        self.assertEqual(xhigh["cache_read"], grok["cache_read"])
        self.assertEqual(xhigh["confidence"], "inferred")

        spark = rows["muse-spark-1.3-contributor"]
        self.assertEqual((float(spark["input"]), float(spark["output"]), float(spark["cache_read"])), (0.10, 0.20, 0.002))
        self.assertFalse(spark["input_includes_cache_read"])
        self.assertEqual(spark["confidence"], "published")
        self.assertEqual(rows["muse-wonder-1.0-contributor"]["confidence"], "inferred")

        for model in ("grok-4.7-high", "grok-4.7-xhigh"):
            bare = rows[model]
            self.assertEqual((bare["input"], bare["output"], bare["cache_read"]), (2, 6, Decimal("0.5")))
            self.assertEqual(bare["billing_mode"], "plan-included")
            self.assertEqual(bare["confidence"], "inferred")
            self.assertTrue(status(model)["priced"])

        for model in (
            "muse-wonder-full-1.0",
            "<synthetic>",
        ):
            self.assertIsNone(rows[model]["input"])
            self.assertEqual(rows[model]["confidence"], "unknown")
            self.assertFalse(status(model)["priced"])

    def test_composer_fast_rate_is_published(self):
        """Cursor publishes Composer 2.5 (Fast) per-million rates: 3/15/0.5,
        no cache-write rate. The row prices token traffic, never zeroes it."""
        rows = {row["model"]: row for row in load()}
        fast = rows["composer-2.5-fast"]
        self.assertEqual(fast["provider"], "cursor")
        self.assertEqual((fast["input"], fast["output"], fast["cache_read"]), (3, 15, Decimal("0.5")))
        self.assertIsNone(fast["cache_write_5m"])
        self.assertIsNone(fast["cache_write_1h"])
        self.assertEqual(fast["billing_mode"], "plan-included")
        self.assertEqual(fast["confidence"], "published")
        self.assertEqual(fast["source_url"], "https://cursor.com/docs/models-and-pricing")
        self.assertTrue(status("composer-2.5-fast")["priced"])
        self.assertAlmostEqual(
            cost("composer-2.5-fast", 1_000_000, 1_000_000, 1_000_000, 0),
            3 + 15 + 0.5,
        )
        self.assertIsNone(cost("composer-2.5-fast", 1, 1, 0, 5))

    def test_unpublished_ids_stay_null(self):
        """muse-wonder-full-1.0 and <synthetic> have no published rate
        (Cursor lists Muse Wonder, not a Full variant, and no synthetic
        model), so the rows stay null and traffic stays unpriced."""
        rows = {row["model"]: row for row in load()}
        for model in ("muse-wonder-full-1.0", "<synthetic>"):
            self.assertIsNone(rows[model]["input"])
            self.assertIsNone(rows[model]["output"])
            self.assertFalse(status(model)["priced"])
            self.assertIsNone(cost(model, 10, 10, 10, 0))

    def test_family_is_unchanged(self):
        self.assertEqual(family("claude-opus-5-5"), "opus")
        self.assertEqual(family("claude-sonnet-5-5"), "sonnet")
        self.assertEqual(family("claude-haiku-5-5"), "haiku")
        self.assertIsNone(family("glm-5.3"))
        self.assertIsNone(family("muse-wonder-full-1.0"))
        self.assertIsNone(family(GROK))
        self.assertIsNone(family(None))

    def test_live_models_from_the_catalog(self):
        self.assertAlmostEqual(cost("claude-haiku-5-5", 100_000, 0, 0, 0), 0.01)
        self.assertAlmostEqual(
            cost("claude-haiku-5-5", 100_001, 0, 0, 0),
            float(Decimal(100_001) * Decimal("0.5") / Decimal(1_000_000)),
        )
        self.assertIsNone(cost("claude-opus-5-5", 1, 1, 0, 10))
        opus = cost(
            "claude-opus-5-5",
            2,
            4,
            0,
            10,
            cache_write_5m=3,
            cache_write_1h=7,
        )
        self.assertAlmostEqual(opus, (2 * 4 + 4 * 20 + 3 * 5 + 7 * 8) / 1_000_000)
        flash = cost("glm-5.3-flash", 600, 10, 400, 0)
        self.assertAlmostEqual(
            flash,
            float((600 * Decimal("0.15") + 400 * Decimal("0.03") + 10 * Decimal("0.5")) / 1_000_000),
        )
        self.assertAlmostEqual(cost(GROK, 1_000_000, 1_000_000, 1_000_000, 0), 17)
        self.assertIsNone(cost(GROK, 1, 1, 0, 5))
        info = status("claude-fable-5-1")
        self.assertTrue(info["priced"])
        self.assertEqual(info["billing_mode"], "api")
        self.assertEqual(info["confidence"], "published")

    def test_health_block(self):
        block = health_block(
            [
                {
                    "model": "claude-opus-5-5",
                    "rows": 2,
                    "input": 5,
                    "output": 6,
                    "cache_read": 7,
                    "cache_write": 8,
                    "reasoning": 1,
                }
            ]
        )
        by_model = {row["model"]: row for row in block["models"]}
        self.assertEqual(by_model["claude-opus-5-5"]["rows"], 2)
        self.assertEqual(by_model["claude-opus-5-5"]["tokens"]["cache_read"], 7)
        self.assertTrue(by_model["claude-opus-5-5"]["priced"])
        self.assertEqual(by_model["claude-opus-5-5"]["confidence"], "published")
        self.assertEqual(by_model[GROK]["rows"], 0)
        self.assertEqual(by_model[GROK]["billing_mode"], "plan-included")
        self.assertFalse(by_model["muse-wonder-full-1.0"]["priced"])
        self.assertEqual(by_model["muse-wonder-full-1.0"]["confidence"], "unknown")


class BeamPricingTest(unittest.TestCase):
    def test_beam_and_free_ids_price_at_zero(self):
        for model in (
            "reflection/beam:free",
            "minimax/minimax-m3.1-flash:free",
            "ornith/ornith-1.5-122b:free",
            "poolside/laguna-s-2.1-free",
            "stealth/glyph-cluster:free",
            "reflection/Beam-1.0-501B-A23B",
            "reflection/Beam-1.0-501B-A23B:xhigh",
            "reflection/Beam-1.0-501B-A23B:high",
            "Beam-1.0-501B-A23B",
            "reflection-beam",
            "reflection-beam:xhigh",
        ):
            info = status(model)
            self.assertTrue(info["priced"], model)
            self.assertEqual(info["billing_mode"], "free", model)
            self.assertEqual(cost(model, 1_000_000, 500_000, 250_000, 0), 0.0, model)

    def test_beam_series_and_effort_suffix_fallback(self):
        from dash.pricing import series_key

        for model in (
            "reflection/beam:free",
            "reflection/Beam-1.0-501B-A23B",
            "reflection/Beam-1.0-501B-A23B:xhigh",
            "reflection-beam",
        ):
            self.assertEqual(series_key(model), "beam", model)
        self.assertIsNone(cost("someprovider/new-model:xhigh", 1000, 100, 0, 0))
        self.assertFalse(status("someprovider/new-model:xhigh")["priced"])


class RepriceTest(unittest.TestCase):
    def test_migrate_adds_split_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dash.sqlite"
            con = sqlite3.connect(path)
            con.execute(
                """CREATE TABLE usage_events (
                       tool TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                       ts REAL, run_id TEXT, model TEXT,
                       input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
                       cache_write INTEGER, reasoning INTEGER, est_cost_usd REAL,
                       PRIMARY KEY (tool, session_id, message_id))"""
            )
            con.execute(
                "INSERT INTO usage_events(tool, session_id, message_id, cache_write) VALUES('claude','s','m',3)"
            )
            con.commit()
            con.close()
            store = Store(path)
            try:
                cols = [row[1] for row in store._conn.execute("PRAGMA table_info(usage_events)")]
                self.assertIn("cache_write_5m", cols)
                self.assertIn("cache_write_1h", cols)
                kept = store._conn.execute("SELECT cache_write FROM usage_events").fetchone()[0]
                self.assertEqual(kept, 3)
            finally:
                store.close()

    def test_reprice_fills_split_from_usage_only(self):
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        prompt = "PROMPT_BODY_DO_NOT_STORE"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            rec = {
                "type": "assistant",
                "timestamp": "2026-10-16T00:00:00.000Z",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": prompt}],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 10,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 3,
                            "ephemeral_1h_input_tokens": 7,
                        },
                    },
                },
            }
            (root / f"{sid}.jsonl").write_bytes(json.dumps(rec).encode() + b"\n")
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.add_usage(
                    {
                        "ts": 10,
                        "session_id": sid,
                        "run_id": "claude-main:%s:main" % sid,
                        "model": "claude-opus-5-5",
                        "tool": "claude",
                        "message_id": "m1",
                        "input": 2,
                        "output": 4,
                        "cache_read": 0,
                        "cache_write": 10,
                        "reasoning": 0,
                    }
                )
                before = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()["est_cost_usd"]
                self.assertIsNone(before)
                summary = reprice(store, str(Path(tmp) / "projects"))
                row = store._conn.execute(
                    "SELECT cache_write_5m, cache_write_1h, est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertEqual(row["cache_write_5m"], 3)
                self.assertEqual(row["cache_write_1h"], 7)
                self.assertAlmostEqual(row["est_cost_usd"], (2 * 4 + 4 * 20 + 3 * 5 + 7 * 8) / 1_000_000)
                self.assertEqual(summary["splits_filled"], 1)
                self.assertNotIn(prompt, json.dumps(summary))
                dumped = json.dumps(
                    [dict(item) for item in store._conn.execute("SELECT * FROM usage_events")]
                )
                self.assertNotIn(prompt, dumped)
            finally:
                store.close()


class NullCostRepriceTest(unittest.TestCase):
    TABLE = [
        {
            "model": "no-such-model-xyz",
            "input": 2,
            "output": 3,
            "cache_read": 1,
            "cache_write_5m": None,
            "cache_write_1h": None,
            "reasoning": None,
            "billing_mode": "api",
            "confidence": "test",
            "provider": "test",
        }
    ]

    def _add_null(self, store, mid, model="no-such-model-xyz"):
        store.add_usage(
            {
                "ts": 1_700_000_000.0,
                "session_id": "s",
                "run_id": "generic:s:%s" % mid,
                "model": model,
                "tool": "generic",
                "message_id": mid,
                "input": 1000,
                "output": 500,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        row = store._conn.execute(
            "SELECT est_cost_usd FROM usage_events WHERE message_id=?", (mid,)
        ).fetchone()
        self.assertIsNone(row["est_cost_usd"])

    def test_null_cost_pass_prices_with_current_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                self._add_null(store, "m1")
                report = reprice_null_costs(store, table=self.TABLE)
                self.assertEqual(report["read"], 1)
                self.assertEqual(report["priced"], 1)
                self.assertFalse(report["truncated"])
                row = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertAlmostEqual(row["est_cost_usd"], (1000 * 2 + 500 * 3) / 1_000_000)
                again = reprice_null_costs(store, table=self.TABLE)
                self.assertEqual(again["read"], 0)
            finally:
                store.close()

    def test_null_cost_pass_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                for mid in ("m1", "m2", "m3"):
                    self._add_null(store, mid)
                report = reprice_null_costs(store, limit=2)
                self.assertEqual(report["read"], 2)
                self.assertEqual(report["priced"], 0)
                self.assertTrue(report["truncated"])
                self.assertEqual(report["still_null"], 2)
                filled = reprice_null_costs(store, limit=2, table=self.TABLE)
                self.assertEqual(filled["read"], 2)
                self.assertEqual(filled["priced"], 2)
                self.assertTrue(filled["truncated"])
                drained = reprice_null_costs(store, table=self.TABLE)
                self.assertEqual(drained["read"], 1)
                self.assertEqual(drained["priced"], 1)
                self.assertFalse(drained["truncated"])
            finally:
                store.close()

    def test_reprice_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.add_usage(
                    {
                        "ts": 1_700_000_000.0,
                        "session_id": "s",
                        "run_id": "generic:s:m1",
                        "model": "claude-fable-5-1",
                        "tool": "generic",
                        "message_id": "m1",
                        "input": 10,
                        "output": 5,
                        "cache_read": 0,
                        "cache_write": 0,
                        "reasoning": 0,
                    }
                )
                store._conn.execute("UPDATE usage_events SET est_cost_usd=NULL")
                store._conn.commit()
                summary = reprice(store, None, dry_run=True)
                self.assertTrue(summary["dry_run"])
                row = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertIsNone(row["est_cost_usd"])
                live = reprice(store, None)
                self.assertFalse(live["dry_run"])
                row = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertIsNotNone(row["est_cost_usd"])
            finally:
                store.close()

    def test_catalog_sig_tracks_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text('{"models": []}', encoding="utf-8")
            first = catalog_sig(path)
            self.assertIsNotNone(first)
            path.write_text('{"models": [{"model": "x"}]}', encoding="utf-8")
            self.assertNotEqual(catalog_sig(path), first)
            self.assertIsNone(catalog_sig(Path(tmp) / "missing.json"))


class RepriceCliTest(unittest.TestCase):
    def _run_main(self, state_dir, args):
        import io
        from contextlib import redirect_stdout

        from agent_dash import main

        old_state = os.environ.get("AGENT_DASH_STATE_DIR")
        old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_STATE_DIR"] = str(state_dir)
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(state_dir)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(args)
        finally:
            if old_state is None:
                os.environ.pop("AGENT_DASH_STATE_DIR", None)
            else:
                os.environ["AGENT_DASH_STATE_DIR"] = old_state
            if old_ws is None:
                os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
            else:
                os.environ["AGENT_DASH_WORKSTREAMS"] = old_ws
        return code, buf.getvalue()

    def test_reprice_help_prints_usage_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--help", "-h"):
                code, out = self._run_main(tmp, ["reprice", flag])
                self.assertEqual(code, 0)
                self.assertIn("--dry-run", out)
            self.assertFalse((Path(tmp) / "dash.sqlite").exists())

    def test_reprice_rejects_unknown_flags_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _out = self._run_main(tmp, ["reprice", "--bogus"])
            self.assertEqual(code, 2)
            self.assertFalse((Path(tmp) / "dash.sqlite").exists())

    def test_reprice_dry_run_leaves_costs_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.add_usage(
                    {
                        "ts": 1_700_000_000.0,
                        "session_id": "s",
                        "run_id": "generic:s:m1",
                        "model": "claude-fable-5-1",
                        "tool": "generic",
                        "message_id": "m1",
                        "input": 10,
                        "output": 5,
                        "cache_read": 0,
                        "cache_write": 0,
                        "reasoning": 0,
                    }
                )
                store._conn.execute("UPDATE usage_events SET est_cost_usd=NULL")
                store._conn.commit()
            finally:
                store.close()
            code, _out = self._run_main(tmp, ["reprice", "--dry-run"])
            self.assertEqual(code, 0)
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                row = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertIsNone(row["est_cost_usd"])
            finally:
                store.close()
            code, _out = self._run_main(tmp, ["reprice"])
            self.assertEqual(code, 0)
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                row = store._conn.execute(
                    "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
                ).fetchone()
                self.assertIsNotNone(row["est_cost_usd"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
