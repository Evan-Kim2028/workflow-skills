"""Claude Code subagent workstream inheritance (spawn-turn rule).

A subagent (``claude-subagent:<sid>:<agent>`` transcripts under
``<proj>/<sid>/subagents/agent-<id>.jsonl``) takes the workstream of
the main-session turn that spawned it: the assistant turn whose Agent
tool_use id matches the subagent sidecar's ``toolUseId`` (and the
parent tool result's ``agentId``). Fallback order when the spawning
turn carries no signal: the majority of the subagent's own signals,
then the split-cursor value at the spawn, else unassigned. Confidence
is always ``inherited``; explicit label attributions still win.

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
NOW = 1_792_000_000
AGENT = "aeab1b76d59942e27"
TOOL_USE = "toolu_01HeYBpWit4aFs4AyFmQLxgt"


def _ts(epoch):
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _usage_line(
    mid,
    *,
    text="working",
    tools=(),
    epoch=NOW,
    model="claude-opus-4-6",
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
    rec = {
        "type": "assistant",
        "timestamp": _ts(epoch),
        "sessionId": SID,
        "cwd": "/tmp/example",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _spawn_turn(mid, tool_use_id, text, description="do the thing", cmd=None):
    """An assistant turn spawning one Agent subagent.

    The text and the Agent prompt are TEXT and never vote; pass
    ``cmd`` for a Bash targeting operand when the spawning turn
    itself should carry the signal.
    """
    content = [
        {"type": "text", "text": text},
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Agent",
            "input": {"description": description, "model": "haiku", "prompt": text},
        },
    ]
    if cmd is not None:
        content.append(
            {
                "type": "tool_use",
                "id": tool_use_id + "-bash",
                "name": "Bash",
                "input": {"command": cmd, "description": description},
            }
        )
    rec = {
        "type": "assistant",
        "timestamp": _ts(NOW),
        "sessionId": SID,
        "cwd": "/tmp/example",
        "message": {
            "id": mid,
            "model": "claude-opus-4-6",
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _spawn_result(tool_use_id, agent_id):
    rec = {
        "type": "user",
        "timestamp": _ts(NOW),
        "sessionId": SID,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [
                        {
                            "type": "text",
                            "text": "Async agent launched.\nagentId: " + agent_id,
                        }
                    ],
                }
            ],
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _sub_line(mid, text, epoch=NOW, tools=()):
    # Pass ``tools`` for TARGETING operands (Read paths, Bash
    # commands); bare text is discussion and never votes.
    return _usage_line(mid, text=text, model="claude-haiku-4-5", epoch=epoch,
                       tools=tools)


def _write(path, data, mtime=NOW):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))


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


def _backfill(store, projects, sid, main_data, subs, backfill_sid=None):
    """Write main + subagent files, poll once, return the run rows."""
    if main_data is not None:
        _write(os.path.join(projects, "enc", f"{sid}.jsonl"), main_data, NOW)
    for agent_id, data, tool_use_id in subs:
        subdir = os.path.join(projects, "enc", sid, "subagents")
        _write(os.path.join(subdir, f"agent-{agent_id}.jsonl"), data, NOW)
        _write(
            os.path.join(subdir, f"agent-{agent_id}.meta.json"),
            json.dumps(
                {
                    "agentType": "general-purpose",
                    "description": "helper",
                    "toolUseId": tool_use_id,
                    "model": "haiku",
                }
            ),
            NOW,
        )
    cfg, _, _ = _cfg(os.path.dirname(projects), backfill_session=backfill_sid or sid)
    cfg["claude_projects"] = projects
    claude.poll(store, cfg, NOW)
    return store


def _totals(store):
    rows = store._conn.execute(
        "SELECT run_id, input_tokens, output_tokens, cache_read,"
        " cache_write, reasoning FROM usage_events"
    ).fetchall()
    per_run: dict = {}
    for row in rows:
        per_run[row["run_id"]] = per_run.get(row["run_id"], 0) + (
            (row["input_tokens"] or 0)
            + (row["output_tokens"] or 0)
            + (row["cache_read"] or 0)
            + (row["cache_write"] or 0)
        )
    return per_run


class PureTests(unittest.TestCase):
    def test_parse_subagent_run(self):
        self.assertEqual(
            lead_split.parse_subagent_run(f"claude-subagent:{SID}:{AGENT}"),
            (SID, AGENT),
        )
        self.assertIsNone(lead_split.parse_subagent_run(f"claude-main:{SID}:main"))
        self.assertIsNone(
            lead_split.parse_subagent_run("claude-workflow:S:wf_x/a")
        )
        self.assertIsNone(lead_split.parse_subagent_run("claude-subagent:only-one"))
        self.assertIsNone(lead_split.parse_subagent_run(None))

    def test_pick_counts_majority_then_first(self):
        self.assertEqual(
            lead_split.pick_counts({"WS10": [1, 0], "WS11": [2, 1]}), "WS11"
        )
        # Tie breaks by first appearance.
        self.assertEqual(
            lead_split.pick_counts({"WS10": [2, 5], "WS11": [2, 1]}), "WS11"
        )
        self.assertIsNone(lead_split.pick_counts({}))
        self.assertIsNone(lead_split.pick_counts({"nope": [3, 0]}))
        self.assertIsNone(lead_split.pick_counts(None))


class SpawnLinkTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        claude.reset_state()

    def test_spawned_from_ws11_turn(self):
        # The spawn turn's signal is its cd operand; the Agent prompt
        # naming WS11 is TEXT and does not vote.
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
            cmd="cd ~/work/lor-wt-ws11-a",
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line("s1", "helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")
        self.assertEqual(run["ws_confidence"], "inherited")

    def test_no_signal_turn_inherits_cursor(self):
        main = (
            _usage_line("m-ws12", text="ws12-exec-docs start WS12.1",
                        tools=[("Bash", {"command": "cd ~/work/lor-wt-ws12-x"})])
            + _spawn_turn("m-spawn", TOOL_USE, "spawn a helper, no signal here")
            + _spawn_result(TOOL_USE, AGENT)
        )
        subs = [(AGENT, _sub_line("s1", "helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS12")
        self.assertEqual(run["ws_confidence"], "inherited")

    def test_multi_signal_spawn_turn_dominant_wins(self):
        # Task ids in the prompt are TEXT; the dominant vote comes
        # from the turn's targeting operands (two WS12 reads, one WS11).
        main = _spawn_turn(
            "m-spawn",
            TOOL_USE,
            "WS11.1 plus WS12.1 plus WS12.2 work",
            cmd="cd ~/work/lor-wt-ws12-a && cat workstreams/WS12/plan.json"
            " && cat workstreams/WS11/plan.json",
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line("s1", "helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS12")

    def test_own_signals_beat_cursor_fallback(self):
        main = (
            _usage_line("m-ws12", text="ws12-exec-docs start WS12.1",
                        tools=[("Bash", {"command": "cd ~/work/lor-wt-ws12-x"})])
            + _spawn_turn("m-spawn", TOOL_USE, "spawn a helper, no signal here")
            + _spawn_result(TOOL_USE, AGENT)
        )
        sub = _sub_line(
            "s1", "checking lor-wt-ws11-x and WS11.4",
            tools=[("Read", {"file_path": "lor-wt-ws11-x/notes.md"})])
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, [(AGENT, sub, TOOL_USE)])
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")
        self.assertEqual(run["ws_confidence"], "inherited")

    def test_spawn_turn_signal_beats_own_signals(self):
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
            cmd="cd ~/work/lor-wt-ws11-a",
        ) + _spawn_result(TOOL_USE, AGENT)
        sub = _sub_line(
            "s1", "checking lor-wt-ws12-x and WS12.4",
            tools=[("Read", {"file_path": "lor-wt-ws12-x/notes.md"})])
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, [(AGENT, sub, TOOL_USE)])
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")

    def test_unassigned_without_any_signal(self):
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "spawn a helper, no signal anywhere"
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line("s1", "plain helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertIsNone(run["workstream"])

    def test_explicit_label_still_wins(self):
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3"
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line("s1", "helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        # The sidecar description is the run label; an explicit ws
        # label there beats the spawning turn.
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"), main, NOW
        )
        subdir = os.path.join(projects, "enc", SID, "subagents")
        _write(os.path.join(subdir, f"agent-{AGENT}.jsonl"), subs[0][1], NOW)
        _write(
            os.path.join(subdir, f"agent-{AGENT}.meta.json"),
            json.dumps(
                {
                    "agentType": "general-purpose",
                    "description": "ws9-exec-docs",
                    "toolUseId": TOOL_USE,
                    "model": "haiku",
                }
            ),
            NOW,
        )
        poll_cfg, _, _ = _cfg(self.tmp.name, backfill_session=SID)
        poll_cfg["claude_projects"] = projects
        claude.poll(self.store, poll_cfg, NOW)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["label"], "ws9-exec-docs")
        self.assertEqual((run["workstream"], run["ws_confidence"]), ("WS9", "regex"))

    def test_parent_arrives_after_child(self):
        cfg, projects, _ = _cfg(self.tmp.name)
        # Child first: the parent turn is not parsed yet.
        _backfill(
            self.store, projects, SID, None, [(AGENT, _sub_line("s1", "work"), TOOL_USE)]
        )
        first = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertIsNone(first["workstream"])
        # Then the parent turn arrives; the subagent resolves retroactively.
        _write(
            os.path.join(projects, "enc", f"{SID}.jsonl"),
            _spawn_turn("m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
                        cmd="cd ~/work/lor-wt-ws11-a")
            + _spawn_result(TOOL_USE, AGENT),
            NOW + 10,
        )
        claude._split_maintained.clear()
        claude.poll(self.store, cfg, NOW + 10)
        after = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(
            (after["workstream"], after["ws_confidence"]), ("WS11", "inherited")
        )
        # Usage counted once: one child turn, no duplication.
        per_run = _totals(self.store)
        self.assertEqual(per_run[f"claude-subagent:{SID}:{AGENT}"], 10 + 20 + 30 + 40)

    def test_conservation_only_columns_move(self):
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
            cmd="cd ~/work/lor-wt-ws11-a",
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line("s1", "helper work"), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        before = _totals(self.store)
        # Re-poll the same bytes: totals identical, attribution stable.
        claude._split_maintained.clear()
        claude.poll(self.store, cfg, NOW + 10)
        after = _totals(self.store)
        self.assertEqual(before, after)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")

    def test_restart_reproduces(self):
        main = _spawn_turn(
            "m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
            cmd="cd ~/work/lor-wt-ws11-a",
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line(
            "s1", "checking lor-wt-ws11-x",
            tools=[("Read", {"file_path": "lor-wt-ws11-x/notes.md"})]), TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        first_ws = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")["workstream"]
        first_totals = _totals(self.store)
        first_votes = self.store._conn.execute(
            "SELECT votes_json FROM subagent_votes WHERE run_id=?",
            (f"claude-subagent:{SID}:{AGENT}",),
        ).fetchone()[0]
        claude.reset_state()
        store2 = Store(Path(self.tmp.name) / "dash2.sqlite")
        try:
            _backfill(store2, projects, SID, main, subs)
            second = store2.get_run(f"claude-subagent:{SID}:{AGENT}")
            self.assertEqual(second["workstream"], first_ws)
            self.assertEqual(
                second["ws_confidence"],
                self.store.get_run(f"claude-subagent:{SID}:{AGENT}")["ws_confidence"],
            )
            self.assertEqual(_totals(store2), first_totals)
            votes2 = store2._conn.execute(
                "SELECT votes_json FROM subagent_votes WHERE run_id=?",
                (f"claude-subagent:{SID}:{AGENT}",),
            ).fetchone()[0]
            self.assertEqual(votes2, first_votes)
        finally:
            store2.close()

    def test_privacy_no_prompt_text_in_store(self):
        # The secret lives in TEXT bodies (prompt, subagent text) and
        # never reaches the store; the votes come from the targeting
        # operands alone.
        secret = "topsecret-plan for WS11.9 in lor-wt-ws11-hideaway"
        main = _spawn_turn(
            "m-spawn", TOOL_USE, secret, cmd="cd ~/work/lor-wt-ws11-hideaway",
        ) + _spawn_result(TOOL_USE, AGENT)
        subs = [(AGENT, _sub_line(
            "s1", "checks " + secret,
            tools=[("Read", {"file_path": "lor-wt-ws11-hideaway/notes.md"})]),
                 TOOL_USE)]
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, subs)
        blobs = []
        for table in ("usage_events", "runs", "sessions", "subagent_spawn", "subagent_votes"):
            try:
                rows = self.store._conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                continue
            for row in rows:
                blobs.append(" ".join("" if value is None else str(value) for value in row))
        haystack = "\n".join(blobs)
        for leaked in ("topsecret-plan", "lor-wt", "workstreams/WS", "WS11.9"):
            self.assertNotIn(leaked, haystack)
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")

    def test_single_run_not_sliced(self):
        # Spawn turn votes WS11 via its cd operand; the subagent's own
        # multi-signal Reads agree (majority WS11) and still never
        # slice the one run.
        main = (
            _spawn_turn("m-spawn", TOOL_USE, "launching the WS11 lane for WS11.3",
                        cmd="cd ~/work/lor-wt-ws11-a")
            + _spawn_result(TOOL_USE, AGENT)
            + _usage_line("m-ws11b", text="more WS11.4 work",
                          tools=[("Bash", {"command": "cd ~/work/lor-wt-ws11-b"})])
        )
        sub = _sub_line(
            "s1", "WS11.1 then WS12.9 then WS11.2",
            tools=[("Read", {"file_path": "lor-wt-ws11-a/one.md"}),
                   ("Read", {"file_path": "lor-wt-ws12-x/two.md"}),
                   ("Read", {"file_path": "lor-wt-ws11-a/three.md"})])
        cfg, projects, _ = _cfg(self.tmp.name)
        _backfill(self.store, projects, SID, main, [(AGENT, sub, TOOL_USE)])
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        # One run, one workstream: the spawning turn votes WS11, so the
        # run takes WS11 whole — a multi-signal subagent is never
        # sliced per turn.
        self.assertEqual(run["workstream"], "WS11")
        sliced = [
            row["run_id"]
            for row in self.store._conn.execute("SELECT run_id FROM runs").fetchall()
            if "@WS" in row["run_id"]
        ]
        self.assertEqual(sliced, [])


if __name__ == "__main__":
    unittest.main()
