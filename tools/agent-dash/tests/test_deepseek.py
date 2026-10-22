"""Tests for DeepSeek V4.1 Flash usage and cost. All fixtures are synthetic."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.models import model_key, run_model_key
from dash.pricing import band_for, cost, series_key, status
from dash.store.util import TOOL_KEYS as LANES

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_source(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("deepseek_src_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grok = _load_source("grok_cli")


def ts(text):
    """Epoch for a UTC timestamp like '2026-10-19 02:00'."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


# Monday 2026-10-19 is an ordinary weekday; Saturday 2026-10-17 and Sunday
# 2026-10-18 are the weekend; Thursday 2026-10-01 is a listed mainland China
# public holiday (National Day week) while Thursday 2026-10-08 is not.
PEAK_MON_0200 = ts("2026-10-19 02:00")
OFF_MON_0430 = ts("2026-10-19 04:30")
OFF_SUN_1537 = ts("2026-10-18 15:37")
OFF_SAT_0200 = ts("2026-10-17 02:00")
OFF_HOLIDAY_0200 = ts("2026-10-01 02:00")
PEAK_THU_0200 = ts("2026-10-08 02:00")


class CatalogTest(unittest.TestCase):
    def test_deepseek_rows(self):
        from dash.pricing import load

        rows = {row["model"]: row for row in load()}
        flash = rows["deepseek-flash"]
        self.assertEqual(flash["provider"], "deepseek")
        self.assertEqual(float(flash["input"]), 0.30)
        self.assertEqual(float(flash["output"]), 1.20)
        self.assertEqual(float(flash["cache_read"]), 0.006)
        self.assertIsNone(flash["cache_write_5m"])
        self.assertIsNone(flash["cache_write_1h"])
        self.assertIsNone(flash["reasoning"])
        self.assertFalse(flash["input_includes_cache_read"])
        self.assertEqual(flash["billing_mode"], "api")
        self.assertEqual(flash["confidence"], "published")
        self.assertEqual(flash["source_url"], "https://api-docs.deepseek.com/quick_start/pricing")
        self.assertEqual(flash["retrieved_at"], "2026-10-18")
        off = flash["time_bands"]["offpeak"]
        self.assertEqual(float(off["input"]), 0.15)
        self.assertEqual(float(off["output"]), 0.60)
        self.assertEqual(float(off["cache_read"]), 0.003)

        pro = rows["deepseek-v4-pro"]
        self.assertEqual((float(pro["input"]), float(pro["output"]), float(pro["cache_read"])), (1.32, 3.96, 0.044))
        off = pro["time_bands"]["offpeak"]
        self.assertEqual((float(off["input"]), float(off["output"]), float(off["cache_read"])), (0.66, 1.98, 0.022))
        self.assertEqual(pro["retrieved_at"], "2026-10-18")

        for alias in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            row = rows[alias]
            for key in ("input", "output", "cache_read"):
                self.assertEqual(float(row[key]), float(flash[key]), alias)
            self.assertEqual(row["time_bands"], flash["time_bands"], alias)

        for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"):
            info = status(model)
            self.assertTrue(info["priced"], model)
            self.assertEqual(info["provider"], "deepseek", model)


