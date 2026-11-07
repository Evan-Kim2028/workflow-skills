"""The process source reads a pid's label from the shared listing, not one ``ps -p`` per pid."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ProcessLabelNoForkTests(unittest.TestCase):
    """A matched parent's label comes from the shared listing, not one ``ps -p`` per pid."""

    def test_poll_reads_parent_label_from_listing(self):
        from dash.sources import process as P

        comm = sorted(P._COMM)[0]
        table = [{"pid": 4242, "ppid": 1, "comm": comm, "elapsed_s": 5, "rss_kb": 10}]
        keep = (P._ps, P._tokens_by_pid, P.label_from_pid, P._children)
        seen = []

        class Store:
            def add_process_sample(self, row):
                seen.append(row)

            def match_run(self, pid, label):
                return None

        try:
            P._ps = lambda: table
            P._tokens_by_pid = lambda: {4242: {"label": "from-listing", "direct": None}}
            P.label_from_pid = lambda pid: (_ for _ in ()).throw(AssertionError("forked"))
            P._children = lambda table: ({}, {4242: 10})
            P.poll(Store(), {}, 1000.0)
        finally:
            P._ps, P._tokens_by_pid, P.label_from_pid, P._children = keep
        self.assertEqual(seen[0]["label"], "from-listing")



if __name__ == "__main__":
    unittest.main()
