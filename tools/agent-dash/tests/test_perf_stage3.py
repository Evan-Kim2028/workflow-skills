"""Stage 3 perf contracts: WAL bound, no VACUUM on the serving path, discovery windows.

Covers the changes in docs/perf.md (stage 3): ``Store.prune_offsets`` never
runs VACUUM (freed pages are reused), ``Store.checkpoint_wal`` truncates a
large WAL and is a no-op below its threshold, and the source-side memos
(``memo_call``, ``discovery_memo``) reuse results only inside their window.
"""

import os
import tempfile
import time
import unittest

from dash.sources import discovery_memo, memo_call, set_discovery_ttl
from dash.store import Store


class PruneAndWalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "dash.sqlite")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_prune_leaves_freed_pages_on_the_freelist(self):
        # A VACUUM would rebuild the file and leave no free pages behind.
        for index in range(200):
            self.store.set_offset(os.path.join(self.tmp.name, f"gone-{index}.jsonl"), 1, 0, 3, 1.0)
        total = 0
        for _ in range(20):
            deleted = self.store.prune_offsets(batch=50)
            total += deleted
            if deleted == 0:
                break
        self.assertEqual(total, 200)
        freelist = self.store._conn.execute("PRAGMA freelist_count").fetchone()[0]
        self.assertGreater(freelist, 0)

    def test_checkpoint_wal_is_a_noop_below_threshold(self):
        self.store.set_offset(os.path.join(self.tmp.name, "a.jsonl"), 1, 0, 3, 1.0)
        self.assertIsNone(self.store.checkpoint_wal(threshold_bytes=1 << 40))

    def test_checkpoint_wal_truncates_the_log(self):
        for index in range(50):
            self.store.set_offset(os.path.join(self.tmp.name, f"f-{index}.jsonl"), 1, index, 3, 1.0)
        wal = self.path + "-wal"
        self.assertGreater(os.path.getsize(wal), 0)
        result = self.store.checkpoint_wal(threshold_bytes=0, mode="TRUNCATE")
        self.assertIsNotNone(result)
        self.assertEqual(result["busy"], 0)
        self.assertEqual(os.path.getsize(wal), 0)

    def test_passive_checkpoint_runs_without_the_store_lock(self):
        for index in range(50):
            self.store.set_offset(os.path.join(self.tmp.name, f"p-{index}.jsonl"), 1, index, 3, 1.0)
        # Holding the store lock must not block a checkpoint (it runs off-lock).
        with self.store._lock:
            result = self.store.checkpoint_wal(threshold_bytes=0)
        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "PASSIVE")

    def test_memory_store_never_checkpoints(self):
        mem = Store(":memory:")
        try:
            self.assertIsNone(mem.checkpoint_wal(threshold_bytes=0))
        finally:
            mem.close()


class MemoWindowTest(unittest.TestCase):
    def test_memo_call_reuses_inside_the_window_only(self):
        calls = []

        def compute():
            calls.append(1)
            return len(calls)

        key = ("memo-window-test", time.monotonic_ns())
        self.assertEqual(memo_call(key, 60.0, compute), 1)
        self.assertEqual(memo_call(key, 60.0, compute), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(memo_call(key, 0.0, compute), 2)

    def test_discovery_memo_follows_the_thread_window(self):
        calls = []

        def walk():
            calls.append(1)
            return ["x"]

        key = ("discovery-window-test", time.monotonic_ns())
        try:
            set_discovery_ttl(0)
            discovery_memo(key, walk)
            discovery_memo(key, walk)
            self.assertEqual(len(calls), 2, "a zero window walks on every call")
            set_discovery_ttl(60)
            discovery_memo(key, walk)
            discovery_memo(key, walk)
            self.assertEqual(len(calls), 3, "a 60 s window reuses the last walk")
        finally:
            set_discovery_ttl(0)


if __name__ == "__main__":
    unittest.main()
