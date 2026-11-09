"""Poll incremental / bounded-walk fixes for the timeout window.

The six timed-out sources (muse, pi, manifests, commandcode, codex, grok_cli)
each poll every 2s. Under 38 live runs their directory walks and per-file
stats had to be bounded so a quiet tier costs stats, not walks, and a large
file costs a tail delta, not a full re-read. These tests pin those bounds.
"""
import os
os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import contextlib
import io
import json
import tempfile
import unittest
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import importlib.util

def load(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("dash_sources_" + name + "_inc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class Store:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
    def get_offset(self, path):
        return self.offsets.get(path)
    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset, size, mtime)

class CodexBoundedTest(unittest.TestCase):
    def test_rollouts_cached_within_ttl(self):
        codex = load("codex")
        codex.reset_state()
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "sessions", "2026", "11", "09")
            os.makedirs(root)
            p = os.path.join(root, "rollout-2026-11-09T00-00-00-11111111-1111-4111-8111-111111111111.jsonl")
            with open(p, "w") as fh:
                fh.write("{}\n")
            now = 1794255645.0
            first = codex._rollouts(root, None, now)
            self.assertEqual(first, [p])
            # Second call within TTL must not re-walk: create a second file
            # and confirm the cache still returns the first snapshot.
            p2 = os.path.join(root, "rollout-2026-11-09T00-00-01-22222222-2222-4222-8222-222222222222.jsonl")
            with open(p2, "w") as fh:
                fh.write("{}\n")
            second = codex._rollouts(root, None, now + 1.0)
            self.assertEqual(second, first)
            # After TTL the new file is visible (60s per bounded-walk TTL).
            third = codex._rollouts(root, None, now + 61.0)
            self.assertIn(p2, third)
            # Direct tests (now=None) always walk.
            fourth = codex._rollouts(root, None)
            self.assertIn(p2, fourth)

class CommandCodeBoundedTest(unittest.TestCase):
    def test_session_index_cached(self):
        cc = load("commandcode")
        cc._session_index_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            enc = os.path.join(tmp, "enc")
            os.makedirs(enc)
            sid = "abc123"
            path = os.path.join(enc, sid + ".jsonl")
            with open(path, "w") as fh:
                fh.write("{}\n")
            now = 1794255645.0
            first = cc._session_index(tmp, now)
            self.assertIn(sid, first)
            # New file not visible within TTL.
            sid2 = "def456"
            with open(os.path.join(enc, sid2 + ".jsonl"), "w") as fh:
                fh.write("{}\n")
            second = cc._session_index(tmp, now + 1.0)
            self.assertNotIn(sid2, second)
            third = cc._session_index(tmp, now + 61.0)
            self.assertIn(sid2, third)

class GrokBoundedTest(unittest.TestCase):
    def test_session_index_cached(self):
        grok = load("grok_cli")
        grok._SESSION_INDEX_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp:
            enc = os.path.join(tmp, "enc")
            os.makedirs(enc)
            sess = os.path.join(enc, "11111111-1111-4111-8111-111111111111")
            os.makedirs(sess)
            now = 1794255645.0
            first = grok._session_index(tmp, now)
            self.assertIn("11111111-1111-4111-8111-111111111111", first)
            # New session not visible within TTL.
            os.makedirs(os.path.join(enc, "22222222-2222-4222-8222-222222222222"))
            second = grok._session_index(tmp, now + 1.0)
            self.assertNotIn("22222222-2222-4222-8222-222222222222", second)
            third = grok._session_index(tmp, now + 61.0)
            self.assertIn("22222222-2222-4222-8222-222222222222", third)

class PiSessionCacheTest(unittest.TestCase):
    def test_stream_session_mtime_skip(self):
        pi = load("pi")
        pi._session_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            pad = os.path.join(tmp, "pad")
            os.makedirs(pad)
            path = os.path.join(pad, "stream-label.ndjson")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "session", "id": "sess-123"}) + "\n")
            first = pi._stream_session(pad, "label")
            self.assertEqual(first, "sess-123")
            # Unchanged file: second call reads from cache (no open).
            # Mutate mtime/size to force a miss and check it still works.
            with open(path, "a") as fh:
                fh.write(json.dumps({"type": "other"}) + "\n")
            # After append, size changes so sig misses and head is still session.
            second = pi._stream_session(pad, "label")
            self.assertEqual(second, "sess-123")

if __name__ == "__main__":
    unittest.main()