class BandTest(unittest.TestCase):
    def test_utc_edges_on_a_weekday(self):
        cases = [
            ("2026-10-19 00:59", "offpeak"),
            ("2026-10-19 01:00", "peak"),
            ("2026-10-19 03:59", "peak"),
            ("2026-10-19 04:00", "offpeak"),
            ("2026-10-19 05:59", "offpeak"),
            ("2026-10-19 06:00", "peak"),
            ("2026-10-19 09:59", "peak"),
            ("2026-10-19 10:00", "offpeak"),
            ("2026-10-19 00:00", "offpeak"),
            ("2026-10-19 23:30", "offpeak"),
        ]
        for text, want in cases:
            self.assertEqual(band_for("deepseek-flash", ts(text)), want, text)
            self.assertEqual(band_for("deepseek-v4-pro", ts(text)), want, text)

    def test_weekend_is_offpeak(self):
        self.assertEqual(band_for("deepseek-flash", OFF_SAT_0200), "offpeak")
        self.assertEqual(band_for("deepseek-flash", OFF_SUN_1537), "offpeak")

    def test_listed_holiday_is_offpeak_all_day(self):
        # Thursday 2026-10-01 sits in the National Day week: off-peak even
        # inside the weekday peak windows.
        self.assertEqual(band_for("deepseek-flash", OFF_HOLIDAY_0200), "offpeak")
        self.assertEqual(band_for("deepseek-flash", ts("2026-10-01 07:30")), "offpeak")
        # The next Thursday is an ordinary weekday.
        self.assertEqual(band_for("deepseek-flash", PEAK_THU_0200), "peak")

    def test_missing_ts_prices_peak(self):
        self.assertEqual(band_for("deepseek-flash", None), "peak")
        self.assertIsNone(band_for("glm-5.3-flash", PEAK_MON_0200))
        self.assertIsNone(band_for("no-such-model", PEAK_MON_0200))


class CostMathTest(unittest.TestCase):
    def test_peak_and_offpeak_hand_values(self):
        peak = cost("deepseek-flash", 18129, 5, 8192, 0, ts=PEAK_MON_0200)
        self.assertAlmostEqual(peak, (18129 * 0.30 + 8192 * 0.006 + 5 * 1.20) / 1_000_000, places=9)
        self.assertAlmostEqual(peak, 0.005493852, places=9)
        off = cost("deepseek-flash", 18129, 5, 8192, 0, ts=OFF_SUN_1537)
        self.assertAlmostEqual(off, (18129 * 0.15 + 8192 * 0.003 + 5 * 0.60) / 1_000_000, places=9)
        self.assertAlmostEqual(off, 0.002746926, places=9)
        # Off-peak is half of peak on every class.
        self.assertAlmostEqual(off, peak / 2, places=12)

    def test_aliases_bill_as_flash(self):
        for alias in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            self.assertAlmostEqual(
                cost(alias, 18129, 5, 8192, 0, ts=PEAK_MON_0200),
                cost("deepseek-flash", 18129, 5, 8192, 0, ts=PEAK_MON_0200),
                places=12,
            )
            self.assertAlmostEqual(
                cost(alias, 18129, 5, 8192, 0, ts=OFF_SUN_1537),
                cost("deepseek-flash", 18129, 5, 8192, 0, ts=OFF_SUN_1537),
                places=12,
            )

    def test_v4_pro(self):
        peak = cost("deepseek-v4-pro", 1000, 100, 500, 0, ts=PEAK_MON_0200)
        self.assertAlmostEqual(peak, (1000 * 1.32 + 100 * 3.96 + 500 * 0.044) / 1_000_000, places=9)
        off = cost("deepseek-v4-pro", 1000, 100, 500, 0, ts=OFF_SUN_1537)
        self.assertAlmostEqual(off, (1000 * 0.66 + 100 * 1.98 + 500 * 0.022) / 1_000_000, places=9)
        self.assertAlmostEqual(off, peak / 2, places=12)

    def test_reasoning_rides_along_without_double_count(self):
        # The chat_completions output count already includes reasoning, so a
        # separately reported reasoning count must not add a second charge.
        self.assertAlmostEqual(
            cost("deepseek-flash", 1000, 50, 0, 0, ts=PEAK_MON_0200, reasoning=30),
            cost("deepseek-flash", 1000, 50, 0, 0, ts=PEAK_MON_0200, reasoning=0),
            places=12,
        )

    def test_cache_write_stays_unpriced(self):
        self.assertIsNone(cost("deepseek-flash", 1, 1, 0, 5, ts=PEAK_MON_0200))
        self.assertEqual(cost("deepseek-flash", 0, 0, 0, 0, ts=PEAK_MON_0200), 0.0)

    def test_missing_ts_falls_back_to_peak(self):
        self.assertAlmostEqual(
            cost("deepseek-flash", 18129, 5, 8192, 0),
            cost("deepseek-flash", 18129, 5, 8192, 0, ts=PEAK_MON_0200),
            places=12,
        )

    def test_other_models_ignore_ts(self):
        # Byte-identical behaviour for every other model: ts changes nothing.
        self.assertAlmostEqual(
            cost("glm-5.3-flash", 600, 10, 400, 0, ts=PEAK_MON_0200),
            float((600 * 0.15 + 400 * 0.03 + 10 * 0.5) / 1_000_000),
            places=12,
        )
        self.assertAlmostEqual(
            cost("claude-opus-5-5", 2, 4, 0, 10, cache_write_5m=3, cache_write_1h=7, ts=OFF_SUN_1537),
            (2 * 4 + 4 * 20 + 3 * 5 + 7 * 8) / 1_000_000,
            places=12,
        )
        self.assertIsNone(cost("muse-wonder-full-1.0", 10, 10, 10, 0, ts=PEAK_MON_0200))


