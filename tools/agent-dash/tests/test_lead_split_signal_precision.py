"""Lead signal precision: TARGETING vs TEXT field split.

Per-turn workstream signals count only TARGETING fields (the
assistant record's ``cwd`` plus tool-use path operands: file paths,
prompt-file paths and label operands of worker launches, ``cd`` /
``git`` / ``gh`` / ``ssh`` / ``pytest`` targeting operands, bundle
paths). TEXT bodies never vote: assistant text, ``Agent``/``Task``
prompts and descriptions, ``Write``/``Edit`` content bodies, heredoc
bodies and quoted multi-line literals, ``echo``/``printf`` payloads,
search patterns, ``label make``/``label check`` examples, URLs, and
tool results. The same split applies to a subagent's own votes.

Synthetic transcripts only; the live instance check covers the real files.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import lead_split  # noqa: E402
from dash.sources import claude  # noqa: E402
from dash.store import Store  # noqa: E402

SID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
AGENT = "d1234567890abcdef1"
TOOL_USE = "toolu_precision001"
NOW = 1_792_000_000


def _ts(epoch):
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _rec(mid, content, epoch=NOW, cwd="/tmp/example", model="claude-opus-4-6"):
    return {
        "type": "assistant",
        "timestamp": _ts(epoch),
        "sessionId": SID,
        "cwd": cwd,
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


def _line(rec):
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _tool(name, tool_id, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


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


def _totals(store):
    rows = store._conn.execute(
        "SELECT model, input_tokens, output_tokens, cache_read,"
        " cache_write, reasoning FROM usage_events"
    ).fetchall()
    out = {}
    for row in rows:
        cell = out.setdefault(row["model"], [0, 0, 0, 0, 0])
        cell[0] += row["input_tokens"] or 0
        cell[1] += row["output_tokens"] or 0
        cell[2] += row["cache_read"] or 0
        cell[3] += row["cache_write"] or 0
        cell[4] += row["reasoning"] or 0
    return out


class TargetingPureTests(unittest.TestCase):
    def test_heredoc_prompt_body_does_not_vote(self):
        body = "ws4-WS4.1-slug does WS9.1 and WS9.2 work on ws9/notes"
        for marker in ("EOF", "'EOF'", "-EOF"):
            command = (
                "cat > /tmp/notes-prompt.md <<%s\n%s\nEOF\n" % (marker, body)
            )
            rec = _rec("m1", [
                {"type": "text", "text": "writing the prompt"},
                _tool("Bash", "tu-0", {"command": command,
                                       "description": "write prompt"}),
            ])
            order, launches = lead_split.signals_in_turn(rec)
            self.assertEqual((order, launches), ([], []))

    def test_python_heredoc_body_does_not_vote(self):
        command = (
            "python3 - <<'EOF'\nprint('ws4-WS4.1-slug fixes WS9.3')\nEOF\n"
            " && cd ~/work/lor-wt-ws12-foo"
        )
        rec = _rec("m1", [_tool("Bash", "tu-0", {"command": command})])
        order, _ = lead_split.signals_in_turn(rec)
        self.assertEqual(order, ["WS12"])

    def test_agent_prompt_does_not_vote_but_file_path_does(self):
        rec = _rec("m1", [
            {"type": "text", "text": "discussing WS11 while planning"},
            _tool("Agent", "tu-a", {"description": "ws11 helper",
                                    "model": "haiku",
                                    "prompt": "do WS11.3 in lor-wt-ws11-x"}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))
        rec = _rec("m2", [
            _tool("Read", "tu-r",
                  {"file_path": "/tmp/scratchpad/ws11/prompts/ws11-exec-x.md"}),
        ])
        order, launches = lead_split.signals_in_turn(rec)
        self.assertIn("WS11", order)
        self.assertEqual(launches, ["ws11-exec-x"])

    def test_cd_worktree_and_pytest_votes(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": "cd ~/work/lor-wt-ws12-foo && pytest tests/ -q"}),
        ])
        order, _ = lead_split.signals_in_turn(rec)
        self.assertEqual(order, ["WS12"])

    def test_launcher_prompt_file_votes(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": "muse exec --prompt-file /tmp/p/ws11-exec-x.md",
                   "description": "launch"}),
        ])
        order, launches = lead_split.signals_in_turn(rec)
        self.assertEqual(order, ["WS11"])
        self.assertEqual(launches, ["ws11-exec-x"])

    def test_label_make_example_does_not_vote(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": "python3 agent_dash.py label make --scope ws4"
                              " --mark exec --slug ws4-WS4.1-slug"}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))
        rec = _rec("m2", [
            _tool("Bash", "tu-0",
                  {"command": "python3 agent_dash.py label check ws11-visuals"}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))

    def test_grep_and_echo_payloads_do_not_vote(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": 'grep -rn "ws9-" workstreams/WS9/plan.json'}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))
        rec = _rec("m2", [
            _tool("Bash", "tu-0",
                  {"command": 'echo "ws11-exec-x done" && printf "%s" WS9.1'}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))

    def test_pattern_fields_do_not_vote_but_searched_paths_do(self):
        rec = _rec("m1", [
            _tool("ToolSearch", "tu-0",
                  {"query": "ws9-WS9.1-slug", "max_results": 5}),
            _tool("Grep", "tu-1",
                  {"pattern": "ws9-.*", "path": "lor-wt-ws11-x/f"}),
        ])
        order, _ = lead_split.signals_in_turn(rec)
        self.assertEqual(order, ["WS11"])

    def test_write_content_and_descriptions_do_not_vote(self):
        rec = _rec("m1", [
            {"type": "text", "text": "ws11-exec-docs status update"},
            _tool("Write", "tu-w",
                  {"file_path": "/tmp/notes.md",
                   "content": "ws4-WS4.1-slug fixes WS9.1"}),
            _tool("Edit", "tu-e",
                  {"file_path": "/tmp/notes.md",
                   "old_string": "WS11.1", "new_string": "WS11.2"}),
            _tool("Bash", "tu-b",
                  {"command": "true", "description": "ws11-exec-docs"}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))

    def test_quoted_multiline_and_url_query_do_not_vote(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": 'python3 -c "print(\'ws11-exec-x\')"\n'
                              'curl "https://x.test/?label=ws9-WS9.1-a"'}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))
        rec = _rec("m2", [
            _tool("WebFetch", "tu-0",
                  {"url": "https://x.test/?q=ws9-WS9.1",
                   "prompt": "summarize WS11.3"}),
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))

    def test_git_checkout_branch_votes(self):
        rec = _rec("m1", [
            _tool("Bash", "tu-0",
                  {"command": "git checkout ws10/tiny && git status"}),
        ])
        order, _ = lead_split.signals_in_turn(rec)
        self.assertEqual(order, ["WS10"])

    def test_tool_result_lines_never_vote(self):
        user = (b'{"type":"user","message":{"role":"user","content":['
                b'{"type":"tool_result","tool_use_id":"tu-0",'
                b'"content":"ws11-exec-docs for WS11.3"}]}}')
        self.assertEqual(lead_split.signals_in_line(user), ([], []))
        blob = b'{"type":"api-request-blob","data":"ws11-exec-docs"}'
        self.assertEqual(lead_split.signals_in_line(blob), ([], []))
        self.assertEqual(lead_split.signals_in_line(b"not json"), ([], []))

    def test_bare_discussion_text_never_votes(self):
        rec = _rec("m1", [
            {"type": "text",
             "text": "main@WS4 and main@WS7 came up while discussing phase rules"},
        ])
        self.assertEqual(lead_split.signals_in_turn(rec), ([], []))


class VersionRebuildTests(unittest.TestCase):
    def setUp(self):
        claude.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        claude.reset_state()

    def _transcript(self):
        return (
            _line(_rec("t1", [
                {"type": "text", "text": "starting WS11"},
                _tool("Bash", "tu-1", {"command": "cd ~/work/lor-wt-ws11-a"}),
            ]))
            + _line(_rec("t2", [
                _tool("Read", "tu-2",
                      {"file_path": "workstreams/WS11/plan.json"}),
            ], model="claude-sonnet-4-6"))
        )

    def _backfill(self):
        cfg, projects, _ = _cfg(self.tmp.name, backfill_session=SID)
        _write(os.path.join(projects, "enc", SID + ".jsonl"),
               self._transcript(), NOW)
        claude.poll(self.store, cfg, NOW)
        return cfg

    def _ws_splits(self):
        return {
            row["message_id"]: row["ws_split"]
            for row in self.store._conn.execute(
                "SELECT message_id, ws_split FROM usage_events"
                " WHERE session_id=?", (SID,))
        }

    def test_version_bump_recomputes(self):
        self._backfill()
        before = self._ws_splits()
        self.assertEqual(before, {"t1": "WS11", "t2": "WS11"})
        self.assertEqual(self.store.split_rules_version(SID),
                         lead_split.SIGNAL_RULES_VERSION)
        # Simulate stored state from the old rules: stale marker plus
        # a bogus per-turn vote planted by the old matcher.
        self.store._conn.execute(
            "UPDATE meta SET value='1' WHERE key=?",
            (f"lead_signal_rules:{SID}",),
        )
        self.store._conn.execute(
            "UPDATE usage_events SET ws_split='WS4' WHERE message_id='t2'"
        )
        self.store._conn.commit()
        cfg, _, _ = _cfg(self.tmp.name, backfill_session=SID)
        claude.poll(self.store, cfg, NOW + 10)
        self.assertEqual(self.store.split_rules_version(SID),
                         lead_split.SIGNAL_RULES_VERSION)
        # The bogus vote is gone: the same bytes replayed under the
        # current rules.
        self.assertEqual(self._ws_splits(), before)

    def test_rebuild_conserves_tokens(self):
        self._backfill()
        before = _totals(self.store)
        self.store._conn.execute(
            "UPDATE meta SET value='1' WHERE key=?",
            (f"lead_signal_rules:{SID}",),
        )
        self.store._conn.commit()
        cfg, _, _ = _cfg(self.tmp.name, backfill_session=SID)
        claude.poll(self.store, cfg, NOW + 10)
        self.assertEqual(_totals(self.store), before)
        rows = self.store._conn.execute(
            "SELECT run_id FROM runs WHERE slice_of IS NOT NULL").fetchall()
        self.assertEqual(rows, [])

    def test_restart_reproduces(self):
        self._backfill()
        first_splits = self._ws_splits()
        first_totals = _totals(self.store)
        claude.reset_state()
        store2 = Store(Path(self.tmp.name) / "dash2.sqlite")
        try:
            cfg, projects, _ = _cfg(self.tmp.name, backfill_session=SID)
            src = os.path.join(projects, "enc", SID + ".jsonl")
            with open(src, "rb") as fh:
                data = fh.read()
            projects2 = os.path.join(self.tmp.name, "p2", "projects")
            os.makedirs(os.path.join(projects2, "enc"), exist_ok=True)
            _write(os.path.join(projects2, "enc", SID + ".jsonl"), data, NOW)
            cfg["claude_projects"] = projects2
            claude.poll(store2, cfg, NOW)
            second_splits = {
                row["message_id"]: row["ws_split"]
                for row in store2._conn.execute(
                    "SELECT message_id, ws_split FROM usage_events"
                    " WHERE session_id=?", (SID,))
            }
            self.assertEqual(second_splits, first_splits)
            self.assertEqual(_totals(store2), first_totals)
        finally:
            store2.close()

    def test_privacy_no_text_in_store(self):
        secret = "topsecret-plan for WS11.9 in lor-wt-ws11-hideaway"
        data = _line(_rec("p1", [
            {"type": "text", "text": secret},
            _tool("Agent", "tu-a",
                  {"description": "helper", "model": "haiku", "prompt": secret}),
            _tool("Write", "tu-w",
                  {"file_path": "/tmp/notes.md", "content": secret}),
            _tool("Bash", "tu-b",
                  {"command": "cd ~/work/lor-wt-ws11-hideaway"}),
        ]))
        cfg, projects, _ = _cfg(self.tmp.name, backfill_session=SID)
        _write(os.path.join(projects, "enc", SID + ".jsonl"), data, NOW)
        claude.poll(self.store, cfg, NOW)
        self.assertEqual(self._ws_splits(), {"p1": "WS11"})
        blobs = []
        for table in ("usage_events", "runs", "sessions", "subagent_spawn",
                      "subagent_votes", "file_offsets"):
            try:
                rows = self.store._conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                continue
            for row in rows:
                blobs.append(" ".join("" if v is None else str(v) for v in row))
        haystack = "\n".join(blobs)
        for leaked in ("topsecret-plan", "lor-wt", "workstreams/WS",
                       "WS11.9", "notes.md"):
            self.assertNotIn(leaked, haystack)

    def test_subagent_own_votes_use_targeting_split(self):
        main = _line(_rec("m-spawn", [
            {"type": "text", "text": "spawn a helper for WS9.4"},
            _tool("Agent", TOOL_USE,
                  {"description": "helper", "model": "haiku",
                   "prompt": "fix WS9.4 prices"}),
        ]))
        result = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": TOOL_USE,
                             "content": [{"type": "text",
                                           "text": "Async agent launched.\n"
                                                   "agentId: " + AGENT}]}],
            },
        }
        main += json.dumps(result, separators=(",", ":")).encode() + b"\n"
        sub = _line(_rec("s1", [
            {"type": "text", "text": "checking WS9.4 prices and WS9.5"},
            _tool("Read", "tu-r",
                  {"file_path": "lor-wt-ws11-x/catalog.md"}),
        ], model="claude-haiku-4-5"))
        cfg, projects, _ = _cfg(self.tmp.name)
        _write(os.path.join(projects, "enc", SID + ".jsonl"), main, NOW)
        subdir = os.path.join(projects, "enc", SID, "subagents")
        _write(os.path.join(subdir, "agent-" + AGENT + ".jsonl"), sub, NOW)
        _write(os.path.join(subdir, "agent-" + AGENT + ".meta.json"),
               json.dumps({"agentType": "general-purpose",
                           "description": "helper",
                           "toolUseId": TOOL_USE, "model": "haiku"}), NOW)
        poll_cfg, _, _ = _cfg(self.tmp.name, backfill_session=SID)
        poll_cfg["claude_projects"] = projects
        claude.poll(self.store, poll_cfg, NOW)
        # Spawn turn and subagent text name WS9, but both are TEXT:
        # the only vote is the subagent's Read, so it takes WS11.
        run = self.store.get_run(f"claude-subagent:{SID}:{AGENT}")
        self.assertEqual(run["workstream"], "WS11")
        self.assertEqual(run["ws_confidence"], "inherited")


if __name__ == "__main__":
    unittest.main()
