"""Poll budgets, bounded scans, and stall alerts (robustness items 1-3).

A hung source must not stall the others; repeat polls over an unchanged
tree must list nothing new and open no files; dead offset rows are
pruned in batches while rotation cursors survive.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import builtins
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_dash  # noqa: E402
import dash.sources as sources_pkg  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.sources import claude  # noqa: E402
from dash.sources import cursor as cursor_mod  # noqa: E402
from dash.sources import manifests  # noqa: E402
from dash.sources import muse as muse_mod  # noqa: E402
from dash.store import Store  # noqa: E402

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


class _Slow:
    NAME = "slow"
    INTERVAL_S = 2

    def poll(self, store, cfg, now):
        time.sleep(30)


class _Fast:
    NAME = "fast"
    INTERVAL_S = 2

    def __init__(self):
        self.n = 0

    def poll(self, store, cfg, now):
        self.n += 1


class WatchdogTests(unittest.TestCase):
    def test_slow_source_does_not_stall_the_others(self):
        store = MemStore()
        slow, fast = _Slow(), _Fast()
        registry = Registry([slow, fast])
        start = time.monotonic()
        registry.tick(store, {"poll_budget_s": 0.5}, NOW, force=False)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10)
        self.assertEqual(fast.n, 1)
        state = registry.state["slow"]
        self.assertTrue(state["timed_out"])
        self.assertIn("budget", state["last_error"])
        self.assertIsNone(state["last_poll"])
        self.assertEqual(registry.state["fast"]["last_poll"], NOW)
        body = registry.health(NOW + 1)
        self.assertFalse(body["ok"])

    def test_lag_beyond_three_intervals_is_a_stall(self):
        store = MemStore()
        fast = _Fast()
        registry = Registry([fast])
        # A mature dashboard (past the startup window): lag judges at once.
        registry._born = NOW - 3600
        registry.tick(store, {}, NOW, force=True)
        self.assertEqual(registry.health(NOW + 1)["ok"], True)
        body = registry.health(NOW + 7)
        self.assertFalse(body["ok"])
        entry = body["sources"][0]
        self.assertEqual(
            set(entry),
            {"name", "last_poll", "last_error", "lag_s", "last_duration_s", "interval_s", "stalled",
             "state", "started_at"},
        )
        self.assertTrue(entry["stalled"])
        self.assertIsNotNone(entry["last_duration_s"])
        self.assertEqual(entry["interval_s"], 2)
        self.assertEqual(
            registry.stalled_messages(NOW + 7),
            ["source stalled: fast (last ok poll 7 s ago)"],
        )

    def test_never_polled_source_stalls_after_its_grace(self):
        registry = Registry([_Fast()])
        body = registry.health(time.time() + 3600)
        self.assertFalse(body["ok"])
        self.assertTrue(body["sources"][0]["stalled"])
        self.assertEqual(len(registry.stalled_messages(time.time() + 3600)), 1)

    def test_source_states_carry_last_poll(self):
        store = MemStore()
        fast = _Fast()
        registry = Registry([fast])
        registry.tick(store, {}, NOW, force=True)
        rows = registry.source_states(NOW + 1)
        self.assertEqual(rows, [{"name": "fast", "last_poll": NOW, "lag_s": 1}])

    def test_error_still_marks_last_poll(self):
        class _Bad:
            NAME = "bad"
            INTERVAL_S = 1

            def poll(self, store, cfg, now):
                raise RuntimeError("boom")

        store = MemStore()
        registry = Registry([_Bad()])
        registry.tick(store, {}, 10, force=True)
        body = registry.health(12)
        self.assertFalse(body["ok"])
        self.assertEqual(body["sources"][0]["last_error"], "RuntimeError: boom")
        self.assertEqual(body["sources"][0]["lag_s"], 2)


class RestartGraceTests(unittest.TestCase):
    """The first poll cycle after a restart is "starting", never stalled."""

    class _FirstScanSlow:
        NAME = "first-scan-slow"
        INTERVAL_S = 2

        def __init__(self):
            import threading

            self.entered = threading.Event()
            self.release = threading.Event()

        def poll(self, store, cfg, now):
            self.entered.set()
            self.release.wait(30)

    def test_slow_first_scan_is_starting_not_stalled(self):
        import threading

        store = MemStore()
        source = self._FirstScanSlow()
        registry = Registry([source])
        born = registry._born
        worker = threading.Thread(
            target=registry.tick, args=(store, {}, born + 1), kwargs={"force": True}, daemon=True
        )
        worker.start()
        try:
            self.assertTrue(source.entered.wait(10))
            # The first scan takes longer than the 2 s interval; the source
            # has never completed a poll, so it is starting, not stalled.
            body = registry.health(born + 5)
            self.assertTrue(body["ok"])
            entry = body["sources"][0]
            self.assertEqual(entry["state"], "starting")
            self.assertFalse(entry["stalled"])
            self.assertEqual(entry["started_at"], born)
            self.assertEqual(body["slow_sources"], 0)
            self.assertEqual(registry.stalled_messages(born + 5), [])
        finally:
            source.release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        # The first poll alone does not end startup (the process is still
        # young): a second poll past max(3x interval, 90 s) reads ok.
        registry.tick(store, {}, born + 95, force=True)
        body = registry.health(born + 96)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sources"][0]["state"], "ok")

    def test_never_polled_source_leaves_grace(self):
        registry = Registry([_Fast()])
        registry._born = NOW
        body = registry.health(NOW + 10)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sources"][0]["state"], "starting")
        self.assertEqual(registry.stalled_messages(NOW + 10), [])
        # Grace is max(3x interval, 90 s): 90 s here.
        body = registry.health(NOW + 91)
        self.assertFalse(body["ok"])
        self.assertEqual(body["sources"][0]["state"], "stalled")
        self.assertEqual(len(registry.stalled_messages(NOW + 91)), 1)

    def test_long_interval_grace_is_three_intervals(self):
        class _Hourly:
            NAME = "hourly"
            INTERVAL_S = 60

        registry = Registry([_Hourly()])
        registry._born = NOW
        body = registry.health(NOW + 100)
        self.assertEqual(body["sources"][0]["state"], "starting")
        body = registry.health(NOW + 181)
        self.assertEqual(body["sources"][0]["state"], "stalled")

    def test_first_poll_error_is_error_not_starting(self):
        class _Bad:
            NAME = "bad"
            INTERVAL_S = 1

            def poll(self, store, cfg, now):
                raise RuntimeError("boom")

        store = MemStore()
        registry = Registry([_Bad()])
        born = registry._born
        registry.tick(store, {}, born + 1, force=True)
        body = registry.health(born + 2)
        self.assertFalse(body["ok"])
        self.assertEqual(body["sources"][0]["state"], "error")

    def test_slow_returning_poll_is_slow_but_ok(self):
        store = MemStore()
        fast = _Fast()
        registry = Registry([fast])
        # Past the startup window so the slow state itself is visible.
        registry._born = NOW - 3600
        registry.tick(store, {}, NOW, force=True)
        registry.state["fast"]["last_duration_s"] = 16.0
        body = registry.health(NOW + 1)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sources"][0]["state"], "slow")
        self.assertEqual(body["slow_sources"], 1)


class StartupFalsePositiveTests(unittest.TestCase):
    """Restart false positive: a source that finishes its first scan early
    must read "starting", not "stalled", while a slower source is still on
    its first scan (the registry polls sequentially and lag counts from the
    first completed poll)."""

    class _SlowFirst:
        NAME = "slow-first"
        INTERVAL_S = 30

    def _registry(self):
        registry = Registry([_Fast(), self._SlowFirst()])
        registry._born = NOW
        return registry

    def test_fast_source_is_starting_while_slow_scans(self):
        registry = self._registry()
        # Fast source polled at NOW + 1; the slow source never completed one.
        registry.state["fast"].update(last_poll=NOW + 1, last_ok=NOW + 1)
        # Lag (7 s) is past 3x the fast interval: without the startup gate
        # this would already read stalled.
        body = registry.health(NOW + 8)
        self.assertTrue(body["ok"])
        by_name = {entry["name"]: entry for entry in body["sources"]}
        self.assertEqual(by_name["fast"]["state"], "starting")
        self.assertFalse(by_name["fast"]["stalled"])
        self.assertEqual(by_name["slow-first"]["state"], "starting")
        self.assertFalse(by_name["slow-first"]["stalled"])
        self.assertEqual(registry.stalled_messages(NOW + 8), [])

    def test_ok_once_startup_is_complete(self):
        registry = self._registry()
        # Startup completes only past max(3x slowest interval, 90 s) with
        # every source polled (90 s here): both poll again at NOW + 91.
        registry.state["fast"].update(last_poll=NOW + 91, last_ok=NOW + 91)
        registry.state["slow-first"].update(last_poll=NOW + 91, last_ok=NOW + 91)
        body = registry.health(NOW + 92)
        self.assertTrue(body["ok"])
        by_name = {entry["name"]: entry for entry in body["sources"]}
        self.assertEqual(by_name["fast"]["state"], "ok")
        self.assertEqual(by_name["slow-first"]["state"], "ok")
        self.assertEqual(registry.stalled_messages(NOW + 92), [])

    def test_error_during_startup_is_error_not_starting(self):
        registry = self._registry()
        registry.state["fast"].update(last_poll=NOW + 1, last_ok=NOW + 1)
        registry.state["slow-first"]["last_error"] = "RuntimeError: boom"
        body = registry.health(NOW + 2)
        self.assertFalse(body["ok"])
        by_name = {entry["name"]: entry for entry in body["sources"]}
        self.assertEqual(by_name["slow-first"]["state"], "error")
        self.assertEqual(by_name["fast"]["state"], "starting")


def _count_fresh_listings():
    real = sources_pkg.list_dir_cached
    calls = []

    def wrapped(path, cache, cap=None):
        entries, listed = real(path, cache, cap=cap)
        calls.append(listed)
        return entries, listed

    sources_pkg.list_dir_cached = wrapped
    return calls, real


class ClaudeScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        claude.reset_state()

    def tearDown(self):
        self.tmp.cleanup()

    def _tree(self, projects=30, per=10):
        root = os.path.join(self.tmp.name, "projects")
        for p in range(projects):
            proj = os.path.join(root, "proj-%d" % p)
            os.makedirs(proj, exist_ok=True)
            for i in range(per):
                sid = "%08x-aaaa-aaaa-aaaa-aaaaaaaaaaaa" % (p * per + i)
                path = os.path.join(proj, sid + ".jsonl")
                with open(path, "wb") as fh:
                    fh.write(b'{"type":"assistant","message":{"id":"m%d","model":"m","usage":{"input_tokens":1}}}\n' % i)
                os.utime(path, (NOW, NOW))
        sessions = os.path.join(self.tmp.name, "sessions")
        os.makedirs(sessions, exist_ok=True)
        return {
            "roots": {"claude_projects": root, "claude_sessions": sessions},
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "running_mtime_s": 900,
            "read_budgets": {"per_file": 4 * 1024 * 1024, "per_tick": 64 * 1024 * 1024},
        }

    def test_repeat_poll_lists_nothing_new_and_opens_no_files(self):
        cfg = self._tree()
        store = MemStore()
        claude.poll(store, cfg, NOW)
        first_runs = dict(store.runs)
        self.assertTrue(first_runs)
        calls, real = _count_fresh_listings()
        real_open = builtins.open
        opened = []

        def wrapped_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        builtins.open = wrapped_open
        try:
            claude.poll(store, cfg, NOW + 100)
        finally:
            builtins.open = real_open
            sources_pkg.list_dir_cached = real
        self.assertNotIn(True, calls)
        self.assertEqual(opened, [])
        self.assertEqual(store.runs, first_runs)

    def test_new_file_is_found_without_a_full_relist(self):
        cfg = self._tree(projects=5, per=5)
        store = MemStore()
        claude.poll(store, cfg, NOW)
        before = len(store.runs)
        sid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        path = os.path.join(cfg["roots"]["claude_projects"], "proj-0", sid + ".jsonl")
        with open(path, "wb") as fh:
            fh.write(b'{"type":"assistant","message":{"id":"m9","model":"m","usage":{"input_tokens":1}}}\n')
        os.utime(path, (NOW + 200, NOW + 200))
        claude.poll(store, cfg, NOW + 200)
        self.assertGreater(len(store.runs), before)
        self.assertIn("claude-main:%s:main" % sid, store.runs)


def _write_db(path, blobs):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    root = "1" * 64
    con.execute(
        "INSERT INTO meta VALUES ('0', ?)",
        (json.dumps({"agentId": "chat", "latestRootBlobId": root}).encode().hex(),),
    )
    con.execute("INSERT INTO blobs VALUES (?, ?)", (root, b"\n\x02"))
    for ident, data in blobs:
        con.execute("INSERT INTO blobs VALUES (?, ?)", (ident, data))
    con.commit()
    con.close()
    return root


def _billed_blob(model):
    return json.dumps(
        {
            "role": "assistant",
            "providerOptions": {"cursor": {"modelName": model}},
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 3,
                "cache_write_tokens": 2,
                "reasoning_tokens": 5,
            },
        }
    ).encode()


class CursorSkipTests(unittest.TestCase):
    UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "cursor")
        chat_dir = os.path.join(self.home, "chats", "bucket", self.UUID)
        os.makedirs(chat_dir, exist_ok=True)
        with open(os.path.join(chat_dir, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schemaVersion": 1,
                    "cwd": "/tmp/demo",
                    "createdAtMs": 1_000_000,
                    "updatedAtMs": 2_000_000,
                    "hasConversation": True,
                },
                fh,
            )
        _write_db(os.path.join(chat_dir, "store.db"), [("2" * 64, _billed_blob("grok-4.7"))])
        tdir = os.path.join(
            self.home, "projects", cursor_mod.encode_cwd("/tmp/demo"), "agent-transcripts", self.UUID
        )
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, self.UUID + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"role":"user","message":{"content":[{"type":"text","text":"[label: ws1-notes]\\n"}}}}\n')
        self._live = cursor_mod.live_label_pids
        cursor_mod.live_label_pids = lambda: {}
        cursor_mod._META_CACHE.clear()
        cursor_mod._DB_FP.clear()
        cursor_mod._dir_cache.clear()

    def tearDown(self):
        cursor_mod.live_label_pids = self._live
        self.tmp.cleanup()

    def cfg(self):
        return {
            "roots": {"cursor_home": self.home, "scratchpads": []},
            "tmp_dir": os.path.join(self.home, "tmp"),
            "read_budget_per_file": 4 * 1024 * 1024,
            "read_budget_per_tick": 8 * 1024 * 1024,
        }

    def test_unchanged_db_is_not_reopened(self):
        store = MemStore()
        cursor_mod.poll(store, self.cfg(), NOW)
        self.assertTrue(store.offsets)
        real = cursor_mod.open_store
        calls = []

        def wrapped(path, tmp_root, budget, tick_budget):
            calls.append(path)
            return real(path, tmp_root, budget, tick_budget)

        cursor_mod.open_store = wrapped
        try:
            cursor_mod.poll(store, self.cfg(), NOW + 10)
        finally:
            cursor_mod.open_store = real
        self.assertEqual(calls, [])


class MuseScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        muse_mod.reset_state()

    def tearDown(self):
        self.tmp.cleanup()

    def _root(self):
        root = os.path.join(self.tmp.name, "scratch-root")
        sess = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        pad = os.path.join(root, "enc", sess, "scratchpad")
        os.makedirs(pad, exist_ok=True)
        with open(os.path.join(pad, "muse-sessions.tsv"), "w", encoding="utf-8") as fh:
            fh.write("ws1-notes\t%s\t1792000001\n" % sess)
        return root

    def test_manifest_walk_lists_nothing_new_on_repeat(self):
        root = self._root()
        first = list(muse_mod._manifest_dirs(root, "muse-sessions.tsv", None))
        self.assertEqual(len(first), 1)
        calls, real = _count_fresh_listings()
        try:
            second = list(muse_mod._manifest_dirs(root, "muse-sessions.tsv", None))
        finally:
            sources_pkg.list_dir_cached = real
        self.assertEqual(first, second)
        self.assertNotIn(True, calls)


class ManifestsScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        manifests.reset_state()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tsv_reread_is_free_when_unchanged(self):
        path = os.path.join(self.tmp.name, "muse-sessions.tsv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("ws1-notes\tsid\t1792000001\n")
        rows, left = manifests._load_tsv(path, 1024 * 1024)
        self.assertEqual(len(rows), 1)
        self.assertLess(left, 1024 * 1024)
        rows2, left2 = manifests._load_tsv(path, left)
        self.assertEqual(rows2, rows)
        self.assertEqual(left2, left)

    def test_scratchpad_walk_lists_nothing_new_on_repeat(self):
        root = os.path.join(self.tmp.name, "scratch-root")
        sess = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        pad = os.path.join(root, "enc", sess, "scratchpad")
        os.makedirs(pad, exist_ok=True)
        with open(os.path.join(pad, "muse-sessions.tsv"), "w", encoding="utf-8") as fh:
            fh.write("ws1-notes\t%s\t1792000001\n" % sess)
        first = manifests._scratchpads(root, NOW, None, 15)
        self.assertEqual(len(first), 1)
        calls, real = _count_fresh_listings()
        try:
            second = manifests._scratchpads(root, NOW + 100, None, 15)
        finally:
            sources_pkg.list_dir_cached = real
        self.assertEqual(first, second)
        self.assertNotIn(True, calls)


class RotationTests(unittest.TestCase):
    def test_old_backups_leave_the_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            for i in range(5):
                path = state / ("dash.sqlite.bak-%d" % i)
                path.write_bytes(b"z")
                os.utime(path, (1000 + i, 1000 + i))
            moved = agent_dash.rotate_backups(state)
            self.assertEqual(len(moved), 2)
            self.assertEqual(
                sorted(p.name for p in state.iterdir() if p.is_file()),
                ["dash.sqlite.bak-2", "dash.sqlite.bak-3", "dash.sqlite.bak-4"],
            )
            retired = state.parent / "state-retired"
            self.assertEqual(
                sorted(p.name for p in retired.iterdir()),
                ["dash.sqlite.bak-0", "dash.sqlite.bak-1"],
            )

    def test_few_backups_stay_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "dash.sqlite.bak-0").write_bytes(b"z")
            self.assertEqual(agent_dash.rotate_backups(state), [])
            self.assertTrue((state / "dash.sqlite.bak-0").is_file())


if __name__ == "__main__":
    unittest.main()
