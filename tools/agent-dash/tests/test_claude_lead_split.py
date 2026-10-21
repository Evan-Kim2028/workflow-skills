"""Tests for the two dashboard faults: idle mains and lead workstream splits.

Fault 1: a main session is running only on fresh evidence (short
transcript window, a live child, or an unanswered tool call);
otherwise it reads idle (``unknown``) while recent and ``done`` after.
Fault 2: a lead session working several workstreams gains
per-workstream slice runs with exact token conservation.
Synthetic transcripts only; the live instance check covers the real files.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import lead_split  # noqa: E402
from dash.sources import claude  # noqa: E402
from dash.store import Store  # noqa: E402

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SID2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
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


def _ts(epoch):
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _usage_line(
    mid,
    *,
    sid=SID,
    model="claude-opus-4-6",
    text="working",
    tools=(),
    tokens=None,
    epoch=NOW,
):
    content = [{"type": "text", "text": text}]
    for index, (name, tool_input) in enumerate(tools):
        content.append(
            {
                "type": "tool_use",
                "id": f"tu-{mid}-{index}",
                "name": name,
                "input": tool_input,
            }
        )
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
    }
    names = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cache_read": "cache_read_input_tokens",
        "cache_write": "cache_creation_input_tokens",
    }
    for key, value in (tokens or {}).items():
        usage[names.get(key, key)] = value
    rec = {
        "type": "assistant",
        "timestamp": _ts(epoch),
        "sessionId": sid,
        "cwd": "/tmp/example",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": content,
            "usage": usage,
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _user_result_line(tool_use_id, text="done"):
    rec = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": text,
                }
            ],
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


class MainStatusTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()

    def tearDown(self):
        self.tmp.cleanup()
        claude.reset_state()

    def _poll_main(self, data, age_s, **cfg_extra):
        cfg, projects, _sessions = _cfg(self.tmp.name, **cfg_extra)
        _write(os.path.join(projects, "enc", f"{SID}.jsonl"), data, NOW - age_s)
        claude.poll(self.store, cfg, NOW)
        return self.store.runs[f"claude-main:{SID}:main"]

    def test_quiet_12min_main_is_idle_not_running(self):
        run = self._poll_main(_usage_line("m1", text="idle chatter"), 720)
        self.assertEqual(run["status"], "unknown")
        self.assertEqual(self.store.sessions[SID]["status"], "idle")

    def test_fresh_1min_main_is_running(self):
        run = self._poll_main(_usage_line("m1", text="typing"), 60)
        self.assertEqual(run["status"], "running")
        self.assertEqual(self.store.sessions[SID]["status"], "running")

    def test_main_window_config_widens_running(self):
        run = self._poll_main(
            _usage_line("m1", text="typing"), 720, main_running_mtime_s=3600
        )
        self.assertEqual(run["status"], "running")

    def test_main_window_default_is_3min(self):
        self.assertEqual(claude.MAIN_RUNNING_MTIME_S, 180)

    def test_in_flight_tool_call_keeps_running(self):
        data = _usage_line(
            "m1",
            text="launching a long build",
            tools=[("Bash", {"command": "make -j world", "description": "long build"})],
        )
        run = self._poll_main(data, 720)
        self.assertEqual(run["status"], "running")

    def test_answered_tool_call_goes_idle(self):
        data = _usage_line(
            "m1",
            text="quick check",
            tools=[("Bash", {"command": "true", "description": "quick"})],
        ) + _user_result_line("tu-m1-0")
        run = self._poll_main(data, 720)
        self.assertEqual(run["status"], "unknown")

    def test_quiet_70min_main_is_done(self):
        run = self._poll_main(_usage_line("m1", text="old chatter"), 4200)
        self.assertEqual(run["status"], "done")
        self.assertIsNotNone(run.get("ended_at"))

    def test_decide_matrix(self):
        decide = claude.decide_main_status
        self.assertEqual(
            decide(caught_up=False, mtime_age=10_000, pid_dead=False,
                   has_live_child=False, pending_tool=False),
            "running",
        )
        self.assertEqual(
            decide(caught_up=True, mtime_age=60, pid_dead=True,
                   has_live_child=False, pending_tool=False),
            "done",
        )
        self.assertEqual(
            decide(caught_up=True, mtime_age=60, pid_dead=False,
                   has_live_child=False, pending_tool=False),
            "running",
        )
        self.assertEqual(
            decide(caught_up=True, mtime_age=720, pid_dead=False,
                   has_live_child=True, pending_tool=False),
            "running",
        )
        self.assertEqual(
            decide(caught_up=True, mtime_age=720, pid_dead=False,
                   has_live_child=False, pending_tool=True),
            "running",
        )
        self.assertEqual(
            decide(caught_up=True, mtime_age=720, pid_dead=False,
                   has_live_child=False, pending_tool=False),
            "unknown",
        )
        # Pending past the old 15 min bound is not evidence of life.
        self.assertEqual(
            decide(caught_up=True, mtime_age=901, pid_dead=False,
                   has_live_child=False, pending_tool=True),
            "unknown",
        )
        # "Under 60 min" is strict: exactly an hour reads done.
        self.assertEqual(
            decide(caught_up=True, mtime_age=3600, pid_dead=False,
                   has_live_child=False, pending_tool=False),
            "done",
        )

    def test_subagent_rule_unchanged_at_12min(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        sub = os.path.join(projects, "enc", SID, "subagents", "agent-foo.jsonl")
        _write(sub, _usage_line("s1", text="agent work"), NOW - 720)
        claude.poll(self.store, cfg, NOW)
        run = self.store.runs[f"claude-subagent:{SID}:foo"]
        self.assertEqual(run["status"], "running")


class SplitPureTests(unittest.TestCase):
    def test_signal_kinds_vote(self):
        order, launches = lead_split.signals_in_text(
            "ws10-exec-docs fixes WS11.3 in lor-wt-ws12-a on ws8/notes "
            "writing workstreams/WS9/plan.json"
        )
        self.assertEqual(order, ["WS10", "WS11", "WS12", "WS8", "WS9"])
        self.assertEqual(launches, ["ws10-exec-docs"])

    def test_hub_casts_no_vote_but_links(self):
        order, launches = lead_split.signals_in_text("hub-exec-thing does WS9.1")
        self.assertEqual(order, ["WS9"])
        self.assertEqual(launches, ["hub-exec-thing"])

    def test_branch_segment_needs_separator(self):
        order, _ = lead_split.signals_in_text("on ws2/notes today")
        self.assertEqual(order, ["WS2"])
        # ``wt-w2`` is not a workstream mention; a train name holding a
        # label-shaped fragment still votes its workstream.
        order, _ = lead_split.signals_in_text("wt-w2 and train-ws2-x")
        self.assertEqual(order, ["WS2"])

    def test_worktree_path_votes_once(self):
        order, _ = lead_split.signals_in_text("in lor-wt-ws11-evid-docs")
        self.assertEqual(order, ["WS11"])

    def test_pick_majority_then_first_seen(self):
        self.assertEqual(lead_split.pick_turn(["WS10", "WS11", "WS11"]), "WS11")
        self.assertEqual(lead_split.pick_turn(["WS10", "WS11"]), "WS10")
        self.assertIsNone(lead_split.pick_turn([]))

    def test_cursor_inherits_bounded_run(self):
        cursor = lead_split.SplitCursor()
        self.assertEqual(cursor.observe(["WS11"]), "WS11")
        for _ in range(lead_split.INHERIT_RUN_MAX):
            self.assertEqual(cursor.observe([]), "WS11")
        self.assertIsNone(cursor.observe([]))

    def test_cursor_resignal_resets_run(self):
        cursor = lead_split.SplitCursor()
        cursor.observe(["WS11"])
        for _ in range(lead_split.INHERIT_RUN_MAX):
            cursor.observe([])
        self.assertEqual(cursor.observe(["WS10"]), "WS10")
        self.assertEqual(cursor.observe([]), "WS10")

    def test_slice_label_is_explicit_and_text_free(self):
        label = lead_split.slice_label("WS11", SID)
        self.assertTrue(label.startswith("ws11-lead-"))
        self.assertNotIn(SID, label)
        from dash.attribution import attribute

        ws, conf = attribute(label, "other-session")
        self.assertEqual((ws, conf), ("WS11", "regex"))

    def test_material_thresholds(self):
        primary, wanted = lead_split.material_slices(
            {"WS9": 1_000_000, "WS11": 100_000}, "WS9"
        )
        self.assertEqual((primary, wanted), ("WS9", ["WS11"]))
        primary, wanted = lead_split.material_slices(
            {"WS9": 1_000_000, "WS10": 60_000}, "WS9"
        )
        self.assertEqual((primary, wanted), ("WS9", ["WS10"]))
        primary, wanted = lead_split.material_slices(
            {"WS9": 1_000_000, "WS10": 49_999}, "WS9"
        )
        self.assertEqual((primary, wanted), ("WS9", []))
        # Single-workstream sessions gain no rows.
        self.assertEqual(
            lead_split.material_slices({"WS9": 500_000}, "WS9"), ("WS9", [])
        )
        # An unattributed lead takes its top workstream as primary and,
        # with a single signalled workstream, gains no extra rows.
        self.assertEqual(
            lead_split.material_slices({"WS9": 10, "WS11": 200_000}, None),
            ("WS11", []),
        )


def _three_ws_transcript():
    # Signals live in TARGETING fields only (Read paths, launch
    # commands, checkout operands); the text blocks are discussion
    # and never vote. WS9 and WS10 stay below the slice bar; WS11
    # clears 100k billed and earns the slice.
    return (
        _usage_line("w9a", text="ws9-exec-a running WS9.4",
                    tools=[("Read", {"file_path": "workstreams/WS9/plan.json"})],
                    tokens={"output": 5_000})
        + _usage_line("w9b", text="more WS9.5", tokens={"output": 5_000})
        + _usage_line(
            "w11a",
            model="claude-sonnet-4-6",
            text="launching the WS11 lane",
            tools=[(
                "Bash",
                {
                    "command": "muse exec --prompt-file "
                    "/tmp/scratchpad/ws11/prompts/ws11-exec-docs.md",
                    "description": "ws11-exec-docs",
                },
            )],
            tokens={"output": 150_000},
        )
        + _usage_line(
            "w11b",
            model="claude-sonnet-4-6",
            text="checked lor-wt-ws11-evid-docs",
            tools=[("Read", {"file_path": "lor-wt-ws11-evid-docs/notes.md"})],
            tokens={"output": 60_000},
        )
        + _usage_line("w10a", text="ws10-exec-tiny",
                      tools=[("Bash", {"command": "git checkout ws10/tiny",
                                       "description": "peek"})],
                      tokens={"output": 1_000})
    )


def _usage_totals(store):
    rows = store._conn.execute(
        "SELECT run_id, model, input_tokens, output_tokens, cache_read,"
        " cache_write, reasoning FROM usage_events"
    ).fetchall()
    per_model: dict = {}
    per_run: dict = {}
    for row in rows:
        cell = per_model.setdefault(row["model"], dict.fromkeys(
            ("input", "output", "cache_read", "cache_write", "reasoning"), 0))
        for kind in cell:
            cell[kind] += row["cache_read"] if kind == "cache_read" else row[
                "output_tokens" if kind == "output" else
                "input_tokens" if kind == "input" else
                "cache_write" if kind == "cache_write" else "reasoning"
            ] or 0
        per_run[row["run_id"]] = per_run.get(row["run_id"], 0) + (
            (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
            + (row["cache_read"] or 0) + (row["cache_write"] or 0)
        )
    return per_model, per_run


class SplitStoreTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        claude.reset_state()

    def _backfill(self, data, sid=SID, seed_ws9_workers=True):
        cfg, projects, _sessions = _cfg(self.tmp.name, backfill_session=sid)
        if seed_ws9_workers:
            for index in range(3):
                self.store.upsert_run(
                    {
                        "run_id": f"muse:seed{index}:ws9-exec-seed{index}",
                        "source": "muse",
                        "label": f"ws9-exec-seed{index}",
                        "session_id": f"seed-{index}",
                        "parent_session_id": sid,
                        "status": "done",
                        "started_at": NOW - 100,
                        "ended_at": NOW - 50,
                    }
                )
        _write(os.path.join(projects, "enc", f"{sid}.jsonl"), data, NOW)
        claude.poll(self.store, cfg, NOW)
        return cfg

    def test_split_conservation_per_kind_per_model(self):
        self._backfill(_three_ws_transcript())
        primary_id = f"claude-main:{SID}:main"
        slice_id = f"claude-main:{SID}:main@WS11"
        per_model, per_run = _usage_totals(self.store)
        opus = per_model["claude-opus-4-6"]
        self.assertEqual(
            opus,
            {"input": 30, "output": 11_000, "cache_read": 90,
             "cache_write": 120, "reasoning": 0},
        )
        sonnet = per_model["claude-sonnet-4-6"]
        self.assertEqual(
            sonnet,
            {"input": 20, "output": 210_000, "cache_read": 60,
             "cache_write": 80, "reasoning": 0},
        )
        # Every token lives on exactly one run: primary remainder plus
        # the WS11 slice tile to the session total, to the token.
        total = sum(per_run.values())
        self.assertEqual(total, 10_160 + 210_160 + 1_080)
        self.assertEqual(per_run[slice_id], 210_160)
        self.assertEqual(per_run[primary_id], 10_160 + 1_080)
        self.assertNotIn(f"claude-main:{SID}:main@WS10", per_run)
        # The slice links to the primary and keeps its attribution.
        sl = self.store.get_run(slice_id)
        self.assertEqual(sl["slice_of"], primary_id)
        self.assertEqual(sl["workstream"], "WS11")
        self.assertEqual(sl["ws_confidence"], "regex")
        self.assertIsNone(sl["parent_session_id"])
        self.assertEqual(sl["model"], "claude-opus-4-6")
        self.assertEqual(sl["status"], "running")
        self.assertIsNone(sl["ended_at"])

    def test_new_turn_only_adds_to_slice(self):
        self._backfill(_three_ws_transcript())
        models_before, per_run_before = _usage_totals(self.store)
        cfg, projects, _sessions = _cfg(self.tmp.name, backfill_session=SID)
        with open(os.path.join(projects, "enc", f"{SID}.jsonl"), "ab") as fh:
            fh.write(_usage_line(
                "w11c", model="claude-sonnet-4-6",
                text="WS11.9 follow-up",
                tools=[("Read", {"file_path": "lor-wt-ws11-evid-docs/more.md"})],
                tokens={"output": 7_000}))
        os.utime(os.path.join(projects, "enc", f"{SID}.jsonl"), (NOW + 10, NOW + 10))
        claude._split_maintained.clear()
        claude.poll(self.store, cfg, NOW + 10)
        models_after, per_run_after = _usage_totals(self.store)
        new_turn = {"input": 10, "output": 7_000, "cache_read": 30,
                    "cache_write": 40, "reasoning": 0}
        for model, cell in models_before.items():
            for kind, value in cell.items():
                delta = new_turn[kind] if model == "claude-sonnet-4-6" else 0
                self.assertEqual(models_after[model][kind], value + delta)
        slice_id = f"claude-main:{SID}:main@WS11"
        self.assertEqual(per_run_after[slice_id], per_run_before[slice_id] + 7_080)
        total_before = sum(per_run_before.values())
        total_after = sum(per_run_after.values())
        self.assertEqual(total_after, total_before + 7_080)
        runs = [row["run_id"] for row in
                self.store._conn.execute("SELECT run_id FROM runs").fetchall()]
        self.assertEqual(len([r for r in runs if "@WS" in r]), 1)

    def test_restart_reproduces_same_slices(self):
        self._backfill(_three_ws_transcript())
        first_models, first_runs = _usage_totals(self.store)
        first_slices = sorted(
            row["run_id"] for row in
            self.store._conn.execute("SELECT run_id FROM runs WHERE slice_of IS NOT NULL").fetchall()
        )
        claude.reset_state()
        store2 = Store(Path(self.tmp.name) / "dash2.sqlite")
        try:
            self.store.close()
            cfg, projects, _sessions = _cfg(self.tmp.name, backfill_session=SID)
            for index in range(3):
                store2.upsert_run(
                    {
                        "run_id": f"muse:seed{index}:ws9-exec-seed{index}",
                        "source": "muse",
                        "label": f"ws9-exec-seed{index}",
                        "session_id": f"seed-{index}",
                        "parent_session_id": SID,
                        "status": "done",
                        "started_at": NOW - 100,
                        "ended_at": NOW - 50,
                    }
                )
            src = os.path.join(projects, "enc", f"{SID}.jsonl")
            with open(src, "rb") as fh:
                data = fh.read()
            projects2 = os.path.join(self.tmp.name, "p2", "projects")
            os.makedirs(os.path.join(projects2, "enc"), exist_ok=True)
            _write(os.path.join(projects2, "enc", f"{SID}.jsonl"), data, NOW)
            cfg["claude_projects"] = projects2
            claude.poll(store2, cfg, NOW)
            second_models, second_runs = _usage_totals(store2)
            second_slices = sorted(
                row["run_id"] for row in
                store2._conn.execute("SELECT run_id FROM runs WHERE slice_of IS NOT NULL").fetchall()
            )
        finally:
            store2.close()
        self.assertEqual(first_models, second_models)
        self.assertEqual(first_runs, second_runs)
        self.assertEqual(first_slices, second_slices)

    def test_single_workstream_session_unchanged(self):
        data = _usage_line("a", text="ws9-exec-a WS9.1") + _usage_line(
            "b", text="WS9.2 done")
        self._backfill(data, seed_ws9_workers=False)
        runs = [row["run_id"] for row in
                self.store._conn.execute("SELECT run_id FROM runs").fetchall()]
        self.assertEqual(runs, [f"claude-main:{SID}:main"])
        per_model, per_run = _usage_totals(self.store)
        self.assertEqual(sum(per_run.values()), 2 * (10 + 20 + 30 + 40))

    def test_no_signal_inherit_run_then_primary(self):
        # The seed signal is a TARGETING operand (a cd into the
        # worktree); the follow-up turns are bare discussion and
        # inherit it for the bounded run, then fall back to primary.
        lines = [_usage_line(
            "sig", text="ws11-exec-docs start",
            tools=[("Bash", {"command": "cd ~/work/lor-wt-ws11-x",
                             "description": "start"})])]
        for index in range(25):
            lines.append(_usage_line(f"q{index}", text="still discussing"))
        store = MemStore()
        claude.reset_state()
        cfg, projects, _sessions = _cfg(self.tmp.name)
        _write(os.path.join(projects, "enc", f"{SID}.jsonl"), b"".join(lines), NOW)
        claude.poll(store, cfg, NOW)
        splits = {row["message_id"]: row.get("ws_split") for row in store.usage.values()}
        self.assertEqual(splits["sig"], "WS11")
        for index in range(20):
            self.assertEqual(splits[f"q{index}"], "WS11")
        for index in range(20, 25):
            self.assertIsNone(splits[f"q{index}"])

    def test_privacy_no_label_text_in_store(self):
        # The text block is discussion (TEXT, never votes, never
        # stored); the vote comes from the cd operand alone, and only
        # the derived workstream number reaches the store.
        data = _usage_line(
            "p1",
            text="ws11-exec-docs in lor-wt-ws11-topsecret on ws11/branch-x "
            "writes workstreams/WS11/secret-plan.json for WS11.9",
            tools=[("Bash", {"command": "cd ~/work/lor-wt-ws11-topsecret",
                             "description": "peek"})],
        )
        self._backfill(data)
        blobs = []
        for table in ("usage_events", "runs", "sessions"):
            for row in self.store._conn.execute(f"SELECT * FROM {table}").fetchall():
                blobs.append(" ".join("" if value is None else str(value) for value in row))
        haystack = "\n".join(blobs)
        for leaked in ("lor-wt", "workstreams/WS", "ws11/branch", "WS11.9",
                       "prompt-file", "topsecret"):
            self.assertNotIn(leaked, haystack)
        splits = {row["ws_split"] for row in
                  self.store._conn.execute("SELECT ws_split FROM usage_events").fetchall()}
        self.assertTrue(splits <= {"WS11", None})

    def test_lane_link_from_launch_label(self):
        self.store.upsert_run(
            {
                "run_id": "muse:lane1:ws11-exec-docs",
                "source": "muse",
                "label": "ws11-exec-docs",
                "session_id": "lane-session",
                "status": "done",
                "started_at": NOW - 100,
                "ended_at": NOW - 50,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "claude-subagent:other:ws11-exec-docs",
                "source": "claude-subagent",
                "label": "ws11-exec-docs",
                "session_id": "other",
                "parent_session_id": "other",
                "status": "done",
            }
        )
        data = _usage_line(
            "launch",
            text="starting the lane",
            tools=[("Bash", {
                "command": "muse exec --prompt-file /tmp/ws11-exec-docs.md",
                "description": "ws11-exec-docs"})],
        )
        self._backfill(data)
        lane = self.store.get_run("muse:lane1:ws11-exec-docs")
        self.assertEqual(lane["parent_session_id"], SID)
        self.assertEqual(lane["workstream"], "WS11")
        sub = self.store.get_run("claude-subagent:other:ws11-exec-docs")
        self.assertEqual(sub["parent_session_id"], "other")

    def test_slices_classify_as_planning_lead(self):
        from dash.phases import classify_run

        self._backfill(_three_ws_transcript())
        primary_id = f"claude-main:{SID}:main"
        slice_id = f"claude-main:{SID}:main@WS11"
        for run_id in (primary_id, slice_id):
            row = self.store.get_run(run_id)
            phase, rule = classify_run(
                {"label": row["label"], "source": row["source"],
                 "parent_session_id": row["parent_session_id"],
                 "started_at": row["started_at"]},
                {"task_id": None, "in_window": False},
            )
            self.assertEqual((phase, rule), ("planning", "lead"))

    def test_live_agents_neutral_and_ws_scoped(self):
        self._backfill(_three_ws_transcript())
        snap = self.store.snapshot(NOW)
        self.assertEqual(snap["counts"]["live_agents"], 0)
        running_ids = {run["run_id"] for run in snap["running"]}
        self.assertIn(f"claude-main:{SID}:main", running_ids)
        self.assertIn(f"claude-main:{SID}:main@WS11", running_ids)
        ws11 = self.store.query_runs(NOW, "WS11", None, None, 100)
        self.assertIn(f"claude-main:{SID}:main@WS11",
                      {run["run_id"] for run in ws11})
        ws9 = self.store.query_runs(NOW, "WS9", None, None, 100)
        self.assertIn(f"claude-main:{SID}:main", {run["run_id"] for run in ws9})
        cells = self.store.query_usage("workstream", "hour", None, None, None)
        billed = sum(cell["input"] + cell["output"] + cell["cache_read"]
                     + cell["cache_write"] for cell in cells)
        self.assertEqual(billed, 10_160 + 210_160 + 1_080)

    def test_slice_follows_primary_liveness_never_stale(self):
        self._backfill(_three_ws_transcript())
        primary_id = f"claude-main:{SID}:main"
        slice_id = f"claude-main:{SID}:main@WS11"
        # Primary live: the slice shows in the running set too.
        snap = self.store.snapshot(NOW)
        self.assertIn(slice_id, {run["run_id"] for run in snap["running"]})
        # Primary closes (pre-mirror transient): the open slice hides
        # with it and is never reported stale on its own.
        self.store.upsert_run(
            {"run_id": primary_id, "status": "done", "ended_at": NOW + 1}
        )
        stale_ids = {run["run_id"] for run, _reason in
                     self.store.stale_running(NOW + 2)}
        self.assertNotIn(slice_id, stale_ids)
        snap = self.store.snapshot(NOW + 2)
        running_ids = {run["run_id"] for run in snap["running"]}
        self.assertNotIn(slice_id, running_ids)
        self.assertNotIn(primary_id, running_ids)
        self.assertEqual(snap["counts"]["live_agents"], 0)
        mismatches = self.store.reconcile_numbers(NOW + 2, processes=[])
        self.assertEqual(
            [mismatch for mismatch in mismatches["mismatches"] if "@WS" in mismatch],
            [],
        )

    def test_live_child_keeps_quiet_main_running(self):
        cfg, projects, _sessions = _cfg(self.tmp.name)
        _write(os.path.join(projects, "enc", f"{SID}.jsonl"),
               _usage_line("m1", text="quiet work"), NOW - 720)
        self.store.upsert_run(
            {
                "run_id": f"claude-subagent:{SID}:helper",
                "source": "claude-subagent",
                "session_id": SID,
                "parent_session_id": SID,
                "status": "running",
                "started_at": NOW - 100,
            }
        )
        claude.poll(self.store, cfg, NOW)
        run = self.store.get_run(f"claude-main:{SID}:main")
        self.assertEqual(run["status"], "running")
        # The child finishing flips the quiet main to idle without a write.
        self.store.upsert_run(
            {"run_id": f"claude-subagent:{SID}:helper", "status": "done",
             "ended_at": NOW + 1}
        )
        claude.poll(self.store, cfg, NOW + 2)
        run = self.store.get_run(f"claude-main:{SID}:main")
        self.assertEqual(run["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
