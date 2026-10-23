"""Tests for dash.sources.claude. Synthetic fixtures plus a bounded real-file smoke."""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash.sources import claude  # noqa: E402

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SID2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PROMPT = "PROMPT_BODY_DO_NOT_STORE"
NOW = 1_792_000_000


class MemStore:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.sessions = {}
        self.usage = {}

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset)

    def upsert_session(self, row):
        self.sessions[row["session_id"]] = row

    def upsert_run(self, row):
        self.runs[row["run_id"]] = row

    def add_usage(self, row):
        key = (row["tool"], row["session_id"], row["message_id"])
        self.usage[key] = row


def _write(path, data, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))


def _line(mid, output, sid=SID, model="claude-opus-5-5", content=PROMPT, ts="2026-10-16T00:00:00.000Z"):
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": sid,
        "cwd": "/tmp/example",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": output,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
                "output_tokens_details": {"thinking_tokens": 3},
            },
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _cfg(root, **extra):
    projects = os.path.join(root, "projects")
    sessions = os.path.join(root, "sessions")
    os.makedirs(projects, exist_ok=True)
    os.makedirs(sessions, exist_ok=True)
    cfg = {
        "claude_projects": projects,
        "claude_sessions": sessions,
        "dir_scan_s": 0,
        "live_window_s": 36 * 3600,
        "running_mtime_s": 900,
        "read_budgets": {"per_file": 4 * 1024 * 1024, "per_tick": 8 * 1024 * 1024},
    }
    cfg.update(extra)
    return cfg, projects, sessions


def _dump(store):
    payload = {
        "runs": list(store.runs.values()),
        "usage": list(store.usage.values()),
        "sessions": list(store.sessions.values()),
    }
    return json.dumps(payload, default=str)


