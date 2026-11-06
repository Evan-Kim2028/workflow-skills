"""Source poll cadence: settled rows skip, live rows never do.

A poll used to re-write every chat and manifest row on every 2 s tick. Finished
rows whose inputs did not change are now written at most once per recheck
window; running rows are written every tick. The attribution map is read once
per poll scope instead of once per upsert. These tests pin both rules.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import attribution
from dash.sources import SettledRows
from dash.store import Store


class SettledRowsTest(unittest.TestCase):
    def test_finished_row_skips_inside_window_and_rechecks_after(self):
        memo = SettledRows(recheck_s=30.0)
        key, sig = ("p", "chat"), ("a", 1)
        self.assertFalse(memo.skip(key, sig, 100.0))
        memo.settle(key, sig, 100.0, live=False)
        self.assertTrue(memo.skip(key, sig, 110.0))
        self.assertFalse(memo.skip(key, sig, 131.0))

    def test_changed_signature_is_never_skipped(self):
        memo = SettledRows(recheck_s=30.0)
        memo.settle(("p", "c"), ("a", 1), 100.0, live=False)
        self.assertFalse(memo.skip(("p", "c"), ("a", 2), 101.0))

    def test_live_row_is_never_settled(self):
        memo = SettledRows(recheck_s=30.0)
        memo.settle(("p", "c"), ("a", 1), 100.0, live=False)
        memo.settle(("p", "c"), ("a", 1), 101.0, live=True)
        self.assertFalse(memo.skip(("p", "c"), ("a", 1), 102.0))

    def test_keys_are_scoped_to_store_path(self):
        class _S:
            def __init__(self, path):
                self.path = path

        a = SettledRows.key(_S("/a.sqlite"), "cursor", "u1")
        b = SettledRows.key(_S("/b.sqlite"), "cursor", "u1")
        self.assertNotEqual(a, b)


class MuseProcessSigTest(unittest.TestCase):
    def test_signature_moves_with_the_live_listing(self):
        from dash.sources import muse

        base = [{"pid": 10, "comm": "muse", "started_at": 5.0, "label": "ws1-a"}]
        same = [dict(base[0])]
        started = base + [{"pid": 11, "comm": "muse", "started_at": 6.0, "label": "ws1-b"}]
        self.assertEqual(muse._processes_sig(base), muse._processes_sig(same))
        self.assertNotEqual(muse._processes_sig(base), muse._processes_sig(started))
        self.assertNotEqual(muse._processes_sig(None), muse._processes_sig([]))
        jitter = [{"pid": 10, "comm": "muse", "started_at": 5.9, "label": "ws1-a"}]
        self.assertEqual(muse._processes_sig(base), muse._processes_sig(jitter))


class MuseTailStampTest(unittest.TestCase):
    def test_unchanged_file_is_not_retailed_until_it_changes(self):
        from dash.sources import muse

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "session.jsonl"
        path.write_text("{}\n")
        calls = []

        def fake_tail(store, p, sid, manifests, parent, budget, per_file, now, processes):
            calls.append(p)
            return budget  # reads nothing: the file is at its end

        real = muse._tail_file
        muse._tail_file = fake_tail
        self.addCleanup(setattr, muse, "_tail_file", real)
        muse._TAILED.clear()
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        files = [(str(path), "s1", None)]
        muse._drain_files(store, files, "muse:test", 10**6, 10**6, 1.0, None, {})
        muse._drain_files(store, files, "muse:test", 10**6, 10**6, 2.0, None, {})
        self.assertEqual(len(calls), 1)
        path.write_text("{}\n{}\n")
        muse._drain_files(store, files, "muse:test", 10**6, 10**6, 3.0, None, {})
        self.assertEqual(len(calls), 2)


class CursorSettledTest(unittest.TestCase):
    def test_finished_chat_written_once_then_skipped_then_live_written(self):
        from dash.sources import cursor

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        calls = []
        real = cursor._upsert
        cursor._upsert = lambda s, chat, now, model: calls.append(chat.get("pid"))
        self.addCleanup(setattr, cursor, "_upsert", real)
        chat = {"uuid": "u-settled-1", "label": "ws1-1 x", "updated_ms": 1000, "pid": None}
        cursor._upsert_settled(store, chat, 100.0)
        cursor._upsert_settled(store, chat, 102.0)
        self.assertEqual(len(calls), 1)
        chat["pid"] = 4242
        cursor._upsert_settled(store, chat, 104.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], 4242)


class AttributionScopeTest(unittest.TestCase):
    def test_rules_read_once_per_scope_and_refreshed_next_scope(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mapfile = Path(tmp.name) / "map.json"
        mapfile.write_text(json.dumps({"exact": {}, "regex": [], "sessions": {}}))
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        reads = []
        real = attribution.rules_snapshot

        def counting(path=None):
            reads.append(1)
            return real(mapfile)

        attribution.rules_snapshot = counting
        self.addCleanup(setattr, attribution, "rules_snapshot", real)
        with store.attribution_scope():
            for _ in range(5):
                store._refresh_attribution_snapshot()
            self.assertEqual(len(reads), 1)
        store._refresh_attribution_snapshot()
        self.assertEqual(len(reads), 2)


if __name__ == "__main__":
    unittest.main()
