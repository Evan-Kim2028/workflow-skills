"""Tests for dash/sources/muse.py. Synthetic fixtures plus one bounded smoke."""

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
            if value is not None:
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
            self.assertEqual(total["input"], 108)
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
                fh.write("ws7-notes\t%s\t1792152010\n" % SID)
            store = Store()
            cfg = self.cfg(os.path.join(tmp, "missing-sessions"), scratch)
            cfg["roots"] = {"muse_sessions": cfg["roots"]["muse_sessions"], "scratch": scratch}
            cfg["read_budgets"] = cfg.pop("read_budget")
            self.poll(store, cfg, 1792153000)
            run = store.runs["muse:%s:ws7-notes" % SID]
            self.assertEqual(run["clock_source"], "manifest")
            self.assertEqual(run["started_at"], 1792152010.0)
            self.assertEqual(store.usage, {})

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


if __name__ == "__main__":
    unittest.main()
