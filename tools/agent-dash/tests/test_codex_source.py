"""Tests for dash/sources/codex.py. Synthetic fixtures plus pricing checks.

Fixtures mirror the real rollout shapes (session_meta, turn_context,
token_usage_record, task_started/task_complete, token_count echoes) with no
prompt text anywhere. The real-data math they encode was verified against
~/.codex/sessions before these were written: total == input + output,
cached_input inside input, reasoning_output inside output, and
sum(usage.total) == final thread total on all nine threads.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def load(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("dash_sources_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


codex = load("codex")

from dash.models import model_key  # noqa: E402
from dash.pricing import cost, status as price_status  # noqa: E402
from dash.store.util import TOOL_KEYS, _tool_key  # noqa: E402

T0 = 1792444800.0


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


class Store:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = {}
        self.sessions = {}
        self.usage_n = 0

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
        for key in ("tool", "session_id", "message_id"):
            assert row.get(key) is not None, key
        self.usage_n += 1
        key = (row["tool"], row["session_id"], row["message_id"])
        self.usage[key] = dict(row)

    def upsert_session(self, row):
        cur = dict(self.sessions.get(row["session_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.sessions[row["session_id"]] = cur


def meta(tid, ts, sid=None, originator="codex-tui", thread_source="user",
         cwd="/tmp/proj", parent=None, base_instructions=None):
    payload = {
        "id": tid,
        "session_id": sid or tid,
        "cwd": cwd,
        "originator": originator,
        "thread_source": thread_source,
        "cli_version": "0.163.0",
        "source": "vscode",
        "model_provider": "openai",
        "git": {"branch": "ws12/thing", "commit_hash": "abc"},
    }
    if parent is not None:
        payload["source"] = {"subagent": {"thread_spawn": {
            "parent_thread_id": parent, "agent_path": "/root/deploy_review"}}}
    if base_instructions is not None:
        payload["base_instructions"] = base_instructions
    return {"type": "session_meta", "timestamp": iso(ts), "payload": payload}


def turn(turn_id, model, ts, cwd="/tmp/proj"):
    return {"type": "turn_context", "timestamp": iso(ts), "payload": {
        "turn_id": turn_id, "root_turn_id": turn_id, "cwd": cwd,
        "model": model, "effort": "high"}}


def usage(tid, turn_id, resp, ts, Input, cached, output, reasoning, cw=0):
    triple = {"input_tokens": Input, "cached_input_tokens": cached,
              "cache_write_input_tokens": cw, "output_tokens": output,
              "reasoning_output_tokens": reasoning,
              "total_tokens": Input + output}
    return {"type": "token_usage_record", "timestamp": iso(ts), "payload": {
        "thread_id": tid, "turn_id": turn_id, "session_id": tid,
        "root_turn_id": turn_id, "response_id": resp, "usage": dict(triple),
        "turn_token_usage": dict(triple), "thread_token_usage": dict(triple)}}


def echo(ts, total, last):
    return {"type": "event_msg", "timestamp": iso(ts), "payload": {
        "type": "token_count",
        "info": {"total_token_usage": dict(total),
                 "last_token_usage": dict(last)}}}


def task(kind, turn_id, ts):
    return {"type": "event_msg", "timestamp": iso(ts), "payload": {
        "type": kind, "turn_id": turn_id, "root_turn_id": turn_id}}


def summed(store, tid=None):
    total = {k: 0 for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    for row in store.usage.values():
        if tid is not None and row["session_id"] != tid:
            continue
        for key in total:
            total[key] += row[key] or 0
    return total


def write_rollout(root, name, lines, mtime=None):
    day = os.path.join(root, "2026", "10", "19")
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, name)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def poll(store, root, now, **cfg):
    params = {"roots": {"codex_sessions": root}}
    params.update(cfg)
    codex.poll(store, params, now)


class CodexCase(unittest.TestCase):
    def setUp(self):
        codex.reset_state()


TID = "01a15609-2376-7ce1-999a-7b5388a1274f"
TURN_A = "01a1560a-0741-7c71-903e-8a4d515aab1a"
TURN_B = "01a1560b-01f0-7690-a7f9-509d1547e02f"


class LedgerTest(CodexCase):
    def test_deltas_are_the_ledger_and_echoes_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            lines = [
                meta(TID, T0),
                task("task_started", TURN_A, T0 + 1),
                turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(TID, TURN_A, "resp-1", T0 + 2, 19014, 12160, 176, 0),
                echo(T0 + 3,
                     {"input_tokens": 19014, "cached_input_tokens": 12160,
                      "cache_write_input_tokens": 0, "output_tokens": 176,
                      "reasoning_output_tokens": 0, "total_tokens": 19190},
                     {"input_tokens": 19014, "cached_input_tokens": 12160,
                      "cache_write_input_tokens": 0, "output_tokens": 176,
                      "reasoning_output_tokens": 0, "total_tokens": 19190}),
                usage(TID, TURN_A, "resp-2", T0 + 4, 24060, 18816, 277, 19),
                task("task_complete", TURN_A, T0 + 5),
            ]
            write_rollout(root, "rollout-2026-10-19T17-20-01-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            total = summed(store, TID)
            # Stored input is the uncached miss; echoes add nothing.
            self.assertEqual(total["input"], (19014 - 12160) + (24060 - 18816))
            self.assertEqual(total["cache_read"], 12160 + 18816)
            self.assertEqual(total["output"], 176 + 277)
            self.assertEqual(total["reasoning"], 19)
            self.assertEqual(len(store.usage), 2)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["source"], "codex")
            self.assertEqual(run["tier"], "codex")
            self.assertEqual(run["model"], "gpt-6.1-sol")
            self.assertEqual(run["status"], "done")
            self.assertIsNotNone(run.get("ended_at"))

    def test_compaction_zero_echo_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            lines = [
                meta(TID, T0),
                turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 400, 50, 5),
                echo(T0 + 3,
                     {"input_tokens": 1000, "cached_input_tokens": 400,
                      "cache_write_input_tokens": 0, "output_tokens": 50,
                      "reasoning_output_tokens": 5, "total_tokens": 1050},
                     {"input_tokens": 0, "cached_input_tokens": 0,
                      "cache_write_input_tokens": 0, "output_tokens": 0,
                      "reasoning_output_tokens": 0, "total_tokens": 22598}),
            ]
            write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            self.assertEqual(summed(store, TID)["input"], 600)

    def test_model_switch_attributes_per_turn(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            lines = [
                meta(TID, T0),
                turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0),
                turn(TURN_B, "gpt-6-luna", T0 + 3),
                usage(TID, TURN_B, "resp-2", T0 + 4, 2000, 200, 20, 0),
            ]
            write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            by_resp = {row["message_id"]: row for row in store.usage.values()}
            self.assertEqual(by_resp["resp-1"]["model"], "gpt-6.1-sol")
            self.assertEqual(by_resp["resp-2"]["model"], "gpt-6-luna")
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["model"], "gpt-6-luna")

    def test_unchanged_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            lines = [meta(TID, T0), turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                     usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0)]
            write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            first = store.usage_n
            self.assertEqual(first, 1)
            poll(store, root, now + 2)
            self.assertEqual(store.usage_n, first)
            self.assertEqual(len(store.usage), 1)

    def test_truncated_last_line_then_completed(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            path = write_rollout(
                root, "rollout-x-%s.jsonl" % TID,
                [meta(TID, T0), turn(TURN_A, "gpt-6.1-sol", T0 + 1)],
                mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            self.assertEqual(len(store.usage), 0)
            with open(path, "ab") as fh:
                blob = json.dumps(usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0)).encode()
                fh.write(blob[:len(blob) // 2])
            poll(store, root, now + 2)
            self.assertEqual(len(store.usage), 0)
            with open(path, "ab") as fh:
                fh.write(blob[len(blob) // 2:] + b"\n")
            poll(store, root, now + 4)
            self.assertEqual(len(store.usage), 1)


SUB = "01a155f3-4c89-7341-a45a-99819a133c27"
PARENT = "01a155ee-f0a1-72f0-a224-c4a72047faad"
SUB_TURN = "01a155f3-4cc8-7422-ba54-0a4bc5415726"


class ForkResumeTest(CodexCase):
    def test_fork_replaying_history_keeps_separate_ledgers(self):
        # A forked thread replays the parent's calls under new response ids:
        # each thread's cumulative still belongs to that thread alone.
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            parent_lines = [
                meta(PARENT, T0),
                turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(PARENT, TURN_A, "resp-p1", T0 + 2, 5000, 1000, 100, 10),
            ]
            child_lines = [
                meta(SUB, T0 + 10, sid=PARENT, thread_source="subagent", parent=PARENT),
                turn(SUB_TURN, "gpt-6.1-sol", T0 + 11),
                usage(SUB, SUB_TURN, "resp-c1", T0 + 12, 5000, 1000, 100, 10),
                usage(SUB, SUB_TURN, "resp-c2", T0 + 13, 6000, 2000, 120, 12),
            ]
            write_rollout(root, "rollout-a-%s.jsonl" % PARENT, parent_lines, mtime=T0 - 9000)
            write_rollout(root, "rollout-b-%s.jsonl" % SUB, child_lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            self.assertEqual(summed(store, PARENT)["input"], 4000)
            self.assertEqual(summed(store, SUB)["input"], 4000 + 4000)
            self.assertEqual(len(store.usage), 3)

    def test_resumed_session_appends_without_recounting(self):
        with tempfile.TemporaryDirectory() as root:
            lines = [meta(TID, T0), turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                     usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0)]
            path = write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, T0 + 600)
            self.assertEqual(summed(store, TID)["output"], 10)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(usage(TID, TURN_A, "resp-2", T0 + 610, 2000, 200, 20, 0)) + "\n")
            poll(store, root, T0 + 620)
            total = summed(store, TID)
            self.assertEqual(total["output"], 30)
            self.assertEqual(total["input"], 900 + 1800)
            self.assertEqual(len(store.usage), 2)


class RestartTest(CodexCase):
    def test_restart_heals_meta_and_never_downgrades_source(self):
        # A fresh process meets a fully consumed, quiet file: the head line
        # restores originator/parent, and the stored lead source survives.
        with tempfile.TemporaryDirectory() as root:
            lines = [
                meta(TID, T0),
                turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0),
                task("task_complete", TURN_A, T0 + 3),
            ]
            write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, T0 + 600)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["source"], "codex")
            self.assertEqual(run["started_at"], T0)
            codex.reset_state()
            poll(store, root, T0 + 900)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["source"], "codex")
            self.assertEqual(run["status"], "done")
            self.assertEqual(len(store.usage), 1)


class SubagentTest(CodexCase):
    def test_subagent_links_parent_and_runs_as_exec(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            lines = [
                meta(SUB, T0, sid=PARENT, thread_source="subagent", parent=PARENT),
                turn(SUB_TURN, "gpt-6.1-sol", T0 + 1),
                usage(SUB, SUB_TURN, "resp-1", T0 + 2, 20356, 12032, 56, 0),
                task("task_complete", SUB_TURN, T0 + 3),
            ]
            write_rollout(root, "rollout-b-%s.jsonl" % SUB, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            run = store.runs["codex:%s:codex-01a155f3" % SUB]
            self.assertEqual(run["parent_session_id"], PARENT)
            self.assertEqual(run["source"], "codex-exec")
            self.assertEqual(run["tier"], "codex")
            self.assertEqual(summed(store, SUB)["input"], 20356 - 12032)
            session = store.sessions[SUB]
            self.assertEqual(session["parent_session_id"], PARENT)
            self.assertEqual(session["cwd"], "/tmp/proj")

    def test_sqlite_edge_is_the_fallback_parent(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "state_5.sqlite")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
            con.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, ?)", (PARENT, SUB, "open"))
            con.commit()
            con.close()
            lines = [
                meta(SUB, T0, sid=SUB, thread_source="subagent"),
                turn(SUB_TURN, "gpt-6.1-sol", T0 + 1),
                usage(SUB, SUB_TURN, "resp-1", T0 + 2, 1000, 100, 10, 0),
            ]
            write_rollout(root, "rollout-b-%s.jsonl" % SUB, lines, mtime=T0 - 9000)
            store = Store()
            codex.poll(store, {"roots": {"codex_sessions": root, "codex_db": db}}, T0 + 600)
            run = store.runs["codex:%s:codex-01a155f3" % SUB]
            self.assertEqual(run["parent_session_id"], PARENT)
            self.assertEqual(run["source"], "codex-exec")


class PrivacyTest(CodexCase):
    def test_prompt_bodies_never_reach_the_store(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 600
            secret = "sekret-prompt-body-9f3c"
            lines = [
                meta(TID, T0, base_instructions="sekret-base-instructions-77aa"),
                {"type": "response_item", "timestamp": iso(T0 + 1),
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": secret}]}},
                {"type": "compacted", "timestamp": iso(T0 + 2),
                 "payload": {"message": "", "replacement_history": [
                     {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": secret}]}]}},
                turn(TURN_A, "gpt-6.1-sol", T0 + 3),
                usage(TID, TURN_A, "resp-1", T0 + 4, 1000, 100, 10, 0),
                task("task_complete", TURN_A, T0 + 5),
            ]
            write_rollout(root, "rollout-x-%s.jsonl" % TID, lines, mtime=T0 - 9000)
            store = Store()
            poll(store, root, now)
            blob = json.dumps([store.runs, list(store.usage.values()),
                               store.sessions, store.offsets])
            self.assertNotIn("sekret-base-instructions-77aa", blob)
            self.assertNotIn(secret, blob)
            self.assertNotIn("base_instructions", blob)
            self.assertEqual(len(store.usage), 1)


class StatusTest(CodexCase):
    def lines(self, complete=True):
        rows = [meta(TID, T0), turn(TURN_A, "gpt-6.1-sol", T0 + 1),
                usage(TID, TURN_A, "resp-1", T0 + 2, 1000, 100, 10, 0)]
        if complete:
            rows.append(task("task_complete", TURN_A, T0 + 3))
        return rows

    def test_live_file_is_running(self):
        with tempfile.TemporaryDirectory() as root:
            now = time.time()
            write_rollout(root, "rollout-x-%s.jsonl" % TID, self.lines(), mtime=now - 5)
            store = Store()
            poll(store, root, now)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["status"], "running")

    def test_quiet_completed_file_is_done(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 9000
            write_rollout(root, "rollout-x-%s.jsonl" % TID, self.lines(), mtime=T0)
            store = Store()
            poll(store, root, now)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["status"], "done")
            self.assertIsNotNone(run.get("ended_at"))

    def test_quiet_uncompleted_file_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            now = T0 + 9000
            write_rollout(root, "rollout-x-%s.jsonl" % TID, self.lines(complete=False), mtime=T0)
            store = Store()
            poll(store, root, now)
            run = store.runs["codex:%s:codex-01a15609" % TID]
            self.assertEqual(run["status"], "unknown")


CODEX_MODELS = [
    "gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-reserve", "codex-auto-review",
]


class CodexModelKeyTest(unittest.TestCase):
    def test_codex_ids_map_to_the_codex_family(self):
        for model in CODEX_MODELS:
            self.assertEqual(model_key(model, "codex"), ("codex", model), model)
        self.assertEqual(model_key("gpt-6.1-sol"), ("codex", "gpt-6.1-sol"))
        self.assertEqual(model_key(None, "codex"), ("codex", "codex-unknown"))
        self.assertEqual(model_key("", "codex-exec"), ("codex", "codex-unknown"))

    def test_codex_lane_and_tool_keys(self):
        self.assertIn("codex", TOOL_KEYS)
        self.assertEqual(_tool_key({"source": "codex", "tier": "codex"}), "codex")
        self.assertEqual(_tool_key({"source": "codex-exec", "tier": "codex"}), "codex")


PRICED_CODEX_MODELS = [
    "gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
]
UNPRICED_CODEX_MODELS = ["gpt-reserve", "codex-auto-review"]


class CodexPricingTest(unittest.TestCase):
    def test_priced_rows_match_the_openai_short_context_list_rates(self):
        from dash.pricing import load
        rows = {row["model"]: row for row in load()}
        for model in PRICED_CODEX_MODELS:
            row = rows[model]
            self.assertEqual(row["provider"], "openai", model)
            self.assertEqual(row["billing_mode"], "plan-included", model)
            self.assertEqual(row["confidence"], "published", model)
            self.assertEqual(row["retrieved_at"], "2026-10-19", model)
            self.assertFalse(row["input_includes_cache_read"], model)
            self.assertIsNone(row["reasoning"], model)
            self.assertIsNone(row["cache_write_1h"], model)
            self.assertEqual(row["long_context"]["threshold"], 272000, model)
            self.assertEqual(row["long_context"]["comparison"], "gt", model)
            info = price_status(model)
            self.assertTrue(info["priced"], model)
            self.assertEqual(info["billing_mode"], "plan-included", model)
        self.assertEqual(
            tuple(float(rows["gpt-6.1-sol"][k]) for k in ("input", "cache_read", "cache_write_5m", "output")),
            (2.0, 0.1, 2.5, 10.0),
        )
        self.assertEqual(
            tuple(float(rows["gpt-6-luna"][k]) for k in ("input", "cache_read", "cache_write_5m", "output")),
            (0.1, 0.01, 0.125, 0.5),
        )

    def test_priced_rows_bill_miss_hit_and_output(self):
        # 100k miss input + 100k cached input + 100k output, all short context.
        self.assertAlmostEqual(
            cost("gpt-6.1-sol", 100_000, 100_000, 100_000, 0),
            0.2 + 0.01 + 1.0,
        )
        self.assertAlmostEqual(
            cost("gpt-6-luna", 100_000, 100_000, 100_000, 0),
            0.01 + 0.001 + 0.05,
        )

    def test_cache_write_bills_at_the_single_write_rate(self):
        # 200k stays under the 272k long-context threshold.
        self.assertAlmostEqual(cost("gpt-6.1-sol", 0, 0, 0, 200_000), 0.5)
        self.assertAlmostEqual(cost("gpt-6-astra", 0, 0, 0, 200_000), 2.5)

    def test_reasoning_is_not_double_counted(self):
        # reasoning tokens sit inside output; a null reasoning rate bills nothing extra.
        self.assertAlmostEqual(
            cost("gpt-6-luna", 0, 1_000_000, 0, 0, reasoning=400_000),
            0.5,
        )

    def test_whole_request_moves_to_long_context_rates_above_272k(self):
        # 200k miss + 100k cached = 300k prompt tokens: every class at the long rates.
        self.assertAlmostEqual(
            cost("gpt-6.1-sol", 200_000, 100_000, 100_000, 0),
            200_000 * 4.0 / 1e6 + 100_000 * 0.2 / 1e6 + 100_000 * 15.0 / 1e6,
        )
        # 272k exactly is not over the threshold (comparison is gt).
        self.assertAlmostEqual(
            cost("gpt-6.1-sol", 272_000, 0, 0, 0),
            272_000 * 2.0 / 1e6,
        )

    def test_unpublished_codex_ids_stay_unpriced(self):
        from dash.pricing import load
        rows = {row["model"]: row for row in load()}
        for model in UNPRICED_CODEX_MODELS:
            row = rows[model]
            self.assertEqual(row["confidence"], "unverified", model)
            self.assertEqual(row["billing_mode"], "plan-included", model)
            self.assertIsNone(row["input"], model)
            self.assertIsNone(row["output"], model)
            self.assertIsNone(row["cache_read"], model)
            self.assertFalse(price_status(model)["priced"], model)
            self.assertIsNone(cost(model, 1_000_000, 500_000, 900_000, 0), model)


if __name__ == "__main__":
    unittest.main()
