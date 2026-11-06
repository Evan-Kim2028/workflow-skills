"""Response caches key on the content sequence, not on every write.

``seq`` moves on every write (cursors, prunes, samples) so the stale badge can
tell live from dead; that stays. The response caches (``/api/state``, the
workstream progress, the phases and burn caches) key on ``content_key``
``(data_seq, sample_seq)``: rows and liveness evidence. A cursor advance or a
prune changes no served byte, so it must not invalidate them. A process sample
does change liveness, so it still does.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash.state import _state_key
from dash.store import Store


def _run(run_id="r-cache-1"):
    return {
        "run_id": run_id,
        "source": "claude",
        "tier": "opus",
        "model": "claude-opus-4-6",
        "label": "ws1-1 cache",
        "session_id": "s-cache-1",
        "status": "running",
        "started_at": 1000.0,
    }


class ContentKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_cursor_write_moves_seq_but_not_content(self):
        self.store.set_offset("/tmp/cache-cursor", 7, 10, 10, 1.0)
        before_seq, before = self.store.seq, self.store.content_key
        self.store.set_offset("/tmp/cache-cursor", 7, 40, 40, 2.0)
        self.assertGreater(self.store.seq, before_seq)
        self.assertEqual(self.store.content_key, before)

    def test_process_sample_moves_content(self):
        before = self.store.content_key
        self.store.add_process_sample(
            {"ts": time.time(), "pid": 4242, "label": "ws1-1 x", "run_id": "r-cache-1", "elapsed_s": 1.0}
        )
        self.assertNotEqual(self.store.content_key, before)
        self.assertGreater(self.store.sample_seq, 0)

    def test_row_write_moves_content(self):
        before = self.store.content_key
        self.store.upsert_run(_run())
        self.assertNotEqual(self.store.content_key, before)

    def test_ws_counts_write_moves_counts_not_data(self):
        # test_server pins that a counts write leaves the snapshot's data version
        # alone; the state body names its boards from ws_counts, so the content
        # key still moves (counts_seq), and the state cache invalidates.
        data_before, content_before = self.store.data_seq, self.store.content_key
        self.store.set_ws_counts([{"ws": "WS1", "name": "one", "done": 2}])
        self.assertEqual(self.store.data_seq, data_before)
        self.assertNotEqual(self.store.content_key, content_before)

    def test_state_key_survives_cursor_churn_and_moves_on_rows(self):
        now = time.time()
        cfg = {}
        self.store.upsert_run(_run())
        key = _state_key(self.store, None, cfg, now, None)
        self.assertIsNotNone(key)
        self.store.set_offset("/tmp/cache-cursor", 9, 99, 99, 3.0)
        self.assertEqual(_state_key(self.store, None, cfg, now, None), key)
        self.store.upsert_run(_run("r-cache-2"))
        self.assertNotEqual(_state_key(self.store, None, cfg, now, None), key)


if __name__ == "__main__":
    unittest.main()
