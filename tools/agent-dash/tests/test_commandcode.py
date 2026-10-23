"""Tests for dash/sources/commandcode.py and the Command Code model and price rows.

Synthetic fixtures only: a cmd-sessions.tsv manifest, a stream ndjson, a
running/<label> pid file and session jsonl files, all written under a temp
directory. No live state, no model calls.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import sys

sys.path.insert(0, str(ROOT))

from dash import pricing
from dash.models import model_key

SPEC = importlib.util.spec_from_file_location("dash_sources_commandcode", ROOT / "dash" / "sources" / "commandcode.py")
cc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cc)

SID_OK = "6a4e8dc4-0715-456a-900c-6eadf1803e8f"
SID_THROTTLED = "3c5481ea-dffc-4ded-bb37-5f9044465b6c"
SID_LIVE = "9e62cbcd-2158-450d-af64-1e49e91d98b7"
SID_DEAD = "b26d6bb2-0000-4000-8000-000000000001"
SID_INTERACTIVE = "050f571d-4c03-41c4-a249-2c591bd2ef25"
FREE = ["reflection/beam:free", "minimax/minimax-m3.1-flash:free", "ornith/ornith-1.5-122b:free", "poolside/laguna-s-2.1-free", "stealth/glyph-cluster:free"]

HEADER = "label\tmodel\tsession_id\tstarted_epoch\tended_epoch\tinput_tokens\toutput_tokens\tstatus\n"


class Store:
    """The store surface the source uses, with usage keyed the way the real store keys it."""

    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = {}
        self.sessions = {}
        self.add_calls = 0

    def get_offset(self, key):
        return self.offsets.get(key)

    def set_offset(self, key, inode, offset, size, mtime):
        self.offsets[key] = (inode, offset, size, mtime)

    def upsert_run(self, row):
        cur = dict(self.runs.get(row["run_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.runs[row["run_id"]] = cur

    def upsert_session(self, row):
        cur = dict(self.sessions.get(row["session_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.sessions[row["session_id"]] = cur

    def running_tool_runs(self):
        return [dict(row) for row in self.runs.values() if row.get("status") == "running"]

    def last_activity(self, run_id, started_at):
        return started_at

    def add_usage(self, row):
        self.add_calls += 1
        self.usage[(row["tool"], row["session_id"], row["message_id"])] = dict(row)

    def tokens(self, sid):
        total = {"input": 0, "output": 0, "cache_read": 0}
        for row in self.usage.values():
            if row["session_id"] != sid:
                continue
            for key in total:
                total[key] += row[key] or 0
        return total


def assistant(mid, model, input_tokens, cache_read, output, ts):
    return {
        "type": "message",
        "id": mid,
        "parentId": "p",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        "usage": {"inputTokens": input_tokens, "outputTokens": output, "cacheReadTokens": cache_read, "cacheWriteTokens": 0, "costUsd": 0},
        "model": model,
    }


def session_file(path, sid, cwd, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-22T00:57:00.000Z", "cwd": cwd}
    user = {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-22T00:57:00.100Z", "message": {"role": "user", "content": [{"type": "text", "text": "PROMPT TEXT"}]}}
    rows = [header, user] + lines
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def stream(path, sid):
    lines = [
        {"type": "event", "event": {"type": "run_start", "sessionId": sid}},
        {"type": "event", "event": {"type": "turn_start", "turnNumber": 1}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.scratch = self.tmp / "scratch"
        self.projects = self.tmp / "projects"
        self.pad = self.scratch / "-home-someone-x" / SID_OK / "scratchpad" / "cmd"
        self.pad.mkdir(parents=True)
        self.store = Store()

    def tearDown(self):
        self._tmp.cleanup()

    def cfg(self):
        return {
            "roots": {"scratchpads": [str(self.scratch)], "commandcode_projects": str(self.projects)},
            "read_budgets": {"per_file": 4 * 1024 * 1024, "per_tick": 8 * 1024 * 1024},
        }

    def poll(self, now=1_800_000_000.0):
        cc.poll(self.store, self.cfg(), now)
        self.assertIsNone(cc.last_error)

    def write_tsv(self, rows):
        text = HEADER + "".join("\t".join(str(x) for x in row) + "\n" for row in rows)
        (self.pad / "cmd-sessions.tsv").write_text(text, encoding="utf-8")

    def project_dir(self, cwd_name="home-someone-projects-demo-repo"):
        path = self.projects / cwd_name
        path.mkdir(parents=True, exist_ok=True)
        return path


class ParseRowsTest(unittest.TestCase):
    def test_header_and_short_rows_skipped_and_dash_sid_is_none(self):
        text = HEADER + "ws6-plan-a\treflection/beam:free\t" + SID_OK + "\t1700000000\t1700000100\t100\t20\tok\n" + "ws6-plan-b\tminimax/minimax-m3.1-flash:free\t-\t1700000200\t1700000201\t0\t0\tthrottled\n" + "short\trow\n"
        rows = cc.parse_rows(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sid"], SID_OK)
        self.assertEqual(rows[0]["model"], "reflection/beam:free")
        self.assertEqual((rows[0]["input"], rows[0]["output"], rows[0]["status"]), (100, 20, "ok"))
        self.assertIsNone(rows[1]["sid"])
        self.assertEqual(rows[1]["status"], "throttled")

    def test_failed_status_keeps_its_text(self):
        rows = cc.parse_rows(HEADER + "ws6-plan-c\tstealth/glyph-cluster:free\t-\t1700000000\t1700000001\t0\t0\tfailed rc=1\n")
        self.assertEqual(rows[0]["status"], "failed rc=1")


class TokenParsingTest(unittest.TestCase):
    def test_input_is_uncached_remainder_and_user_lines_are_skipped(self):
        raw = b"".join(
            [
                json.dumps({"type": "session", "id": SID_OK, "timestamp": "2026-10-22T00:57:00.000Z"}).encode() + b"\n",
                json.dumps({"type": "message", "id": "u", "timestamp": "2026-10-22T00:57:00.001Z", "message": {"role": "user"}, "usage": {"inputTokens": 999, "outputTokens": 9, "cacheReadTokens": 0}}).encode() + b"\n",
                json.dumps(assistant("a1", "reflection/beam:free", 19482, 7168, 114, "2026-10-22T00:57:05.000Z")).encode() + b"\n",
                json.dumps(assistant("a2", "reflection/beam:free", 25099, 19456, 67, "2026-10-22T00:57:09.500Z")).encode() + b"\n",
            ]
        )
        header, events, last_ts = cc._parse_lines(raw)
        self.assertEqual(header["id"], SID_OK)
        self.assertEqual([e["message_id"] for e in events], ["a1", "a2"])
        self.assertEqual((events[0]["input"], events[0]["cache_read"], events[0]["output"]), (19482 - 7168, 7168, 114))
        self.assertEqual(events[1]["input"], 25099 - 19456)
        self.assertGreater(last_ts, 0)

    def test_line_without_id_gets_a_stable_content_key(self):
        line = json.dumps({"type": "message", "message": {"role": "assistant"}, "usage": {"inputTokens": 5, "outputTokens": 1, "cacheReadTokens": 0}, "model": "m"}).encode()
        _h, first, _l = cc._parse_lines(line + b"\n")
        _h, second, _l = cc._parse_lines(line + b"\n")
        self.assertEqual(first[0]["message_id"], second[0]["message_id"])
        self.assertTrue(first[0]["message_id"].startswith("line:"))


class PollTest(Fixture):
    def test_labelled_ok_row_takes_tokens_from_the_session_file(self):
        self.write_tsv([["ws6-plan-clients", "reflection/beam:free", SID_OK, 1792000000, 1792000100, 19482, 114, "ok"]])
        stream(self.pad / "stream-ws6-plan-clients-1.ndjson", SID_OK)
        session_file(
            self.project_dir() / (SID_OK + ".jsonl"),
            SID_OK,
            "/home/someone/projects/demo-repo",
            [assistant("a1", "reflection/beam:free", 19482, 7168, 114, "2026-10-22T00:57:05.000Z"), assistant("a2", "reflection/beam:free", 25099, 19456, 67, "2026-10-22T00:57:09.000Z")],
        )
        self.poll()
        run = self.store.runs["commandcode:%s:ws6-plan-clients" % SID_OK]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["model"], "reflection/beam:free")
        self.assertEqual(run["label"], "ws6-plan-clients")
        self.assertEqual(run["source"], "commandcode")
        self.assertEqual(run["tier"], "commandcode")
        self.assertAlmostEqual(run["started_at"], 1792000000.0)
        tokens = self.store.tokens(SID_OK)
        self.assertEqual(tokens, {"input": (19482 - 7168) + (25099 - 19456), "output": 114 + 67, "cache_read": 7168 + 19456})
        zeroed = [row for row in self.store.usage.values() if row["message_id"] == "tsv:" + SID_OK]
        self.assertTrue(all(row["input"] == 0 and row["output"] == 0 for row in zeroed))

    def test_throttled_row_is_died_early_quota_and_its_own_run(self):
        self.write_tsv(
            [
                ["ws6-plan-retry", "reflection/beam:free", SID_THROTTLED, 1792000000, 1792000010, 0, 0, "throttled"],
                ["ws6-plan-retry", "minimax/minimax-m3.1-flash:free", SID_OK, 1792000020, 1792000100, 19482, 114, "ok"],
            ]
        )
        self.project_dir()
        session_file(self.project_dir() / (SID_OK + ".jsonl"), SID_OK, "/x", [assistant("a1", "minimax/minimax-m3.1-flash:free", 5000, 0, 50, "2026-10-22T00:58:00.000Z")])
        self.poll()
        throttled = self.store.runs["commandcode:%s:ws6-plan-retry" % SID_THROTTLED]
        self.assertEqual(throttled["status"], "died_early")
        self.assertEqual(throttled["verdict"], "QUOTA")
        self.assertEqual(throttled["model"], "reflection/beam:free")
        self.assertEqual(throttled["ended_at"], 1792000010)
        self.assertEqual(self.store.runs["commandcode:%s:ws6-plan-retry" % SID_OK]["status"], "done")

    def test_root_may_be_the_encoded_cwd_folder(self):
        self.write_tsv([["ws6-plan-enc", "reflection/beam:free", SID_OK, 1792000000, 1792000100, 10, 2, "ok"]])
        cfg = self.cfg()
        cfg["roots"]["scratchpads"] = [str(self.scratch / "-home-someone-x")]
        cc.poll(self.store, cfg, 1_800_000_000.0)
        self.assertIn("commandcode:%s:ws6-plan-enc" % SID_OK, self.store.runs)

    def test_failed_row_is_died_early_without_verdict(self):
        self.write_tsv([["ws6-plan-bad", "poolside/laguna-s-2.1-free", "-", 1792000000, 1792000003, 0, 0, "failed rc=1"]])
        self.poll()
        run = self.store.runs["commandcode:attempt-ws6-plan-bad-1792000000:ws6-plan-bad"]
        self.assertEqual(run["status"], "died_early")
        self.assertNotIn("verdict", run)

    def test_tsv_totals_fall_back_when_no_session_file(self):
        self.write_tsv([["ws6-plan-nofile", "ornith/ornith-1.5-122b:free", SID_DEAD, 1792000000, 1792000100, 4321, 210, "ok"]])
        self.poll()
        fallback = [row for row in self.store.usage.values() if row["message_id"] == "tsv:" + SID_DEAD]
        self.assertEqual(len(fallback), 1)
        self.assertEqual((fallback[0]["input"], fallback[0]["output"]), (4321, 210))

    def test_live_launch_is_running_while_its_pid_lives(self):
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws6-plan-live").write_text("%d\tstealth/glyph-cluster:free" % os.getpid(), encoding="utf-8")
        stream(self.pad / "stream-ws6-plan-live-1.ndjson", SID_LIVE)
        session_file(self.project_dir() / (SID_LIVE + ".jsonl"), SID_LIVE, "/x", [assistant("l1", "stealth/glyph-cluster:free", 900, 0, 12, "2026-10-22T01:00:00.000Z")])
        self.poll()
        run = self.store.runs["commandcode:%s:ws6-plan-live" % SID_LIVE]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], os.getpid())
        self.assertIsNone(run.get("ended_at"))
        self.assertEqual(run["model"], "stealth/glyph-cluster:free")
        self.assertEqual(self.store.tokens(SID_LIVE)["input"], 900)

    def test_stored_died_early_with_live_launcher_returns_to_running(self):
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws6-plan-revive").write_text(
            "%d\treflection/beam:free" % os.getpid(), encoding="utf-8"
        )
        stream(self.pad / "stream-ws6-plan-revive-1.ndjson", SID_LIVE)
        run_id = "commandcode:%s:ws6-plan-revive" % SID_LIVE
        self.store.runs[run_id] = {
            "run_id": run_id,
            "source": "commandcode",
            "tier": "commandcode",
            "session_id": SID_LIVE,
            "label": "ws6-plan-revive",
            "status": "died_early",
            "pid": None,
            "started_at": 1792000000.0,
            "ended_at": 1792000100.0,
        }
        self.poll()
        run = self.store.runs[run_id]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], os.getpid())

    def test_dead_launch_without_a_row_is_died_early(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws6-plan-dead").write_text("%d\tpoolside/laguna-s-2.1-free" % proc.pid, encoding="utf-8")
        stream(self.pad / "stream-ws6-plan-dead-1.ndjson", SID_DEAD)
        self.poll()
        run = self.store.runs["commandcode:%s:ws6-plan-dead" % SID_DEAD]
        self.assertEqual(run["status"], "died_early")
        self.assertNotEqual(run.get("status"), "running")

    def test_live_file_without_stream_session_is_skipped(self):
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws6-plan-early").write_text("%d\treflection/beam:free" % os.getpid(), encoding="utf-8")
        self.poll()
        self.assertEqual(self.store.runs, {})

    def test_pi_outdir_with_running_folder_is_ignored(self):
        # A live pi launch (its own running/ folder plus a pi-shaped
        # stream, a prompt file, even a manifest) must not read as a
        # second commandcode run for the same label.
        pi_pad = self.scratch / "-home-someone-x" / SID_OK / "scratchpad" / "pi"
        pi_pad.mkdir(parents=True)
        (pi_pad / "running").mkdir()
        (pi_pad / "running" / "ws6-plan-pi").write_text(
            "%d\triver/deepseek-ai/DeepSeek-V4.1-Flash-1017" % os.getpid(), encoding="utf-8"
        )
        (pi_pad / "stream-ws6-plan-pi.ndjson").write_text(
            '{"type": "session", "id": "pi-sid-1"}\n', encoding="utf-8"
        )
        (pi_pad / "prompt-ws6-plan-pi.md").write_text(
            "[label: ws6-plan-pi]\ndo it\n", encoding="utf-8"
        )
        self.poll()
        self.assertEqual(self.store.runs, {})
        (pi_pad / "pi-sessions.tsv").write_text(
            "label\tsession_id\tstarted_epoch\tmodel\n"
            "ws6-plan-pi\tpi-sid-1\t1792000000\triver/deepseek-ai/DeepSeek-V4.1-Flash-1017\n",
            encoding="utf-8",
        )
        self.poll()
        self.assertEqual(self.store.runs, {})

    def test_running_only_cmd_attempt_is_kept(self):
        # The guard must not hide a genuine Command Code launch that has
        # no TSV row yet: running/ plus a cooldown marker and an attempt
        # stream is a cmd outdir from the first attempt.
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws6-plan-early").write_text(
            "%d\treflection/beam:free" % os.getpid(), encoding="utf-8"
        )
        (self.pad / "cooldown.json").write_text("{}", encoding="utf-8")
        stream(self.pad / "stream-ws6-plan-early-1.ndjson", SID_LIVE)
        session_file(self.project_dir() / (SID_LIVE + ".jsonl"), SID_LIVE, "/x", [])
        self.poll()
        self.assertIn("commandcode:%s:ws6-plan-early" % SID_LIVE, self.store.runs)
    def test_orphaned_live_only_attempt_closes_on_the_next_poll(self):
        run_id = "commandcode:%s:ws6-plan-gone" % SID_DEAD
        self.store.runs[run_id] = {
            "run_id": run_id,
            "source": "commandcode",
            "tier": "commandcode",
            "session_id": SID_DEAD,
            "label": "ws6-plan-gone",
            "status": "running",
            "pid": 999999999,
            "started_at": 1792000000.0,
        }
        (self.pad / "running").mkdir()
        self.poll()
        run = self.store.runs[run_id]
        self.assertEqual(run["status"], "died_early")
        self.assertEqual(run["ended_at"], 1792000000.0)

    def test_live_only_orphan_closes_when_the_tsv_row_has_another_session(self):
        self.write_tsv([["ws6-plan-retry2", "reflection/beam:free", SID_OK, 1792000000, 1792000100, 10, 2, "ok"]])
        orphan_id = "commandcode:%s:ws6-plan-retry2" % SID_DEAD
        self.store.runs[orphan_id] = {
            "run_id": orphan_id,
            "source": "commandcode",
            "tier": "commandcode",
            "session_id": SID_DEAD,
            "label": "ws6-plan-retry2",
            "status": "running",
            "pid": 999999999,
            "started_at": 1791999900.0,
        }
        self.poll()
        self.assertEqual(self.store.runs["commandcode:%s:ws6-plan-retry2" % SID_OK]["status"], "done")
        self.assertEqual(self.store.runs[orphan_id]["status"], "died_early")

    def test_backfill_scoped_poll_does_not_close_other_sessions(self):
        run_id = "commandcode:%s:ws6-plan-gone" % SID_DEAD
        self.store.runs[run_id] = {
            "run_id": run_id,
            "source": "commandcode",
            "tier": "commandcode",
            "session_id": SID_DEAD,
            "label": "ws6-plan-gone",
            "status": "running",
            "pid": 999999999,
            "started_at": 1792000000.0,
        }
        cfg = dict(self.cfg(), backfill_session="someone-else")
        cc.poll(self.store, cfg, 1_800_000_000.0)
        self.assertIsNone(cc.last_error)
        self.assertEqual(self.store.runs[run_id]["status"], "running")

    def test_unlabelled_session_is_a_run_with_no_label(self):
        session_file(
            self.project_dir("home-someone-projects-other-repo") / (SID_INTERACTIVE + ".jsonl"),
            SID_INTERACTIVE,
            "/home/someone/projects/other-repo",
            [assistant("i1", "reflection/beam:free", 30000, 29000, 400, "2026-10-21T14:51:32.000Z")],
        )
        self.poll()
        run = self.store.runs["commandcode:%s:%s" % (SID_INTERACTIVE, SID_INTERACTIVE)]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["model"], "reflection/beam:free")
        self.assertNotIn("label", run)
        self.assertEqual(self.store.tokens(SID_INTERACTIVE), {"input": 1000, "output": 400, "cache_read": 29000})
        self.assertEqual(self.store.sessions[SID_INTERACTIVE]["tool"], "commandcode")

    def test_appended_lines_are_read_once_and_unchanged_files_are_not_rewritten(self):
        path = self.project_dir() / (SID_INTERACTIVE + ".jsonl")
        session_file(path, SID_INTERACTIVE, "/x", [assistant("i1", "reflection/beam:free", 100, 0, 5, "2026-10-21T14:51:32.000Z")])
        self.poll()
        calls = self.store.add_calls
        self.poll()
        self.assertEqual(self.store.add_calls, calls, "an unchanged file must not re-add usage")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant("i2", "reflection/beam:free", 300, 100, 7, "2026-10-21T14:52:00.000Z")) + "\n")
        self.poll()
        self.assertEqual(self.store.tokens(SID_INTERACTIVE), {"input": 100 + 200, "output": 5 + 7, "cache_read": 100})

    def test_checkpoint_files_are_not_sessions(self):
        session_file(self.project_dir() / (SID_INTERACTIVE + ".jsonl"), SID_INTERACTIVE, "/x", [assistant("i1", "reflection/beam:free", 10, 0, 1, "2026-10-21T14:51:32.000Z")])
        (self.project_dir() / (SID_DEAD + ".checkpoints.jsonl")).write_text("{}\n", encoding="utf-8")
        self.poll()
        self.assertNotIn(SID_DEAD, self.store.sessions)


class LateLabelRepairTest(unittest.TestCase):
    """Usage filed before the manifest links the label moves to the labeled run.

    End to end against the real store: a session file is consumed with
    no TSV row (orphan ``commandcode:<sid>:<sid>``), then the manifest
    row lands and the next poll must leave one labeled run holding all
    events.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.scratch = self.tmp / "scratch"
        self.projects = self.tmp / "projects"
        self.pad = self.scratch / "-home-someone-x" / SID_OK / "scratchpad" / "cmd"
        self.pad.mkdir(parents=True)
        from dash.store import Store as RealStore

        self.store = RealStore(self.tmp / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def cfg(self):
        return {
            "roots": {"scratchpads": [str(self.scratch)], "commandcode_projects": str(self.projects)},
            "read_budgets": {"per_file": 4 * 1024 * 1024, "per_tick": 8 * 1024 * 1024},
        }

    def test_pre_link_usage_moves_to_labeled_run(self):
        path = self.projects / "home-someone-projects-demo-repo"
        path.mkdir(parents=True)
        session_file(
            path / (SID_OK + ".jsonl"),
            SID_OK,
            "/home/someone/projects/demo-repo",
            [
                assistant("i1", "reflection/beam:free", 30000, 29000, 400, "2026-10-21T14:51:32.000Z"),
                assistant("i2", "reflection/beam:free", 31000, 30000, 410, "2026-10-21T14:52:00.000Z"),
            ],
        )
        cc.poll(self.store, self.cfg(), 1_800_000_000.0)
        self.assertIsNone(cc.last_error)
        orphan = "commandcode:%s:%s" % (SID_OK, SID_OK)
        self.assertIsNotNone(self.store.get_run(orphan))
        BEFORE = HEADER + "ws6-plan-cc\treflection/beam:free\t%s\t1791999900\t1792000000\t100\t20\tok\n" % SID_OK
        (self.pad / "cmd-sessions.tsv").write_text(BEFORE, encoding="utf-8")
        cc.poll(self.store, self.cfg(), 1_800_000_100.0)
        self.assertIsNone(cc.last_error)
        labeled = "commandcode:%s:ws6-plan-cc" % SID_OK
        self.assertIsNone(self.store.get_run(orphan))
        self.assertIsNotNone(self.store.get_run(labeled))
        run_ids = {
            row["run_id"]
            for row in self.store._conn.execute(
                "SELECT run_id FROM usage_events WHERE session_id=?", (SID_OK,)
            ).fetchall()
        }
        self.assertEqual(run_ids, {labeled})


class FreeModelPricingTest(unittest.TestCase):
    def test_five_free_models_price_at_zero(self):
        for model in FREE:
            status = pricing.status(model)
            self.assertTrue(status["priced"], model)
            self.assertEqual(status["billing_mode"], "free", model)
            self.assertEqual(pricing.cost(model, 1_000_000, 500_000, 250_000, 0), 0.0, model)

    def test_unknown_command_code_model_is_unpriced_not_zero(self):
        self.assertIsNone(pricing.cost("someprovider/new-model:free", 1000, 100, 0, 0))
        self.assertFalse(pricing.status("someprovider/new-model:free")["priced"])

    def test_families_and_series(self):
        for model in FREE:
            if "beam" in model:
                self.assertEqual(model_key(model, "commandcode"), ("beam", "beam"))
                self.assertEqual(model_key(model, ""), ("beam", "beam"), model)
                self.assertEqual(pricing.series_key(model), "beam")
                continue
            self.assertEqual(model_key(model, "commandcode")[0], "commandcode")
            self.assertEqual(model_key(model, "")[0], "commandcode", model)
            self.assertEqual(pricing.series_key(model), "commandcode")
        self.assertEqual(model_key("", "commandcode"), ("commandcode", "commandcode-unknown"))
        self.assertEqual(model_key("glm-5.3", ""), ("glm", "glm-5.3"))


class LiveRunEvidenceTest(unittest.TestCase):
    def test_stream_session_reads_the_newest_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            pad = Path(tmp)
            stream(pad / "stream-ws6-x-1.ndjson", SID_OK)
            stream(pad / "stream-ws6-x-2.ndjson", SID_LIVE)
            self.assertEqual(cc._stream_session(str(pad), "ws6-x"), SID_LIVE)
            self.assertIsNone(cc._stream_session(str(pad), "ws6-missing"))


if __name__ == "__main__":
    unittest.main()
