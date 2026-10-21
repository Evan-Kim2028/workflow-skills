"""Linear card payload: shape, counts by source over 24 h and 7 d, unlinked bundles, no token."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import linearview

NOW = 1_762_000_000.0
SECRET = "lin_api_SECRET_TOKEN_123"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bundle(root, ws, title, linked=True, rows=(), approvals=None, plan=None):
    base = Path(root) / ws
    _write(base / "bundle.json", json.dumps({"id": ws, "title": title, "order": int(ws[2:])}))
    if plan is None:
        plan = {"dag": {"groups": [{"id": "g", "tasks": [{"id": ws + ".a", "title": "First task"}]}]}}
    _write(base / "plan.json", json.dumps(plan))
    if rows is not None:
        _write(base / "history.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
    if approvals is not None:
        _write(base / "approvals.json", json.dumps({"ws": ws, "items": approvals}))
    if linked:
        link = {
            "version": 1, "team_key": "ENG",
            "project": {"id": "p", "name": title, "url": "https://linear.app/example/project/" + ws.lower()},
            "tasks": {}, "approvals": {"P-1": {"id": "x", "identifier": "ENG-9", "url": "https://linear.app/example/issue/ENG-9", "synced_status": "pending"}},
            "synced_at": NOW - 120, "last_error": None,
        }
        _write(base / "linear.json", json.dumps(link))
    return base


class LinearViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        linearview._CACHE.update({"key": None, "at": 0.0, "body": None})
        status = mock.patch("dash.linearview.linear_auth.status", return_value={"method": "oauth", "issuer": "x", "expires_at": None})
        status.start()
        self.addCleanup(status.stop)
        rows = [
            {"ts": NOW - 3600, "task": "WS4.a", "from": "todo", "to": "wip"},
            {"ts": NOW - 7200, "task": "WS4.a", "from": "wip", "to": "pr", "by": "linear"},
            {"ts": NOW - 3 * 86400, "task": "WS4.a", "from": "pr", "to": "done", "by": "linear"},
            {"ts": NOW - 20 * 86400, "task": "WS4.a", "from": "done", "to": "todo"},
            {"ts": "bad", "task": "WS4.a"},
        ]
        approvals = [
            {"id": "P-1", "step": "Approve the window", "status": "pending"},
            {"id": "P-2", "step": "Already decided", "status": "approved"},
        ]
        _bundle(self.root, "WS4", "Notes cutover", rows=rows, approvals=approvals)
        _bundle(self.root, "WS9", "Relay", linked=False, rows=[{"ts": NOW - 60, "task": "WS9.a", "to": "wip"}])

    def test_payload_shape_and_linked_only(self):
        body = linearview.build(NOW, self.root)
        self.assertEqual(body["signed_in"], "oauth")
        self.assertEqual([w["ws"] for w in body["workstreams"]], ["WS4"])
        ws = body["workstreams"][0]
        for key in ("ws", "title", "url", "synced_at", "last_error", "outbox", "last_update_at",
                    "changes_24h", "changes_7d", "recent", "pending_approvals"):
            self.assertIn(key, ws)
        self.assertEqual(ws["title"], "Notes cutover")
        self.assertEqual(ws["url"], "https://linear.app/example/project/ws4")
        self.assertEqual(ws["synced_at"], NOW - 120)
        self.assertEqual(ws["outbox"], 0)

    def test_counts_by_source_over_24h_and_7d(self):
        ws = linearview.build(NOW, self.root)["workstreams"][0]
        self.assertEqual(ws["changes_24h"], {"local": 1, "linear": 1})
        self.assertEqual(ws["changes_7d"], {"local": 1, "linear": 2})

    def test_recent_newest_first_with_source_and_titles(self):
        recent = linearview.build(NOW, self.root)["workstreams"][0]["recent"]
        stamps = [row["ts"] for row in recent]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual([row["by"] for row in recent], ["local", "linear", "linear", "local"])
        self.assertEqual(recent[0]["title"], "First task")
        self.assertEqual(recent[0]["from"], "todo")
        self.assertEqual(recent[0]["to"], "wip")

    def test_pending_approvals_map_to_linear_identifier(self):
        pending = linearview.build(NOW, self.root)["workstreams"][0]["pending_approvals"]
        self.assertEqual(pending, [{"id": "P-1", "step": "Approve the window", "identifier": "ENG-9",
                                    "url": "https://linear.app/example/issue/ENG-9"}])

    def test_signed_in_none_without_method(self):
        with mock.patch("dash.linearview.linear_auth.status", return_value={"method": None, "issuer": None, "expires_at": None}):
            self.assertEqual(linearview.build(NOW, self.root)["signed_in"], "none")

    def test_no_linked_bundles_is_an_empty_list(self):
        empty = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.assertEqual(linearview.build(NOW, empty)["workstreams"], [])

    def test_payload_never_carries_a_token(self):
        _bundle(self.root, "WS5", "Clients", approvals=[])
        link_path = self.root / "WS5" / "linear.json"
        link = json.loads(link_path.read_text())
        link["access_token"] = SECRET
        link_path.write_text(json.dumps(link))
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": SECRET}):
            text = json.dumps(linearview.build(NOW, self.root))
        self.assertNotIn(SECRET, text)
        self.assertNotIn("access_token", text)

    def test_cached_reuses_until_stamps_or_interval_change(self):
        first = linearview.cached(NOW, self.root)
        again = linearview.cached(NOW + 1, self.root)
        self.assertEqual(first, again)
        with mock.patch("dash.linearview.build", return_value={"signed_in": "none", "workstreams": []}) as build:
            linearview.cached(NOW + 1, self.root)
            build.assert_not_called()
            linearview.cached(NOW + linearview.REFRESH_S + 5, self.root)
            build.assert_called_once()
        with open(self.root / "WS4" / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": NOW - 10, "task": "WS4.a", "from": "pr", "to": "done", "by": "linear"}) + "\n")
        fresh = linearview.cached(NOW + 2, self.root)
        self.assertEqual(fresh["workstreams"][0]["changes_24h"], {"local": 1, "linear": 2})

    def test_cached_returns_a_copy(self):
        body = linearview.cached(NOW, self.root)
        body["workstreams"].append({"ws": "X"})
        self.assertEqual(len(linearview.cached(NOW, self.root)["workstreams"]), 1)


if __name__ == "__main__":
    unittest.main()