class ModelKeyTest(unittest.TestCase):
    def test_deepseek_keys(self):
        self.assertEqual(model_key("deepseek-flash"), ("deepseek", "deepseek-flash"))
        self.assertEqual(model_key("deepseek-v4-flash"), ("deepseek", "deepseek-flash"))
        self.assertEqual(model_key("deepseek-v4-flash-vision-exp"), ("deepseek", "deepseek-flash"))
        self.assertEqual(model_key("deepseek-v4-pro"), ("deepseek", "deepseek-v4-pro"))
        self.assertEqual(series_key("deepseek-flash"), "deepseek")
        self.assertEqual(series_key("deepseek-v4-pro"), "deepseek")

    def test_run_hint_does_not_swallow_deepseek(self):
        # Grok CLI runs carry tier/source glm; the model id still decides.
        self.assertEqual(
            run_model_key({"model": "deepseek-flash", "tier": "glm", "source": "glm"}),
            "deepseek-flash",
        )


SID = "99999999-9999-4999-8999-999999999999"
HUB = "00000000-0000-0000-0000-000000000002"


class FakeStore:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = {}
        self.sessions = {}

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset, size, mtime)

    def upsert_run(self, row):
        cur = dict(self.runs.get(row["run_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.runs[row["run_id"]] = cur

    def add_usage(self, row):
        self.usage[(row["tool"], row["session_id"], row["message_id"])] = dict(row)

    def upsert_session(self, row):
        cur = dict(self.sessions.get(row["session_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.sessions[row["session_id"]] = cur


def _synthetic_usage(**over):
    """A usage.json shaped like the real Grok Build log, synthetic counts."""
    turn = {
        "turnNumber": 1,
        "endedAt": "2026-10-19T02:14:11+00:00",
        "inputTokens": 26321,
        "cachedReadTokens": 8192,
        "outputTokens": 5,
        "cacheCreationTokens": 0,
        "reasoningTokens": 0,
        "primaryModelId": "deepseek-flash",
        "totalTokens": 26326,
    }
    turn.update(over)
    return {
        "updatedAt": turn["endedAt"],
        "session": {
            "inputTokens": turn["inputTokens"],
            "cachedReadTokens": turn["cachedReadTokens"],
            "outputTokens": turn["outputTokens"],
            "cacheCreationTokens": turn["cacheCreationTokens"],
            "reasoningTokens": turn["reasoningTokens"],
            "primaryModelId": turn["primaryModelId"],
            "totalTokens": turn["totalTokens"],
        },
        "turns": [turn],
    }


def _poll_tree(tmp, usage):
    sessions = os.path.join(tmp, "sessions")
    sdir = os.path.join(sessions, "cwd", SID)
    os.makedirs(sdir)
    with open(os.path.join(sdir, "usage.json"), "w") as fh:
        json.dump(usage, fh)
    pads = os.path.join(tmp, "pads")
    scratch = os.path.join(pads, "encoded", HUB, "scratchpad")
    os.makedirs(scratch)
    with open(os.path.join(scratch, "glm-sessions.tsv"), "w") as fh:
        fh.write("ds-review\t%s\t1792153370\tdeepseek-flash\n" % SID)
    log_path = os.path.join(tmp, "unified.jsonl")
    return {
        "roots": {
            "grok_sessions": sessions,
            "grok_log": log_path,
            "scratchpads": pads,
        },
        "read_budget": {"per_file": 1024 * 1024, "per_tick": 4 * 1024 * 1024},
    }


class GrokCliDeepseekTest(unittest.TestCase):
    def test_usage_json_records_miss_and_hit(self):
        # usage.json inputTokens includes the cache read (26321); the store
        # keeps the miss (18129) in input and the hit in cache_read.
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            grok.poll(store, _poll_tree(tmp, _synthetic_usage()), 1792154000)
        rows = [row for row in store.usage.values() if row["session_id"] == SID]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["model"], "deepseek-flash")
        self.assertEqual(row["input"], 26321 - 8192)
        self.assertEqual(row["cache_read"], 8192)
        self.assertEqual(row["output"], 5)
        self.assertEqual(row["cache_write"], 0)
        self.assertEqual(row["reasoning"], 0)
        run = store.runs["glm:%s:ds-review" % SID]
        self.assertEqual(run["model"], "deepseek-flash")

    def test_reasoning_rides_along(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            grok.poll(
                store,
                _poll_tree(tmp, _synthetic_usage(reasoningTokens=7, outputTokens=20, totalTokens=26341)),
                1792154000,
            )
        row = next(iter(store.usage.values()))
        self.assertEqual(row["reasoning"], 7)
        self.assertEqual(row["output"], 20)

    def test_session_fallback_keeps_four_fields(self):
        doc = _synthetic_usage()
        del doc["turns"]
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            grok.poll(store, _poll_tree(tmp, doc), 1792154000)
        row = next(iter(store.usage.values()))
        self.assertEqual(row["message_id"], "session")
        self.assertEqual((row["input"], row["cache_read"], row["output"], row["cache_write"]), (18129, 8192, 5, 0))


def _cents(value):
    return round(float(value) * 100, 2)


class StorePricingTest(unittest.TestCase):
    def test_est_uses_the_record_band(self):
        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.add_usage(
                    {
                        "ts": PEAK_MON_0200,
                        "session_id": "s-peak",
                        "run_id": "glm:s-peak:ds",
                        "model": "deepseek-flash",
                        "tool": "glm",
                        "message_id": "turn:1",
                        "input": 18129,
                        "output": 5,
                        "cache_read": 8192,
                        "cache_write": 0,
                        "reasoning": 0,
                    }
                )
                store.add_usage(
                    {
                        "ts": OFF_SUN_1537,
                        "session_id": "s-off",
                        "run_id": "glm:s-off:ds",
                        "model": "deepseek-flash",
                        "tool": "glm",
                        "message_id": "turn:1",
                        "input": 18129,
                        "output": 5,
                        "cache_read": 8192,
                        "cache_write": 0,
                        "reasoning": 0,
                    }
                )
                rows = {
                    row["session_id"]: row
                    for row in store._conn.execute("SELECT session_id, est_cost_usd FROM usage_events")
                }
                self.assertEqual(_cents(rows["s-peak"]["est_cost_usd"]), _cents(0.005493852))
                self.assertEqual(_cents(rows["s-off"]["est_cost_usd"]), _cents(0.002746926))
                self.assertAlmostEqual(rows["s-peak"]["est_cost_usd"], rows["s-off"]["est_cost_usd"] * 2, places=12)
            finally:
                store.close()

    def test_buckets_and_run_fold_agree(self):
        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                for sid, stamp in (("s-peak", PEAK_MON_0200), ("s-off", OFF_SUN_1537)):
                    store.upsert_run(
                        {
                            "run_id": "glm:%s:ds" % sid,
                            "source": "glm",
                            "tier": "glm",
                            "model": "deepseek-flash",
                            "session_id": sid,
                        }
                    )
                    store.add_usage(
                        {
                            "ts": stamp,
                            "session_id": sid,
                            "run_id": "glm:%s:ds" % sid,
                            "model": "deepseek-flash",
                            "tool": "glm",
                            "message_id": "turn:1",
                            "input": 18129,
                            "output": 5,
                            "cache_read": 8192,
                            "cache_write": 0,
                            "reasoning": 0,
                        }
                    )
                cells = store.query_usage("model", "hour", None, OFF_SUN_1537 - 3600, PEAK_MON_0200 + 7200)
                flash = [cell for cell in cells if cell["key"] == "deepseek-flash"]
                self.assertTrue(flash)
                bucket_cost = sum(cell["cost_usd"] for cell in flash)
                self.assertEqual(_cents(bucket_cost), _cents(0.005493852 + 0.002746926))
                self.assertEqual(
                    sum(cell["input"] for cell in flash), 18129 * 2,
                )
                self.assertEqual(sum(cell["cache_read"] for cell in flash), 8192 * 2)
            finally:
                store.close()


class SnapshotAgreementTest(unittest.TestCase):
    def test_snapshot_and_usage_agree_to_the_cent(self):
        from dash.server import DashServer
        from dash.sources import Registry
        from dash.store import Store
        from agent_dash import default_cfg

        events = [
            ("s-peak", PEAK_MON_0200, 18129, 5, 8192),
            ("s-off", OFF_SUN_1537, 27463, 5, 0),
        ]
        hand = 0.005493852 + (27463 * 0.15 + 5 * 0.60) / 1_000_000
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                for sid, stamp, miss, out, hit in events:
                    store.upsert_run(
                        {
                            "run_id": "glm:%s:ds" % sid,
                            "source": "glm",
                            "tier": "glm",
                            "model": "deepseek-flash",
                            "session_id": sid,
                        }
                    )
                    store.add_usage(
                        {
                            "ts": stamp,
                            "session_id": sid,
                            "run_id": "glm:%s:ds" % sid,
                            "model": "deepseek-flash",
                            "tool": "glm",
                            "message_id": "turn:1",
                            "input": miss,
                            "output": out,
                            "cache_read": hit,
                            "cache_write": 0,
                            "reasoning": 0,
                        }
                    )
                port = server.server_address[1]
                since = OFF_SUN_1537 - 3600
                until = PEAK_MON_0200 + 7200
                snap = self._get(port, "/api/snapshot?ws=all&since=%s&until=%s&usage_group=model" % (since, until))
                usage = self._get(port, "/api/usage?group=model&bucket=hour&since=%s&until=%s" % (since, until))
                totals = snap["totals"]
                self.assertEqual(totals["tokens"]["input"], 18129 + 27463)
                self.assertEqual(totals["tokens"]["cache_read"], 8192)
                self.assertEqual(totals["tokens"]["output"], 10)
                self.assertEqual(_cents(totals["cost_usd"]), _cents(hand))
                cells = [cell for cell in usage["buckets"] if cell["key"] == "deepseek-flash"]
                self.assertTrue(cells)
                self.assertEqual(_cents(sum(cell["cost_usd"] for cell in cells)), _cents(hand))
                self.assertEqual(_cents(sum(cell["cost_usd"] for cell in cells)), _cents(totals["cost_usd"]))
                self.assertEqual(snap["usage"], usage["buckets"])
                self.assertEqual(totals["unpriced_models"], [])
            finally:
                server.stop.set()
                server.shutdown()
                thread.join(3)
                server.server_close()
                store.close()

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200, path)
            return json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()


LANES = tuple(LANES)


def _grok_run(store, run_id, model, start, end=None, status="done", label=None):
    """A synthetic Grok CLI run row: ids and counts only, never prompt text."""
    store.upsert_run(
        {
            "run_id": run_id,
            "source": "glm",
            "tier": "glm",
            "model": model,
            "label": label or run_id,
            "session_id": "sess-" + run_id.replace(":", "-"),
            "status": status,
            "started_at": start,
            "ended_at": end,
        }
    )


class GrokCliFamilyTest(unittest.TestCase):
    def test_model_ids_map_to_families(self):
        cases = {
            "deepseek-flash": ("deepseek", "deepseek-flash"),
            "deepseek-v4-flash": ("deepseek", "deepseek-flash"),
            "deepseek-v4-flash-vision-exp": ("deepseek", "deepseek-flash"),
            "deepseek-v4-pro": ("deepseek", "deepseek-v4-pro"),
            "glm-5.3": ("glm", "glm-5.3"),
            "glm-5.3-flash": ("glm", "glm-5.3-flash"),
        }
        for model, want in cases.items():
            self.assertEqual(model_key(model, "glm glm"), want, model)
            self.assertEqual(model_key(model), want, model)

    def test_unknown_grok_hosted_id_is_other(self):
        from dash.store import _tool_key

        for model in ("mimo-v2-flash", "step-3.5-flash", "zzz-9"):
            self.assertEqual(model_key(model, "glm glm"), ("other", "other"), model)
            self.assertEqual(model_key(model), ("other", "other"), model)
            run = {"source": "glm", "tier": "glm", "model": model}
            self.assertEqual(_tool_key(run), "other", model)

    def test_unrecorded_model_stays_glm(self):
        from dash.store import _tool_key

        for model in (None, ""):
            self.assertEqual(model_key(model, "glm glm"), ("glm", "glm-unknown"))
            run = {"source": "glm", "tier": "glm", "model": model}
            self.assertEqual(_tool_key(run), "glm")

    def test_tool_lanes_split_only_deepseek_and_other(self):
        from dash.store import _tool_key

        self.assertEqual(
            _tool_key({"source": "glm", "tier": "glm", "model": "deepseek-flash"}),
            "deepseek",
        )
        # GLM ids, unrecorded models, and other families through the same
        # CLI keep the GLM lane: existing GLM counts do not move.
        for model in ("glm-5.3", "glm-5.3-flash", None, "", "grok-4.7"):
            self.assertEqual(
                _tool_key({"source": "glm", "tier": "glm", "model": model}),
                "glm",
                repr(model),
            )
        self.assertIsNone(_tool_key({"source": "dagster", "tier": "", "model": None}))


class ConcurrencySplitTest(unittest.TestCase):
    BASE = 1_700_000_000

    def _points(self, store):
        return store.query_concurrency(self.BASE + 1000, None, self.BASE - 60, 60)

    def test_overlapping_glm_and_deepseek_runs_split(self):
        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                b = self.BASE
                _grok_run(store, "glm:s1:g-build", "glm-5.3-flash", b, b + 500)
                _grok_run(store, "glm:s2:ds-review", "deepseek-flash", b + 200, b + 700)
                points = self._points(store)
                self.assertTrue(points)
                for point in points:
                    self.assertEqual(set(point), {"t", "models"} | set(LANES))
                    lane_total = sum(point[key] for key in LANES)
                    self.assertEqual(lane_total, sum(point["models"].values()))
                by_t = {point["t"]: point for point in points}
                # GLM-only stretch: the GLM lane is untouched by the split.
                self.assertEqual((by_t[b]["glm"], by_t[b]["deepseek"]), (1, 0))
                # Overlap: two lanes with the right peaks.
                self.assertEqual((by_t[b + 240]["glm"], by_t[b + 240]["deepseek"]), (1, 1))
                self.assertEqual(
                    by_t[b + 240]["models"], {"glm-5.3-flash": 1, "deepseek-flash": 1}
                )
                # DeepSeek-only stretch.
                self.assertEqual((by_t[b + 600]["glm"], by_t[b + 600]["deepseek"]), (0, 1))
                self.assertEqual(max(point["glm"] for point in points), 1)
                self.assertEqual(max(point["deepseek"] for point in points), 1)
                self.assertEqual(sum(point["glm"] for point in points), 9)
            finally:
                store.close()

    def test_other_family_run_leaves_glm_alone(self):
        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                b = self.BASE
                _grok_run(store, "glm:s1:g-build", "glm-5.3-flash", b, b + 500)
                _grok_run(store, "glm:s3:mimo-x", "mimo-v2-flash", b + 120, b + 240)
                points = self._points(store)
                by_t = {point["t"]: point for point in points}
                self.assertEqual(by_t[b + 180]["other"], 1)
                self.assertEqual(by_t[b + 180]["models"], {"glm-5.3-flash": 1, "other": 1})
                self.assertEqual(by_t[b]["other"], 0)
                self.assertEqual(by_t[b + 300]["other"], 0)
                # The GLM lane still counts exactly its own run.
                self.assertEqual(sum(point["glm"] for point in points), 9)
            finally:
                store.close()


class ConcurrencyApiTest(unittest.TestCase):
    def test_concurrency_endpoint_splits_families(self):
        import time

        from dash.server import DashServer
        from dash.sources import Registry
        from dash.store import Store
        from agent_dash import default_cfg

        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            try:
                _grok_run(
                    store, "glm:s1:g-build", "glm-5.3-flash",
                    now - 300, None, status="running",
                )
                _grok_run(
                    store, "glm:s2:ds-review", "deepseek-flash",
                    now - 200, None, status="running",
                )
                snap = self._get(server.server_address[1], "/api/snapshot?ws=all")
                self.assertEqual(snap["counts"]["live_agents"], 2)
                by_tool = snap["counts"]["live_by_tool"]
                self.assertEqual((by_tool["glm"], by_tool["deepseek"]), (1, 1))
                self.assertEqual(sum(by_tool.values()), 2)
                by_model = snap["counts"]["live_by_model"]
                self.assertEqual(
                    (by_model["glm-5.3-flash"], by_model["deepseek-flash"]), (1, 1)
                )
                conc = self._get(
                    server.server_address[1],
                    "/api/concurrency?since=%s&step=60" % (now - 600),
                )
                points = conc["points"]
                self.assertTrue(points)
                for point in points:
                    self.assertEqual(set(point), {"t", "models"} | set(LANES))
                    self.assertEqual(
                        sum(point[key] for key in LANES),
                        sum(point["models"].values()),
                    )
                last = points[-1]
                self.assertEqual((last["glm"], last["deepseek"]), (1, 1))
                self.assertEqual(
                    last["models"], {"glm-5.3-flash": 1, "deepseek-flash": 1}
                )
            finally:
                server.stop.set()
                server.shutdown()
                thread.join(3)
                server.server_close()
                store.close()

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200, path)
            return json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()


class DoctorPricingTest(unittest.TestCase):
    def _cfg(self, state_dir):
        from agent_dash import default_cfg

        cfg = default_cfg()
        cfg["state_dir"] = state_dir
        return cfg

    def _add(self, state_dir, model, tokens, message):
        from dash.store import Store

        store = Store(Path(state_dir) / "dash.sqlite")
        try:
            store.add_usage(
                {
                    "ts": time.time(),
                    "session_id": "s",
                    "run_id": "generic:s:%s" % model,
                    "model": model,
                    "tool": "generic",
                    "message_id": message,
                    "input": tokens,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                }
            )
        finally:
            store.close()

    def test_deepseek_ids_do_not_fire_unpriced(self):
        from unittest import mock

        from dash.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            for model in (
                "deepseek-flash",
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "deepseek-v4-pro",
            ):
                self._add(state, model, 10, "m-%s" % model)
            with mock.patch("dash.doctor._home_scratch", return_value=os.path.join(tmp, "scratch")):
                result = run_doctor(self._cfg(state), "http://127.0.0.1:1")
        unpriced = [item for item in result["problems"] if item["code"] == "unpriced-model"]
        self.assertEqual(unpriced, [])

    def test_unknown_model_still_fires(self):
        from unittest import mock

        from dash.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            self._add(state, "deepseek-flash", 10, "m-ds")
            self._add(state, "no-such-model-zz", 10, "m-zz")
            with mock.patch("dash.doctor._home_scratch", return_value=os.path.join(tmp, "scratch")):
                result = run_doctor(self._cfg(state), "http://127.0.0.1:1")
        unpriced = [item for item in result["problems"] if item["code"] == "unpriced-model"]
        self.assertEqual(len(unpriced), 1)
        self.assertIn("no-such-model-zz", unpriced[0]["message"])
        self.assertNotIn("deepseek-flash", unpriced[0]["message"])


if __name__ == "__main__":
    unittest.main()