class ClaudeParseTests(unittest.TestCase):
    def test_tier_of(self):
        self.assertEqual(claude.tier_of("claude-opus-5-5"), "opus")
        self.assertEqual(claude.tier_of("claude-sonnet-5-5"), "sonnet")
        self.assertEqual(claude.tier_of("haiku"), "haiku")
        self.assertEqual(claude.tier_of("glm-5.3"), "glm")
        self.assertIsNone(claude.tier_of(""))
        self.assertIsNone(claude.tier_of(None))

    def test_dedupe_last_wins_including_shrink(self):
        data = _line("m1", 10) + _line("m1", 50) + _line("m1", 40) + _line("m2", 1)
        rows = claude.parse_assistant_records(data)
        self.assertEqual([row["message_id"] for row in rows], ["m1", "m2"])
        self.assertEqual(rows[0]["output"], 40)
        self.assertEqual(rows[0]["reasoning"], 3)
        self.assertEqual(rows[0]["cache_write"], 7)
        self.assertNotIn("content", rows[0])
        self.assertNotIn(PROMPT, json.dumps(rows))

    def test_skips_blob_even_when_needle_is_inside(self):
        blob = (
            b'{"type":"api-request-blob","echo":"prefix '
            b'\\"type":"assistant" tail"}\n'
        )
        self.assertIn(b'"type":"assistant"', blob)
        rows = claude.parse_assistant_records(blob + _line("m9", 4))
        self.assertEqual([row["message_id"] for row in rows], ["m9"])

    def test_spaced_json_and_non_assistant(self):
        rec = {
            "type": "assistant",
            "timestamp": "2026-10-16T00:00:01.000Z",
            "sessionId": SID,
            "message": {
                "id": "spaced",
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
        spaced = json.dumps(rec).encode() + b"\n"
        self.assertIn(b'"type": "assistant"', spaced)
        user = b'{"type":"user","message":{"content":"hi"}}\n'
        rows = claude.parse_assistant_records(user + spaced)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-haiku-5-5")
        self.assertEqual(claude.tier_of(rows[0]["model"]), "haiku")

    def test_backfill_cfg_keys(self):
        self.assertEqual(claude._backfill({"backfill_session": "abc"}), "abc")
        self.assertEqual(
            claude._backfill({"mode": "backfill", "session_id": "abc"}), "abc"
        )
        self.assertIsNone(claude._backfill({}))

    def test_transcript_status_matrix(self):
        self.assertEqual(
            claude.transcript_status(
                caught_up=True, fresh=False, registry_busy=True, finished=True
            ),
            "running",
        )
        self.assertEqual(
            claude.transcript_status(
                caught_up=False, fresh=False, registry_busy=False, finished=True
            ),
            "running",
        )
        self.assertEqual(
            claude.transcript_status(
                caught_up=True, fresh=True, registry_busy=False, finished=True
            ),
            "done",
        )
        self.assertEqual(
            claude.transcript_status(
                caught_up=True, fresh=True, registry_busy=False, finished=False
            ),
            "running",
        )
        self.assertEqual(
            claude.transcript_status(
                caught_up=True, fresh=False, registry_busy=False, finished=False
            ),
            "done",
        )


class ClaudePollTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()

    def tearDown(self):
        self.tmp.cleanup()
        claude.reset_state()

    def test_incremental_budget_and_cross_chunk_growth(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        first = _line("m1", 10)
        second = _line("m1", 80)
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, first + second, NOW)
        cfg["read_budgets"] = {"per_file": 1024 * 1024, "per_tick": len(first)}
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["output"], 10)
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["output"], 80)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["source"], "claude-main")
        self.assertEqual(run["tier"], "opus")
        self.assertNotIn(PROMPT, _dump(self.store))

    def test_newer_file_wins_the_tick_budget(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        older = _line("old", 1, sid=SID)
        newer = _line("new", 2, sid=SID2)
        _write(os.path.join(projects, "enc", f"{SID}.jsonl"), older, NOW - 10)
        _write(os.path.join(projects, "enc", f"{SID2}.jsonl"), newer, NOW)
        cfg["read_budgets"] = {"per_file": 1024 * 1024, "per_tick": len(newer)}
        claude.poll(self.store, cfg, NOW)
        self.assertIn(("claude", SID2, "new"), self.store.usage)
        self.assertNotIn(("claude", SID, "old"), self.store.usage)
        claude.poll(self.store, cfg, NOW)
        self.assertIn(("claude", SID, "old"), self.store.usage)

    def test_partial_line_waits_for_newline(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, _line("m1", 6).rstrip(b"\n"), NOW)
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage, {})
        with open(path, "ab") as fh:
            fh.write(b"\n")
        os.utime(path, (NOW, NOW))
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["output"], 6)

    def test_stale_eof_without_newline_is_read(self):
        cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=100)
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, _line("m1", 9).rstrip(b"\n"), NOW - 10_000)
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["output"], 9)

    def test_live_window_skips_old_backfill_reads_it(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, _line("m1", 3), NOW - 40 * 3600)
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage, {})
        self.assertEqual(self.store.offsets, {})
        cfg["backfill_session"] = SID
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["input"], 2)

    def test_subagent_label_from_meta_not_prompt(self):
        cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=100)
        base = os.path.join(projects, "enc", SID, "subagents")
        jsonl = os.path.join(base, "agent-aaa.jsonl")
        desc = ("A" * 200) + "SENTINEL_TAIL"
        _write(jsonl, _line("m1", 4, model="claude-haiku-5-5"), NOW)
        _write(
            os.path.join(base, "agent-aaa.meta.json"),
            json.dumps(
                {
                    "description": desc,
                    "model": "haiku",
                    "toolUseId": "toolu_x",
                    "prompt": PROMPT,
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-subagent:{SID}:aaa"]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["parent_session_id"], SID)
        self.assertEqual(run["tier"], "haiku")
        self.assertEqual(len(run["label"]), 200)
        self.assertNotIn("SENTINEL_TAIL", run["label"])
        self.assertNotIn(PROMPT, _dump(self.store))
        os.utime(jsonl, (NOW - 5000, NOW - 5000))
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self.store.runs[f"claude-subagent:{SID}:aaa"]["status"], "done")

    def test_workflow_meta_beats_journal_and_result_finishes(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        wf = os.path.join(projects, "enc", SID, "subagents", "workflows", "wf_abc")
        _write(
            os.path.join(wf, "journal.jsonl"),
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "started",
                            "agentId": "aaa",
                            "label": "review",
                            "phase": "Review",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "result",
                            "agentId": "aaa",
                            "result": {"text": PROMPT},
                        }
                    ),
                ]
            )
            + "\n",
            NOW,
        )
        _write(
            os.path.join(wf, "agent-aaa.jsonl"),
            _line("m1", 11, model="claude-sonnet-5-5"),
            NOW,
        )
        _write(
            os.path.join(wf, "agent-aaa.meta.json"),
            json.dumps({"description": "Spike the index", "model": "sonnet"}),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-workflow:{SID}:wf_abc/aaa"]
        self.assertEqual(run["label"], "Spike the index")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["source"], "claude-workflow")
        self.assertEqual(run["tier"], "sonnet")
        self.assertEqual(run["parent_session_id"], SID)
        self.assertNotIn(PROMPT, _dump(self.store))
        self.assertNotEqual(run["label"], "review")

    def test_registry_busy_with_live_pid_and_fresh_transcript_runs(self):
        cfg, projects, sessions = _cfg(self.tmp.name, running_mtime_s=60)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW,
        )
        started_ms = 1_700_000_000_000
        live_pid = os.getpid()
        _write(
            os.path.join(sessions, "42.json"),
            json.dumps(
                {
                    "pid": live_pid,
                    "sessionId": SID,
                    "cwd": "/tmp/example",
                    "startedAt": started_ms,
                    "status": "busy",
                    "name": "example-session",
                    "messagingSocketPath": "/tmp/secret.sock",
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        session = self.store.sessions[SID]
        self.assertEqual(session["status"], "busy")
        self.assertEqual(session["cwd"], "/tmp/example")
        self.assertAlmostEqual(session["started_at"], 1_700_000_000, places=3)
        self.assertNotIn("sock", _dump(self.store))
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], live_pid)

    def test_registry_dead_pid_with_fresh_transcript_stays_running(self):
        # A dead registry pid must not close a session that is still
        # writing: the registry goes stale across CLI restarts while the
        # transcript is ground truth.
        cfg, projects, sessions = _cfg(self.tmp.name, running_mtime_s=60)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW,
        )
        _write(
            os.path.join(sessions, "43.json"),
            json.dumps(
                {
                    "pid": 999999,
                    "sessionId": SID,
                    "cwd": "/tmp/example",
                    "startedAt": 1_700_000_000_000,
                    "status": "busy",
                    "name": "example-session",
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run.get("pid"))
        self.assertIsNone(run.get("ended_at"))

    def test_registry_busy_with_dead_pid_and_stale_transcript_closes(self):
        cfg, projects, sessions = _cfg(self.tmp.name, running_mtime_s=60)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW - 3600,
        )
        _write(
            os.path.join(sessions, "43.json"),
            json.dumps(
                {
                    "pid": 999999,
                    "sessionId": SID,
                    "cwd": "/tmp/example",
                    "startedAt": 1_700_000_000_000,
                    "status": "busy",
                    "name": "example-session",
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "done")
        self.assertIsNone(run.get("pid"))
        self.assertIsNotNone(run.get("ended_at"))

    def test_reopened_main_clears_idle_verdict(self):
        # A stale close (done, then a later verdict/ended set by the
        # liveness sweep) must not linger once the transcript resumes:
        # the reopened row reads running with no end evidence.
        cfg, projects, sessions = _cfg(self.tmp.name, running_mtime_s=60)
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, _line("m1", 1), NOW - 3600)
        _write(
            os.path.join(sessions, "43.json"),
            json.dumps(
                {
                    "pid": 999999,
                    "sessionId": SID,
                    "cwd": "/tmp/example",
                    "startedAt": 1_700_000_000_000,
                    "status": "busy",
                    "name": "example-session",
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run_id = f"claude-main:{SID}:main"
        self.assertEqual(self.store.runs[run_id]["status"], "done")
        self.store.runs[run_id]["verdict"] = "IDLE"
        claude._runs[run_id]["verdict"] = "IDLE"
        with open(path, "ab") as fh:
            fh.write(_line("m2", 2))
        os.utime(path, (NOW, NOW))
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[run_id]
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run.get("ended_at"))
        self.assertIsNone(run.get("verdict"))

    def test_registry_busy_with_stale_transcript_is_not_running(self):
        cfg, projects, sessions = _cfg(self.tmp.name, running_mtime_s=60)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW - 3600,
        )
        _write(
            os.path.join(sessions, "44.json"),
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sessionId": SID,
                    "cwd": "/tmp/example",
                    "startedAt": 1_700_000_000_000,
                    "status": "busy",
                    "name": "example-session",
                }
            ),
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "done")

    def test_quiet_main_past_short_window_is_idle(self):
        # Twelve quiet minutes: past the 3 min main window, inside the
        # old 15 min bound. Reads idle (unknown), never running.
        cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=900)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW - 720,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "unknown")
        self.assertEqual(self.store.sessions[SID]["status"], "idle")

    def test_main_window_is_configurable(self):
        cfg, projects, _sessions = _cfg(
            self.tmp.name, running_mtime_s=900, main_running_mtime_s=3600
        )
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW - 720,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-main:{SID}:main"]
        self.assertEqual(run["status"], "running")

    def test_bad_json_and_offset_error_do_not_stop_the_tick(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)

        class Selective(MemStore):
            def set_offset(self, path, inode, offset, size, mtime):
                if "badproj" in str(path):
                    raise RuntimeError("nope")
                super().set_offset(path, inode, offset, size, mtime)

        bad = os.path.join(projects, "badproj", f"{SID}.jsonl")
        good = os.path.join(projects, "goodproj", f"{SID2}.jsonl")
        _write(bad, b'{"type":"assistant" not json\n' + _line("bad", 1, sid=SID), NOW)
        _write(good, _line("good", 2, sid=SID2), NOW - 5)
        store = Selective()
        claude.poll(store, cfg, NOW)
        self.assertIn(("claude", SID2, "good"), store.usage)
        self.assertNotIn(PROMPT, _dump(store))

    def test_oversized_blob_is_skipped(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        blob = b'{"type":"api-request-blob","pad":"' + (b"x" * 3000) + b'"}\n'
        path = os.path.join(projects, "enc", f"{SID}.jsonl")
        _write(path, blob + _line("m1", 5), NOW)
        cfg["read_budgets"] = {"per_file": 500, "per_tick": 500}
        for _ in range(20):
            claude.poll(self.store, cfg, NOW)
            if ("claude", SID, "m1") in self.store.usage:
                break
        self.assertEqual(self.store.usage[("claude", SID, "m1")]["output"], 5)

    def test_directory_scan_is_cached(self):
        cfg, projects, _sessions = _cfg(self.tmp.name, dir_scan_s=1000)
        _write(os.path.join(projects, "enc", f"{SID}.jsonl"), _line("a", 1, sid=SID), NOW)
        claude.poll(self.store, cfg, NOW)
        self.assertIn(("claude", SID, "a"), self.store.usage)
        _write(
            os.path.join(projects, "enc", f"{SID2}.jsonl"),
            _line("b", 1, sid=SID2),
            NOW + 1,
        )
        claude.poll(self.store, cfg, NOW + 1)
        self.assertNotIn(("claude", SID2, "b"), self.store.usage)
        claude.poll(self.store, cfg, NOW + 1001)
        self.assertIn(("claude", SID2, "b"), self.store.usage)


class ClaudeInheritanceTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()

    def tearDown(self):
        self.tmp.cleanup()
        claude.reset_state()

    def test_main_has_no_parent_subagent_links_to_lead(self):
        cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=100)
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _line("m1", 1),
            NOW,
        )
        base = os.path.join(projects, "enc", SID, "subagents")
        _write(os.path.join(base, "agent-aaa.jsonl"), _line("m2", 2, model="claude-haiku-5-5"), NOW)
        claude.poll(self.store, cfg, NOW)
        main = self.store.runs[f"claude-main:{SID}:main"]
        self.assertIsNone(main["parent_session_id"])
        sub = self.store.runs[f"claude-subagent:{SID}:aaa"]
        self.assertEqual(sub["parent_session_id"], SID)
        self.assertEqual(sub["session_id"], SID)

    def test_journal_only_workflow_row_links_to_lead(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        wf = os.path.join(projects, "enc", SID, "subagents", "workflows", "wf_x")
        _write(
            os.path.join(wf, "journal.jsonl"),
            json.dumps({"type": "started", "agentId": "aaa", "label": "helper"}) + "\n",
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-workflow:{SID}:wf_x/aaa"]
        self.assertEqual(run["parent_session_id"], SID)
        self.assertEqual(run["session_id"], SID)

    def test_bundle_write_hint_lands_on_session(self):
        bundles = os.path.join(self.tmp.name, "bundles")
        os.makedirs(os.path.join(bundles, "WS9"), exist_ok=True)
        old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = bundles
        try:
            cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=100)
            rec = {
                "type": "assistant",
                "timestamp": "2026-10-16T00:00:00.000Z",
                "sessionId": SID,
                "cwd": "/tmp/example",
                "message": {
                    "id": "m1",
                    "model": "claude-sonnet-5-5",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": os.path.join(bundles, "WS9", "plan.json")},
                        }
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            }
            _write(
                os.path.join(projects, "enc", f"{SID}.jsonl"),
                json.dumps(rec, separators=(",", ":")).encode() + b"\n",
                NOW,
            )
            claude.poll(self.store, cfg, NOW)
            self.assertEqual(self.store.sessions[SID].get("ws_hint"), "WS9")
            self.assertNotIn(bundles, _dump(self.store))
        finally:
            if old is None:
                os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
            else:
                os.environ["AGENT_DASH_WORKSTREAMS"] = old

    def test_write_outside_bundles_leaves_no_hint(self):
        cfg, projects, _sessions = _cfg(self.tmp.name, running_mtime_s=100)
        rec = {
            "type": "assistant",
            "timestamp": "2026-10-16T00:00:00.000Z",
            "sessionId": SID,
            "cwd": "/tmp/example",
            "message": {
                "id": "m1",
                "model": "claude-sonnet-5-5",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/tmp/example/notes.md"},
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        }
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            json.dumps(rec, separators=(",", ":")).encode() + b"\n",
            NOW,
        )
        claude.poll(self.store, cfg, NOW)
        self.assertIsNone(self.store.sessions[SID].get("ws_hint"))


class ClaudeSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local transcript")
    def test_transcript_prefix(self):
        raw_path = os.environ.get("AGENT_DASH_SMOKE_TRANSCRIPT")
        if not raw_path or not os.path.isfile(raw_path):
            self.skipTest("AGENT_DASH_SMOKE_TRANSCRIPT is not set")
        raw = Path(raw_path).read_bytes()[:2_000_000]
        cut = raw.rfind(b"\n")
        self.assertGreater(cut, 0)
        rows = claude.parse_assistant_records(raw[: cut + 1])
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertNotIn("content", row)
            self.assertIsInstance(row["output"], int)


if __name__ == "__main__":
    unittest.main()
