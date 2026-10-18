"""Tests for dash/sources/muse.py. Synthetic fixtures plus one bounded smoke."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("dash_sources_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


muse = load("muse")

SID = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"
HUB = "00000000-0000-0000-0000-000000000002"

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
            cur[key] = value
        self.runs[row["run_id"]] = cur

    def add_usage(self, row):
        self.usage_n += 1
        key = (row["tool"], row["session_id"], row["message_id"])
        self.usage[key] = dict(row)

    def upsert_session(self, row):
        cur = dict(self.sessions.get(row["session_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.sessions[row["session_id"]] = cur

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def reassign_run(self, src, dst):
        if src not in self.runs or dst not in self.runs or src == dst:
            return False
        source = self.runs[src]
        target = self.runs[dst]
        for key in ("model", "started_at", "clock_source"):
            if not target.get(key) and source.get(key):
                target[key] = source[key]
        for row in self.usage.values():
            if row.get("run_id") == src:
                row["run_id"] = dst
        del self.runs[src]
        return True

    def repair_muse_orphans(self):
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from dash.store import muse_orphan_pairs

        moved = 0
        for src, dst in muse_orphan_pairs(list(self.runs.values())):
            if self.reassign_run(src, dst):
                moved += 1
        return moved


def mc(sid, rec_id, **fields):
    usage = {
        "input_tokens": fields.get("input", 0),
        "output_tokens": fields.get("output", 0),
        "cached_tokens": fields.get("cached", 0),
        "cache_read_tokens": fields.get("cache_read", 0),
        "cache_write_tokens": fields.get("cache_write", 0),
        "reasoning_tokens": fields.get("reasoning", 0),
    }
    body = {
        "recorded_at": fields.get("recorded_at", 1792152018681696),
        "stream": {"kind": "session", "id": sid},
        "payload": {
            "event": {
                "kind": "model_completed",
                "model": fields.get("model", "muse-1"),
                "duration_ms": 10,
                "usage": usage,
            }
        },
    }
    if rec_id is not None:
        body["id"] = rec_id
    if "sequence" in fields:
        body["sequence"] = fields["sequence"]
    return (json.dumps(body) + "\n").encode()


def summed(store, sid=None):
    total = {k: 0 for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    for row in store.usage.values():
        if sid is not None and row["session_id"] != sid:
            continue
        for key in total:
            total[key] += row[key] or 0
    return total


class MuseTests(unittest.TestCase):
    def setUp(self):
        muse._FRESH.clear()
        muse._LOCATE.clear()
        muse.last_error = None
        self._live = muse.live_agent_processes
        muse.live_agent_processes = lambda now=None: []

    def tearDown(self):
        muse.live_agent_processes = self._live

    def poll(self, store, cfg, now, quiet=True):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            muse.poll(store, cfg, now)
        if quiet:
            self.assertEqual(buf.getvalue(), "")
        return buf.getvalue()

    def cfg(self, sessions, scratch, **extra):
        box = {
            "roots": {"muse_sessions": sessions, "scratchpads": scratch},
            "fresh_hours": 0,
            "read_budget": {"per_file": 1024 * 1024, "per_tick": 4 * 1024 * 1024},
            # Isolate from the machine's registered run folders: without this,
            # scan_roots picks up live run-dirs.txt entries. Never created.
            "state_dir": os.path.join(
                os.path.dirname(os.path.abspath(scratch)), "dash-state"
            ),
        }
        box.update(extra)
        return box

    def test_manifest_maps_usage_and_keeps_cache_separate(self):
        self.assertEqual(muse.NAME, "muse")
        self.assertEqual(muse.INTERVAL_S, 2)
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            pads = os.path.join(tmp, "pads")
            scratch = os.path.join(pads, "encoded", HUB, "scratchpad")
            os.makedirs(scratch)
            path = os.path.join(sessions, "2026", "10", "16", SID, "session.jsonl")
            os.makedirs(os.path.dirname(path))
            decoy = {
                "id": "decoy",
                "payload": {"event": {"kind": "started", "note": "model_completed"}},
            }
            blob = (json.dumps(decoy) + "\n").encode()
            blob += mc(
                SID, "rec-a", input=100, output=4, cached=10, cache_read=3,
                cache_write=4, reasoning=6, recorded_at=1792152018681696,
            )
            blob += mc(
                SID, "rec-b", input=7, output=1, cached=2, cache_read=8,
                cache_write=1, reasoning=1,
            )
            blob += mc(SID, None, sequence=9, input=1, output=1, cached=0, cache_read=0)
            with open(path, "wb") as fh:
                fh.write(blob)
            manifest = (
                "# comment\n"
                "\n"
                "label\tsession_id\tstarted_epoch\n"
                "junk\n"
                "nope\tnot-a-uuid\t1\n"
                "ws7-notes\t%s\t1792152010\n" % SID
            )
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write(manifest)
            store = Store()
            now = 1792152100.0
            self.poll(store, self.cfg(sessions, pads), now)
            run_id = "muse:%s:ws7-notes" % SID
            self.assertIn(run_id, store.runs)
            run = store.runs[run_id]
            self.assertEqual(run["source"], "muse")
            self.assertEqual(run["tier"], "muse")
            self.assertEqual(run["label"], "ws7-notes")
            self.assertEqual(run["session_id"], SID)
            self.assertEqual(run["parent_session_id"], HUB)
            self.assertEqual(run["model"], "muse-1")
            self.assertEqual(run["started_at"], 1792152010.0)
            self.assertEqual(run["clock_source"], "manifest")
            self.assertNotIn("status", run)
            self.assertNotIn("verdict", run)
            self.assertNotIn("out_bytes", run)
            self.assertEqual(store.sessions[SID]["tool"], "muse")
            self.assertEqual(store.sessions[SID]["label"], "ws7-notes")
            total = summed(store, SID)
            self.assertEqual(total["input"], 91)
            self.assertEqual(total["cache_read"], 18)
            self.assertEqual(total["cache_write"], 5)
            self.assertEqual(total["reasoning"], 7)
            self.assertEqual(total["output"], 6)
            rows = [row for row in store.usage.values() if row["session_id"] == SID]
            self.assertEqual(len(rows), 3)
            by_id = {row["message_id"]: row for row in rows}
            self.assertEqual(by_id["rec-a"]["cache_read"], 10)
            self.assertEqual(by_id["rec-b"]["cache_read"], 8)
            self.assertEqual(by_id["seq:9"]["input"], 1)
            self.assertAlmostEqual(by_id["rec-a"]["ts"], 1792152018.681696)
            self.assertEqual(by_id["rec-a"]["run_id"], run_id)
            self.assertEqual(by_id["rec-a"]["tool"], "muse")
            calls = store.usage_n
            self.poll(store, self.cfg(sessions, pads), now + 5)
            self.assertEqual(store.usage_n, calls)

    def test_manifest_without_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws7-notes\t%s\t1792152010\tmuse-spark-1.3-contributor\n" % SID)
            store = Store()
            cfg = self.cfg(os.path.join(tmp, "missing-sessions"), scratch)
            cfg["roots"] = {"muse_sessions": cfg["roots"]["muse_sessions"], "scratch": scratch}
            cfg["read_budgets"] = cfg.pop("read_budget")
            self.poll(store, cfg, 1792153000)
            run = store.runs["muse:%s:ws7-notes" % SID]
            self.assertEqual(run["clock_source"], "manifest")
            self.assertEqual(run["started_at"], 1792152010.0)
            self.assertEqual(run["model"], "muse-spark-1.3-contributor")
            self.assertEqual(store.usage, {})

    def test_live_model_fills_run_without_log_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws7-notes\t%s\t1792152010\n" % SID)
            store = Store()
            cfg = self.cfg(os.path.join(tmp, "missing-sessions"), scratch)
            cfg["roots"] = {"muse_sessions": cfg["roots"]["muse_sessions"], "scratch": scratch}
            cfg["read_budgets"] = cfg.pop("read_budget")
            original = muse.live_agent_processes
            muse.live_agent_processes = lambda now=None: [
                {
                    "pid": 42,
                    "comm": "muse",
                    "started_at": 1792152010.0,
                    "label": None,
                    "prompt_label": None,
                    "session_id": SID,
                    "model": "muse-spark-live",
                }
            ]
            try:
                self.poll(store, cfg, 1792153000)
            finally:
                muse.live_agent_processes = original
            run = store.runs["muse:%s:ws7-notes" % SID]
            self.assertEqual(run["model"], "muse-spark-live")
            self.assertEqual(run["status"], "running")

    def test_log_model_beats_live_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, scratch, path = self._one_session(tmp, SID)
            with open(path, "wb") as fh:
                fh.write(mc(SID, "a", input=3, output=1, model="muse-from-log"))
            store = Store()
            original = muse.live_agent_processes
            muse.live_agent_processes = lambda now=None: [
                {
                    "pid": 42,
                    "comm": "muse",
                    "started_at": 1792152010.0,
                    "label": None,
                    "prompt_label": None,
                    "session_id": SID,
                    "model": "muse-spark-live",
                }
            ]
            try:
                self.poll(store, self.cfg(sessions, scratch), 1792153000)
            finally:
                muse.live_agent_processes = original
            run = store.runs["muse:%s:ws7-notes" % SID]
            self.assertEqual(run["model"], "muse-from-log")

    def test_incremental_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, scratch, path = self._one_session(tmp, SID)
            with open(path, "wb") as fh:
                fh.write(mc(SID, "a", input=3, output=1))
            store = Store()
            cfg = self.cfg(sessions, scratch)
            self.poll(store, cfg, 1792153000)
            self.assertEqual(summed(store)["input"], 3)
            with open(path, "ab") as fh:
                fh.write(mc(SID, "b", input=4, output=1))
            self.poll(store, cfg, 1792153002)
            self.assertEqual(summed(store)["input"], 7)
            self.assertEqual(store.usage_n, 2)
            self.assertEqual(store.offsets[os.path.abspath(path)][1], os.path.getsize(path))

    def test_partial_line_held_until_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, scratch, path = self._one_session(tmp, SID)
            line = mc(SID, "a", input=5, output=1)
            with open(path, "wb") as fh:
                fh.write(line[:-1])
            store = Store()
            cfg = self.cfg(sessions, scratch)
            self.poll(store, cfg, 1792153000)
            self.assertEqual(store.usage, {})
            self.assertNotIn(os.path.abspath(path), store.offsets)
            with open(path, "ab") as fh:
                fh.write(b"\n")
            self.poll(store, cfg, 1792153002)
            self.assertEqual(summed(store)["input"], 5)

    def test_shrink_rereads_from_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, scratch, path = self._one_session(tmp, SID)
            with open(path, "wb") as fh:
                fh.write(mc(SID, "a", input=1, output=3))
                fh.write(mc(SID, "b", input=1, output=4))
            store = Store()
            cfg = self.cfg(sessions, scratch)
            self.poll(store, cfg, 1792153000)
            with open(path, "wb") as fh:
                fh.write(mc(SID, "a", input=1, output=9))
            self.poll(store, cfg, 1792153002)
            row = store.usage[("muse", SID, "a")]
            self.assertEqual(row["output"], 9)
            self.assertEqual(store.offsets[os.path.abspath(path)][1], os.path.getsize(path))

    def test_budget_defers_second_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            p0 = os.path.join(sessions, "2026", "10", "16", SID, "session.jsonl")
            p1 = os.path.join(sessions, "2026", "10", "16", SID2, "session.jsonl")
            os.makedirs(os.path.dirname(p0))
            os.makedirs(os.path.dirname(p1))
            with open(p0, "wb") as fh:
                fh.write(mc(SID, "a", input=11, output=1))
            with open(p1, "wb") as fh:
                fh.write(mc(SID2, "b", input=22, output=1))
            man = os.path.join(scratch, "muse-sessions.tsv")
            with open(man, "w") as fh:
                fh.write("one\t%s\t10\ntwo\t%s\t11\n" % (SID, SID2))
            per_tick = os.path.getsize(man) + os.path.getsize(p0)
            per_file = max(os.path.getsize(p0), os.path.getsize(p1), os.path.getsize(man))
            store = Store()
            cfg = self.cfg(sessions, scratch)
            cfg["read_budget"] = {"per_file": per_file, "per_tick": per_tick}
            self.poll(store, cfg, 1792153000)
            self.assertEqual(summed(store, SID)["input"], 11)
            self.assertEqual(summed(store, SID2)["input"], 0)
            cfg["read_budget"] = {"per_file": per_file, "per_tick": 1024 * 1024}
            self.poll(store, cfg, 1792153002)
            self.assertEqual(summed(store, SID2)["input"], 22)
            self.assertEqual(summed(store, SID)["input"], 11)

    def test_unknown_label_uses_session_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            path = os.path.join(
                sessions,
                "%04d" % day.year,
                "%02d" % day.month,
                "%02d" % day.day,
                sid,
                "session.jsonl",
            )
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as fh:
                fh.write(mc(sid, "a", input=6, output=1, recorded_at=int(now * 1_000_000)))
            now = time.time()
            store = Store()
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0)
            self.poll(store, cfg, now)
            run_id = "muse:%s:%s" % (sid, sid)
            self.assertIn(run_id, store.runs)
            run = store.runs[run_id]
            self.assertNotIn("label", run)
            self.assertEqual(run["session_id"], sid)
            self.assertEqual(run["clock_source"], "muse_log")
            self.assertEqual(summed(store, sid)["input"], 6)

    def test_subagent_is_its_own_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            pads = os.path.join(tmp, "pads")
            scratch = os.path.join(pads, "encoded", HUB, "scratchpad")
            os.makedirs(scratch)
            parent = os.path.join(sessions, "2026", "10", "16", SID, "session.jsonl")
            child = os.path.join(sessions, "2026", "10", "16", SID, "subagent", CHILD, "session.jsonl")
            os.makedirs(os.path.dirname(child))
            with open(parent, "wb") as fh:
                fh.write(mc(SID, "p", input=10, output=1))
            with open(child, "wb") as fh:
                fh.write(mc(CHILD, "c", input=4, output=2, model="muse-1"))
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws7-notes\t%s\t1792152010\n" % SID)
            store = Store()
            self.poll(store, self.cfg(sessions, pads), 1792153000)
            parent_run = store.runs["muse:%s:ws7-notes" % SID]
            child_run = store.runs["muse:%s:%s" % (CHILD, CHILD)]
            self.assertEqual(parent_run["parent_session_id"], HUB)
            self.assertEqual(child_run["parent_session_id"], SID)
            self.assertNotIn("label", child_run)
            self.assertEqual(summed(store, SID)["input"], 10)
            self.assertEqual(summed(store, CHILD)["input"], 4)
            self.assertEqual(store.usage[("muse", CHILD, "c")]["run_id"], "muse:%s:%s" % (CHILD, CHILD))

    def test_bad_line_and_store_error_do_not_abort(self):
        class Boom(Store):
            def __init__(self):
                super().__init__()
                self.left = 1

            def add_usage(self, row):
                if self.left:
                    self.left -= 1
                    raise RuntimeError("UNIQUEPROMPT should stay off stderr")
                super().add_usage(row)

        with tempfile.TemporaryDirectory() as tmp:
            sessions, scratch, path = self._one_session(tmp, SID)
            blob = b"{model_completed UNIQUEPROMPT\n"
            blob += mc(SID, "a", input=1, output=1)
            blob += mc(SID, "b", input=2, output=1)
            with open(path, "wb") as fh:
                fh.write(blob)
            store = Boom()
            err = self.poll(store, self.cfg(sessions, scratch), 1792153000, quiet=False)
            self.assertIn("usage failed", err)
            self.assertNotIn("UNIQUEPROMPT", err)
            self.assertEqual(summed(store)["input"], 2)
            self.assertIn(("muse", SID, "b"), store.usage)
            self.assertNotIn(("muse", SID, "a"), store.usage)

    def test_manifest_retires_unlabeled_session_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws2-WS2.14-cards-list\t%s\t1792152010\n" % SID)
            store = Store()
            shadow = "muse:%s:%s" % (SID, SID)
            labeled = "muse:%s:ws2-WS2.14-cards-list" % SID
            store.runs[shadow] = {
                "run_id": shadow,
                "source": "muse",
                "tier": "muse",
                "session_id": SID,
                "status": "running",
                "pid": 99,
                "started_at": 1792152000.0,
            }
            store.usage[("muse", SID, "old")] = {
                "tool": "muse",
                "session_id": SID,
                "message_id": "old",
                "run_id": shadow,
                "input": 10,
                "output": 1,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
            cfg = self.cfg(os.path.join(tmp, "missing-sessions"), scratch)
            cfg["roots"] = {"muse_sessions": cfg["roots"]["muse_sessions"], "scratch": scratch}
            cfg["read_budgets"] = cfg.pop("read_budget")
            original = muse.live_agent_processes
            muse.live_agent_processes = lambda now=None: [
                {
                    "pid": 42,
                    "comm": "muse-bin",
                    "started_at": 1792152010.0,
                    "label": "ws2-WS2.14-cards-list",
                    "prompt_label": "ws2-WS2.14-cards-list",
                    "session_id": SID,
                }
            ]
            try:
                self.poll(store, cfg, 1792153000)
            finally:
                muse.live_agent_processes = original
            run = store.runs[labeled]
            self.assertEqual(run["label"], "ws2-WS2.14-cards-list")
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["pid"], 42)
            self.assertNotIn(shadow, store.runs)
            self.assertEqual(store.usage[("muse", SID, "old")]["run_id"], labeled)

    def test_shadow_retires_when_session_uuid_case_differs(self):
        upper = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        lower = upper.lower()
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("jax-kernel\t%s\t1792152010\n" % upper)
            store = Store()
            shadow = "muse:%s:%s" % (lower, lower)
            labeled = "muse:%s:jax-kernel" % upper
            store.runs[shadow] = {
                "run_id": shadow,
                "source": "muse",
                "tier": "muse",
                "session_id": lower,
                "status": "running",
                "pid": 77,
                "started_at": 1792152000.0,
            }
            cfg = self.cfg(os.path.join(tmp, "missing-sessions"), scratch)
            cfg["roots"] = {"muse_sessions": cfg["roots"]["muse_sessions"], "scratch": scratch}
            cfg["read_budgets"] = cfg.pop("read_budget")
            original = muse.live_agent_processes
            muse.live_agent_processes = lambda now=None: [
                {
                    "pid": 77,
                    "comm": "muse-bin",
                    "started_at": 1792152010.0,
                    "label": "jax-kernel",
                    "prompt_label": "jax-kernel",
                    "session_id": upper,
                }
            ]
            try:
                self.poll(store, cfg, 1792153000)
            finally:
                muse.live_agent_processes = original
            self.assertEqual(store.runs[labeled]["status"], "running")
            self.assertEqual(store.runs[labeled]["pid"], 77)
            self.assertNotIn(shadow, store.runs)

    def test_live_prompt_file_labels_a_run_before_the_tsv_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(sessions)
            os.makedirs(scratch)
            store = Store()
            shadow = "muse:%s:%s" % (SID, SID)
            labeled = "muse:%s:ws2-WS2.14-prod-columns" % SID
            store.runs[shadow] = {
                "run_id": shadow,
                "source": "muse",
                "tier": "muse",
                "session_id": SID,
                "status": "running",
                "pid": 50,
                "started_at": 1792152000.0,
            }
            original = muse.live_agent_processes
            muse.live_agent_processes = lambda now=None: [
                {
                    "pid": 50,
                    "comm": "muse-bin",
                    "started_at": 1792152010.0,
                    "label": "ws2-WS2.14-prod-columns",
                    "prompt_label": "ws2-WS2.14-prod-columns",
                    "session_id": SID,
                }
            ]
            try:
                self.poll(store, self.cfg(sessions, scratch), 1792153000)
            finally:
                muse.live_agent_processes = original
            run = store.runs[labeled]
            self.assertEqual(run["label"], "ws2-WS2.14-prod-columns")
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["pid"], 50)
            self.assertNotIn(shadow, store.runs)

    def test_zero_byte_read_does_not_create_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            path = os.path.join(
                sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day, sid, "session.jsonl"
            )
            os.makedirs(os.path.dirname(path))
            open(path, "wb").close()
            store = Store()
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0)
            self.poll(store, cfg, now)
            self.assertEqual(store.runs, {})
            self.assertEqual(store.usage, {})

    def test_manifest_moves_orphan_usage_onto_the_labeled_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            sid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            path = os.path.join(
                sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day, sid, "session.jsonl"
            )
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as fh:
                fh.write(mc(sid, "early", input=40, output=3, model="muse-1"))
            store = Store()
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0)
            self.poll(store, cfg, now)
            orphan = "muse:%s:%s" % (sid, sid)
            self.assertIn(orphan, store.runs)
            self.assertEqual(store.usage[("muse", sid, "early")]["run_id"], orphan)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws2-notes\t%s\t1792152010\n" % sid)
            self.poll(store, cfg, now + 5)
            labeled = "muse:%s:ws2-notes" % sid
            self.assertNotIn(orphan, store.runs)
            self.assertEqual(store.usage[("muse", sid, "early")]["run_id"], labeled)
            self.assertEqual(summed(store, sid)["input"], 40)
            self.assertEqual(store.runs[labeled]["model"], "muse-1")
            self.assertEqual(store.runs[labeled]["label"], "ws2-notes")

    def test_cold_rotation_visits_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            day_dir = os.path.join(sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day)
            paths = []
            blob = b"x" * 63 + b"\n"
            for i in range(4):
                sid = "f000000%d-0000-4000-8000-00000000000%d" % (i, i)
                path = os.path.join(day_dir, sid, "session.jsonl")
                os.makedirs(os.path.dirname(path))
                with open(path, "wb") as fh:
                    fh.write(blob)
                paths.append(os.path.abspath(path))
            store = Store()
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0, recent_s=0)
            cfg["read_budget"] = {"per_file": len(blob), "per_tick": len(blob)}
            for step in range(4):
                self.poll(store, cfg, now + step)
            for path in paths:
                self.assertEqual(store.offsets[path][1], len(blob))

    def test_manifest_file_is_read_before_cold_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            day_dir = os.path.join(sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day)
            hot = os.path.join(day_dir, SID, "session.jsonl")
            cold = os.path.join(day_dir, SID2, "session.jsonl")
            os.makedirs(os.path.dirname(hot))
            os.makedirs(os.path.dirname(cold))
            with open(hot, "wb") as fh:
                fh.write(mc(SID, "hot", input=9, output=1))
            with open(cold, "wb") as fh:
                fh.write(b"y" * 4000 + b"\n")
            tsv = os.path.join(scratch, "muse-sessions.tsv")
            with open(tsv, "w") as fh:
                fh.write("ws2-hot\t%s\t1792152010\n" % SID)
            per_tick = os.path.getsize(tsv) + os.path.getsize(hot)
            per_file = max(per_tick, os.path.getsize(cold))
            store = Store()
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0, recent_s=0)
            cfg["read_budget"] = {"per_file": per_file, "per_tick": per_tick}
            self.poll(store, cfg, now)
            self.assertEqual(summed(store, SID)["input"], 9)
            self.assertNotIn(os.path.abspath(cold), store.offsets)

    def test_runs_subdir_manifest_sets_started_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            os.makedirs(sessions)
            pad = os.path.join(tmp, "enc", HUB, "scratchpad", "runs")
            os.makedirs(pad)
            with open(os.path.join(pad, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws2-WS2.14-prod-columns\t%s\t1792167605\tmuse-spark-1.3-contributor\n" % SID)
            store = Store()
            self.poll(store, self.cfg(sessions, os.path.join(tmp, "enc")), 1792168000)
            run = store.runs["muse:%s:ws2-WS2.14-prod-columns" % SID]
            self.assertEqual(run["started_at"], 1792167605.0)
            self.assertEqual(run["clock_source"], "manifest")
            self.assertEqual(run["parent_session_id"], HUB)

    def test_label_merge_does_not_drop_the_epoch(self):
        manifests = {}
        muse._keep_latest(manifests, {"label": "from-file", "session_id": SID, "started_at": 1792167605.0})
        muse._keep_latest(manifests, {"label": "from-live", "session_id": SID, "started_at": None})
        row = manifests[SID.lower()]
        self.assertEqual(row["started_at"], 1792167605.0)
        self.assertEqual(row["label"], "from-file")
        manifests = {}
        muse._keep_latest(manifests, {"label": "from-live", "session_id": SID, "started_at": None})
        muse._keep_latest(manifests, {"label": "from-file", "session_id": SID, "started_at": 1792167605.0})
        row = manifests[SID.lower()]
        self.assertEqual(row["started_at"], 1792167605.0)
        self.assertEqual(row["label"], "from-file")

    def test_first_session_event_is_the_start_when_the_tail_is_past_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc)
            path = os.path.join(
                sessions, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day, sid, "session.jsonl"
            )
            os.makedirs(os.path.dirname(path))
            first = {"recorded_at": 1792152000000000, "payload": {"event": {"kind": "started"}}}
            line1 = (json.dumps(first) + "\n").encode()
            line2 = mc(sid, "a", input=6, output=1, recorded_at=1792152090000000)
            with open(path, "wb") as fh:
                fh.write(line1 + line2)
            store = Store()
            st = os.stat(path)
            store.set_offset(os.path.abspath(path), st.st_ino, len(line1), st.st_size, st.st_mtime)
            cfg = self.cfg(sessions, scratch, fresh_hours=36, dir_scan_s=0)
            self.poll(store, cfg, now)
            run = store.runs["muse:%s:%s" % (sid, sid)]
            self.assertEqual(run["started_at"], 1792152000.0)
            self.assertEqual(run["clock_source"], "muse_log")
            self.assertEqual(summed(store, sid)["input"], 6)

    def test_missing_root(self):
        store = Store()
        self.poll(
            store,
            self.cfg("/no/such/muse-sessions", "/no/such/scratchpads"),
            1792153000,
        )
        self.assertEqual(store.runs, {})
        self.assertEqual(store.usage, {})

    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local muse session")
    def test_smoke_real_session_bounded(self):
        sessions = os.environ.get("AGENT_DASH_SMOKE_MUSE_ROOT")
        sid = os.environ.get("AGENT_DASH_SMOKE_MUSE_SID")
        if not sessions or not sid or not os.path.isdir(sessions):
            self.skipTest("AGENT_DASH_SMOKE_MUSE_ROOT and AGENT_DASH_SMOKE_MUSE_SID are not set")
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws7-notes\t%s\t1792152010\n" % sid)
            store = Store()
            cfg = self.cfg(sessions, scratch)
            cfg["fresh_hours"] = 0
            self.poll(store, cfg, time.time())
            self.assertTrue(any(sid in key for key in store.offsets))

    def _one_session(self, tmp, sid):
        sessions = os.path.join(tmp, "sessions")
        scratch = os.path.join(tmp, "scratch")
        os.makedirs(scratch)
        path = os.path.join(sessions, "2026", "10", "16", sid, "session.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(os.path.join(scratch, "muse-sessions.tsv"), "w") as fh:
            fh.write("ws7-notes\t%s\t1792152010\n" % sid)
        return sessions, scratch, path


class ManifestDirsTests(unittest.TestCase):
    def setUp(self):
        muse._FRESH.clear()
        muse._LOCATE.clear()

    def _pad(self, root, enc, hub, label="ws2-x", sid=SID):
        pad = os.path.join(root, enc, hub, "scratchpad")
        os.makedirs(pad)
        with open(os.path.join(pad, "muse-sessions.tsv"), "w") as fh:
            fh.write("%s\t%s\t1792152010\n" % (label, sid))
        return os.path.realpath(pad)

    def test_parent_root_lists_enc_session_pads(self):
        with tempfile.TemporaryDirectory() as tmp:
            want = self._pad(tmp, "enc", HUB)
            pads = dict(muse._manifest_dirs(tmp, "muse-sessions.tsv", None))
            self.assertEqual(pads, {want: HUB})

    def test_encoded_cwd_root_lists_session_pads_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            want = self._pad(tmp, "enc", HUB)
            pads = dict(muse._manifest_dirs(os.path.join(tmp, "enc"), "muse-sessions.tsv", None))
            self.assertEqual(pads, {want: HUB})

    def test_only_filters_hub_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._pad(tmp, "enc", HUB)
            enc = os.path.join(tmp, "enc")
            self.assertEqual(list(muse._manifest_dirs(enc, "muse-sessions.tsv", "other")), [])
            self.assertEqual(len(list(muse._manifest_dirs(enc, "muse-sessions.tsv", HUB))), 1)

    def test_non_uuid_children_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc = os.path.join(tmp, "enc")
            os.makedirs(os.path.join(enc, "not-a-session", "scratchpad"))
            self.assertEqual(list(muse._manifest_dirs(enc, "muse-sessions.tsv", None)), [])

    def test_nested_child_folder_manifest_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pad = os.path.join(tmp, "enc", HUB, "scratchpad")
            nested = os.path.join(pad, "runs")
            os.makedirs(nested)
            with open(os.path.join(nested, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws2-nested\t%s\t1792152010\n" % SID)
            pads = dict(muse._manifest_dirs(os.path.join(tmp, "enc"), "muse-sessions.tsv", None))
            self.assertEqual(pads, {os.path.realpath(nested): HUB})

    def test_registered_run_dir_is_scanned_without_scratch_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = os.path.join(tmp, "sessions")
            os.makedirs(sessions)
            rundir = os.path.join(tmp, "ws2")
            os.makedirs(rundir)
            with open(os.path.join(rundir, "muse-sessions.tsv"), "w") as fh:
                fh.write("ws2-build\t%s\t1792152010\tmuse-spark-1.3-contributor\n" % SID)
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
                fh.write(rundir + "\n")
            cfg = {
                "roots": {"muse_sessions": sessions, "scratchpads": []},
                "state_dir": state,
                "fresh_hours": 0,
                "read_budget": {"per_file": 1024 * 1024, "per_tick": 4 * 1024 * 1024},
            }
            store = Store()
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                muse.poll(store, cfg, 1792152100.0)
            self.assertEqual(buf.getvalue(), "")
            run = store.runs["muse:%s:ws2-build" % SID]
            self.assertEqual(run["label"], "ws2-build")
            self.assertEqual(run["source"], "muse")


if __name__ == "__main__":
    unittest.main()
