"""view_seq: the page-visible sequence stays quiet on invisible writes.

``seq`` moves on every write (heartbeats, cursors, samples) so the
stale badge can tell live from dead. ``data_seq`` moves when a served
byte can change, so content caches invalidate. ``view_seq`` moves only
when snapshot- or usage-exposed data changes, so an idle page's SSE
tick comparison skips its refetch. These tests pin that contract:
no-op upserts, process samples, and offset cursors must not move it;
usage rows, status and attribution changes must.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash.store import Store


def _run(run_id="r-view-1", **kw):
    row = {
        "run_id": run_id,
        "source": "claude",
        "tier": "opus",
        "model": "claude-opus-4-6",
        "label": "ws1-1 do a thing",
        "session_id": "s-view-1",
        "status": "running",
        "started_at": 1000.0,
    }
    row.update(kw)
    return row


def _usage(message_id="m1", **kw):
    row = {
        "ts": 1100.0,
        "session_id": "s-view-1",
        "run_id": "r-view-1",
        "model": "claude-opus-4-6",
        "tool": "claude",
        "message_id": message_id,
        "input": 100,
        "output": 10,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    row.update(kw)
    return row


class ViewSeqTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_run_moves_view(self):
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.upsert_run(_run())
        self.assertGreater(self.store.seq, before[0])
        self.assertGreater(self.store.data_seq, before[1])
        self.assertGreater(self.store.view_seq, before[2])

    def test_noop_run_upsert_moves_nothing(self):
        self.store.upsert_run(_run())
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.upsert_run(_run())
        self.assertEqual(
            (self.store.seq, self.store.data_seq, self.store.view_seq), before
        )

    def test_telemetry_only_rewrite_moves_data_not_view(self):
        self.store.upsert_run(_run())
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.upsert_run(_run(out_bytes=4096, out_growing=1, err_bytes=3))
        self.assertGreater(self.store.seq, before[0])
        self.assertGreater(self.store.data_seq, before[1])
        self.assertEqual(self.store.view_seq, before[2])

    def test_status_change_moves_view(self):
        self.store.upsert_run(_run())
        before = self.store.view_seq
        self.store.upsert_run(_run(status="done", ended_at=1200.0))
        self.assertGreater(self.store.view_seq, before)

    def test_session_upsert_moves_view(self):
        before = self.store.view_seq
        self.store.upsert_session(
            {"session_id": "s-view-1", "tool": "claude", "label": "ws1-1 do a thing"}
        )
        self.assertGreater(self.store.view_seq, before)

    def test_noop_session_upsert_moves_nothing(self):
        self.store.upsert_session(
            {"session_id": "s-view-1", "tool": "claude", "label": "ws1-1 do a thing"}
        )
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.upsert_session(
            {"session_id": "s-view-1", "tool": "claude", "label": "ws1-1 do a thing"}
        )
        self.assertEqual(
            (self.store.seq, self.store.data_seq, self.store.view_seq), before
        )

    def test_usage_write_moves_view_but_repeat_does_not(self):
        self.store.upsert_run(_run())
        before = self.store.view_seq
        self.store.add_usage(_usage())
        self.assertGreater(self.store.view_seq, before)
        steady = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.add_usage(_usage())
        self.assertEqual(
            (self.store.seq, self.store.data_seq, self.store.view_seq), steady
        )

    def test_process_sample_moves_seq_only(self):
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.add_process_sample(
            {"ts": time.time(), "pid": 4242, "run_id": "r-view-1"}
        )
        self.assertGreater(self.store.seq, before[0])
        self.assertEqual(self.store.data_seq, before[1])
        self.assertEqual(self.store.view_seq, before[2])

    def test_offset_write_moves_seq_only(self):
        self.store.set_offset("/tmp/view-cursor", 7, 10, 10, 1.0)
        before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.store.set_offset("/tmp/view-cursor", 7, 40, 40, 2.0)
        self.assertGreater(self.store.seq, before[0])
        self.assertEqual(self.store.data_seq, before[1])
        self.assertEqual(self.store.view_seq, before[2])

    def test_snapshot_carries_view_seq_on_miss_and_hit(self):
        self.store.upsert_run(_run())
        now = time.time()
        miss = self.store.snapshot(now, "all")
        self.assertEqual(miss["view_seq"], self.store.view_seq)
        self.store.set_offset("/tmp/view-cursor", 9, 99, 99, 3.0)
        hit = self.store.snapshot_cached(now, "all")
        self.assertEqual(hit["view_seq"], self.store.view_seq)
        self.assertEqual(hit["data_seq"], self.store.data_seq)

    def test_quiet_writes_keep_snapshot_cache_hot(self):
        now = time.time()
        self.store.upsert_run(_run())
        first = self.store.snapshot_cached(now, "all")
        self.store.add_process_sample(
            {"ts": time.time(), "pid": 4242, "run_id": "r-view-1"}
        )
        self.store.set_offset("/tmp/view-cursor", 7, 40, 40, 2.0)
        second = self.store.snapshot_cached(now, "all")
        self.assertEqual(first["view_seq"], second["view_seq"])
        self.assertEqual(first["totals"], second["totals"])

    def test_reassign_moves_view(self):
        self.store.upsert_run(_run("r-view-1"))
        self.store.upsert_run(_run("r-view-2", session_id="s-view-2"))
        before = self.store.view_seq
        self.assertTrue(self.store.reassign_run("r-view-2", "r-view-1"))
        self.assertGreater(self.store.view_seq, before)


if __name__ == "__main__":
    unittest.main()
