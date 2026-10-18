"""Workstream lifecycle controls from the page: state, bulk, and undo.

Spins the real server over a temp bundle root holding synthetic
bundles in every state, then drives the endpoints the management
table uses: the transition table (``POST /api/workstreams/state`` runs
the same ``dash/lifecycle.py`` rules and writes as ``ws state``),
bulk batches with partial success (``POST /api/workstreams/bulk``),
Undo through the reverse transition (single and bulk), the
fail-closed guards, the read-only switch, the program/OVERVIEW kind
rules, cache invalidation, and ``allowed_actions`` on ``GET
/api/state`` (so the page never offers an invalid button).

The static half pins the post-tab chrome: no horizontal tab strip
markup or styles left, the switcher is a real button with
aria-expanded and aria-controls, every action button names its
workstream, writes go only through ``apiPost``, and the viewer-local
pins stay in localStorage behind try/catch.
"""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import http.client
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATIC = ROOT / "static"

from agent_dash import default_cfg  # noqa: E402
from dash.bundles import cmd_ws  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402


def _finish_plan(root: Path, ws: str) -> None:
    plan_path = root / ws / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for group in plan["dag"]["groups"]:
        for task in group["tasks"]:
            task["status"] = "done"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")


def _closing_report(root: Path, ws: str) -> None:
    docs = root / ws / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "closing-report.md").write_text("# Closing report\n\nShipped.\n", encoding="utf-8")


def _bundle(root: Path, ws: str, title: str, state: str = "active", done: bool = False) -> None:
    assert cmd_ws(["new", ws, title] + (["--state", state] if state != "active" else [])) == 0
    if done:
        _finish_plan(root, ws)
        _closing_report(root, ws)
        assert cmd_ws(["state", ws, "done"]) == 0


class ManageActionsTest(unittest.TestCase):
    def setUp(self):
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        self.old_ro = os.environ.get("AGENT_DASH_READONLY")
        self.bundles = tempfile.TemporaryDirectory()
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.bundles.name
        os.environ.pop("AGENT_DASH_READONLY", None)
        self.root = Path(self.bundles.name)
        _bundle(self.root, "WS1", "Alpha work")
        _bundle(self.root, "WS2", "Beta work", state="planned")
        _bundle(self.root, "WS3", "Gamma work", done=True)
        prog = self.root / "PROG1"
        prog.mkdir()
        (prog / "bundle.json").write_text(
            json.dumps({"id": "PROG1", "title": "Program lane", "kind": "program"}), encoding="utf-8"
        )
        over = self.root / "OVERVIEW"
        over.mkdir()
        (over / "bundle.json").write_text(
            json.dumps({"id": "OVERVIEW", "title": "Overview", "kind": "program"}), encoding="utf-8"
        )
        self.state = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.state.name) / "dash.sqlite")
        self.server = DashServer(self.store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.bundles.cleanup()
        self.state.cleanup()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        if self.old_ro is None:
            os.environ.pop("AGENT_DASH_READONLY", None)
        else:
            os.environ["AGENT_DASH_READONLY"] = self.old_ro

    def _post(self, path, body, origin=None, action_header=True, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            if action_header:
                headers["X-AgentDash-Action"] = "1"
            if origin is not None:
                headers["Origin"] = origin
            if host is not None:
                headers["Host"] = host
            payload = raw if raw is not None else json.dumps(body).encode()
            conn.request("POST", path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _state_post(self, body, **kw):
        kw.setdefault("origin", self.origin)
        return self._post("/api/workstreams/state", body, **kw)

    def _meta(self, ws):
        return json.loads((self.root / ws / "bundle.json").read_text(encoding="utf-8"))

    # The transition table: same rules and writes as ``ws state``.

    def test_pause_then_resume_round_trip(self):
        status, body = self._state_post({"ws": "WS1", "state": "paused"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "ws": "WS1", "state": "paused",
                                "prev_state": "active", "state_changed_at": body["state_changed_at"]})
        self.assertIsInstance(body["state_changed_at"], (int, float))
        self.assertEqual(self._meta("WS1")["state"], "paused")
        status, body = self._state_post({"ws": "WS1", "state": "active"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["state"], body["prev_state"]), ("active", "paused"))

    def test_start_planned(self):
        status, body = self._state_post({"ws": "WS2", "state": "active"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["state"], body["prev_state"]), ("active", "planned"))

    def test_mark_done(self):
        status, body = self._state_post({"ws": "WS1", "state": "done"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["prev_state"], "active")

    def test_drop_is_terminal(self):
        status, body = self._state_post({"ws": "WS1", "state": "dropped"})
        self.assertEqual(status, 200, body)
        status, body = self._state_post({"ws": "WS1", "state": "active"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "invalid_transition")
        self.assertIn("none (terminal state)", body["error"])

    def test_illegal_move_is_409_with_allowed_next_states(self):
        # paused -> done is not a legal move; the hint lists what is.
        self._state_post({"ws": "WS1", "state": "paused"})
        status, body = self._state_post({"ws": "WS1", "state": "done"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "invalid_transition")
        self.assertEqual(body["allowed"], ["active", "dropped"])
        self.assertIn("active", body["hint"])
        self.assertEqual(self._meta("WS1")["state"], "paused")

    def test_reopen_done_needs_a_note(self):
        status, body = self._state_post({"ws": "WS3", "state": "active"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "invalid_transition")
        status, body = self._state_post({"ws": "WS3", "state": "active", "note": "Undo from the dashboard"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["state"], body["prev_state"]), ("active", "done"))

    def test_noop_move_answers_ok_without_writing(self):
        before = self._meta("WS1").get("state_changed_at")
        status, body = self._state_post({"ws": "WS1", "state": "active"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["state"], body["prev_state"]), ("active", "active"))
        self.assertEqual(self._meta("WS1").get("state_changed_at"), before)

    def test_archived_is_not_a_state_target(self):
        status, body = self._state_post({"ws": "WS3", "state": "archived"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "bad_request")

    def test_unknown_ws_is_404(self):
        status, body = self._state_post({"ws": "WS9", "state": "paused"})
        self.assertEqual(status, 404, body)
        self.assertEqual(body["code"], "not_found")

    def test_bad_bodies_are_400(self):
        for raw in (b"{}", b'{"ws": "WS1"}', b'{"state": "paused"}', b"not json",
                    b'{"ws": "WS1", "state": "paused", "extra": 1}',
                    b'{"ws": "WS1", "state": "flying"}', b'{"ws": "../x", "state": "paused"}',
                    b'{"ws": "WS1", "state": "active", "note": ""}'):
            status, body = self._post("/api/workstreams/state", None, origin=self.origin, raw=raw)
            self.assertEqual(status, 400, (raw, body))
            self.assertEqual(body["code"], "bad_request")

    def test_program_and_overview_never_transition(self):
        for ws in ("PROG1", "OVERVIEW"):
            status, body = self._state_post({"ws": ws, "state": "paused"})
            self.assertEqual(status, 409, (ws, body))
            self.assertEqual(body["code"], "not_eligible")

    def test_state_change_records_history(self):
        self._state_post({"ws": "WS1", "state": "paused"})
        lines = (self.root / "WS1" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        self.assertEqual(row, {"kind": "state", "ts": row["ts"], "ws": "WS1", "from": "active", "to": "paused"})

    def test_guards_match_archive(self):
        status, body = self._state_post({"ws": "WS1", "state": "paused"}, origin="http://evil.example")
        self.assertEqual(status, 403, body)
        self.assertEqual(body["code"], "forbidden")
        status, body = self._state_post({"ws": "WS1", "state": "paused"}, action_header=False)
        self.assertEqual(status, 403, body)
        self.assertEqual(self._meta("WS1").get("state", "active"), "active")

    def test_read_only_disables_state_and_bulk(self):
        os.environ["AGENT_DASH_READONLY"] = "1"
        try:
            for path, body in (("/api/workstreams/state", {"ws": "WS1", "state": "paused"}),
                               ("/api/workstreams/bulk", {"action": "state", "state": "paused", "ws": ["WS1"]})):
                status, payload = self._post(path, body, origin=self.origin)
                self.assertEqual(status, 403, (path, payload))
                self.assertEqual(payload["code"], "read_only")
        finally:
            os.environ.pop("AGENT_DASH_READONLY", None)

    # Bulk: per-id results, partial success, never aborts midway.

    def test_bulk_state_partial_success(self):
        self._state_post({"ws": "WS3", "state": "active", "note": "reopen for bulk"})
        status, body = self._post("/api/workstreams/bulk",
                                  {"action": "state", "state": "paused", "ws": ["WS1", "WS3", "WS9"]},
                                  origin=self.origin)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["action"], "state")
        by_id = {item["ws"]: item for item in body["results"]}
        self.assertTrue(by_id["WS1"]["ok"])
        self.assertEqual((by_id["WS1"]["state"], by_id["WS1"]["prev_state"]), ("paused", "active"))
        self.assertTrue(by_id["WS3"]["ok"])
        self.assertFalse(by_id["WS9"]["ok"])
        self.assertEqual(by_id["WS9"]["code"], "not_found")
        self.assertEqual(sorted(body["changed"]), ["WS1", "WS3"])
        # The failure did not abort the rest, and each write was atomic.
        self.assertEqual(self._meta("WS1")["state"], "paused")
        self.assertEqual(self._meta("WS3")["state"], "paused")
        self.assertEqual(list((self.root / "WS1").glob("*.tmp")), [])

    def test_bulk_archive_done_then_bulk_restore_undoes_all(self):
        status, body = self._post("/api/workstreams/bulk",
                                  {"action": "archive", "ws": ["WS3"]}, origin=self.origin)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["changed"], ["WS3"])
        self.assertEqual(body["results"][0]["prev_state"], "done")
        status, current = self._get("/api/workstreams")
        self.assertNotIn("WS3", [row["ws"] for row in current["workstreams"]])
        # One Undo restores all that changed.
        revs = [{"ws": item["ws"], "state": item["prev_state"]} for item in body["results"] if item["ok"]]
        status, undo = self._post("/api/workstreams/bulk",
                                  {"action": "restore", "ws": [item["ws"] for item in revs]},
                                  origin=self.origin)
        self.assertEqual(status, 200, undo)
        self.assertTrue(undo["ok"])
        self.assertEqual(self._meta("WS3")["state"], "done")

    def test_bulk_bad_bodies_are_400(self):
        good = {"action": "state", "state": "paused", "ws": ["WS1"]}
        bads = [dict(good, action="launch"), dict(good, ws=[]),
                dict(good, ws=["WS1"] * 51), dict(good, state="archived"),
                {"action": "archive", "ws": ["WS1"], "state": "paused"},
                {"action": "archive"}, {"action": "archive", "ws": ["../x"]}]
        no_state = {"action": "state", "ws": ["WS1"]}
        for body in bads + [no_state]:
            status, payload = self._post("/api/workstreams/bulk", body, origin=self.origin)
            self.assertEqual(status, 400, (body, payload))
            self.assertEqual(payload["code"], "bad_request")

    def test_bulk_program_kind_is_per_id_409(self):
        status, body = self._post("/api/workstreams/bulk",
                                  {"action": "state", "state": "paused", "ws": ["WS1", "PROG1"]},
                                  origin=self.origin)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        by_id = {item["ws"]: item for item in body["results"]}
        self.assertTrue(by_id["WS1"]["ok"])
        self.assertFalse(by_id["PROG1"]["ok"])
        self.assertEqual(by_id["PROG1"]["code"], "not_eligible")
        self.assertEqual(body["changed"], ["WS1"])

    # Undo, caches, and allowed_actions.

    def test_undo_reverse_transition(self):
        status, body = self._state_post({"ws": "WS1", "state": "paused"})
        rev = {"ws": "WS1", "state": body["prev_state"]}
        status, back = self._state_post(rev)
        self.assertEqual(status, 200, back)
        self.assertEqual(back["state"], "active")

    def test_state_change_invalidates_caches(self):
        seq_before = self.store.seq
        self._state_post({"ws": "WS1", "state": "paused"})
        self.assertGreater(self.store.seq, seq_before)
        status, listed = self._get("/api/workstreams?state=paused")
        self.assertEqual(status, 200)
        self.assertIn("WS1", [row["ws"] for row in listed["workstreams"]])
        status, current = self._get("/api/workstreams?state=active")
        self.assertNotIn("WS1", [row["ws"] for row in current["workstreams"]])

    def test_allowed_actions_per_state(self):
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        by_id = {row["id"]: row for row in body["workstreams"]}
        self.assertEqual(by_id["WS1"]["allowed_actions"], ["pause", "mark_done", "drop"])
        self.assertEqual(by_id["WS2"]["allowed_actions"], ["start", "drop"])
        self.assertEqual(by_id["WS3"]["allowed_actions"], ["archive", "reopen"])
        status, rows = self._get("/api/workstreams?state=all")
        by_ws = {row["ws"]: row for row in rows["workstreams"]}
        self.assertEqual(by_ws["WS1"]["allowed_actions"], ["pause", "mark_done", "drop"])
        # Program bundles never gain workstream rows, so their empty
        # action set lives in the pure helper (pinned below) and the
        # endpoints refuse them (pinned above).
        from dash.actions import allowed_actions_for

        self.assertEqual(allowed_actions_for("PROG1", "active", "program"), [])
        self.assertEqual(allowed_actions_for("OVERVIEW", "active", "program"), [])
        self.assertEqual(allowed_actions_for("WS9", "active", None), [])
        self.assertEqual(allowed_actions_for("WS1", "bogus", "workstream"), [])

    def test_other_posts_stay_405(self):
        status, body = self._post("/api/workstreams/launch", {"ws": "WS1"}, origin=self.origin)
        self.assertEqual(status, 405)


class ManagePageRules(unittest.TestCase):
    """The post-tab chrome: no strip, a real switcher, named buttons."""

    def _page(self):
        return (STATIC / "index.html").read_text()

    def _scripts(self):
        return {path.name: path.read_text() for path in
                [STATIC / "core" / "switcher.js", STATIC / "core" / "manage.js",
                 STATIC / "core" / "state.js", STATIC / "cards" / "manage.css"]}

    def test_no_horizontal_tab_strip_left(self):
        page = self._page()
        for gone in ('id="tabs"', 'role="tablist"', "tabfilter", "tab-name", "tabs-empty", "22EE"):
            self.assertNotIn(gone, page, f"the tab strip is back: {gone}")
        self.assertNotIn('class="tab', page)
        css = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        self.assertNotIn(".tab", css)
        self.assertNotIn(".tabs", css)

    def test_switcher_is_a_real_button_with_expander(self):
        page = self._page()
        m = re.search(r'<button[^>]*id="ws-switcher"[^>]*>', page)
        self.assertIsNotNone(m, "no #ws-switcher button")
        tag = m.group(0)
        for attr in ('aria-expanded="false"', 'aria-controls="ws-switcher-pop"', 'aria-keyshortcuts="/"'):
            self.assertIn(attr, tag, f"the switcher button lost {attr}")
        for keep in ('id="overall-btn"', 'id="selected-chip"', 'id="ws-pins"'):
            self.assertIn(keep, page, f"the calm bar lost {keep}")

    def test_action_buttons_name_the_workstream(self):
        src = (STATIC / "core" / "manage.js").read_text()
        # Every action button carries an aria-label with the workstream id.
        self.assertIn('setAttribute("aria-label", d.label + " " + label)', src)
        self.assertIn("displayName(row.ws, row.name)", src)
        self.assertIn('"aria-label": "Select " + label', src)

    def test_writes_go_only_through_api_post(self):
        for name in ("switcher.js", "manage.js"):
            src = (STATIC / "core" / name).read_text()
            self.assertNotIn("fetch(", src, f"{name} fetches outside apiPost")
            self.assertNotIn("XMLHttpRequest", src, f"{name} posts outside apiPost")
        manage = (STATIC / "core" / "manage.js").read_text()
        # Single actions share one apiPost call site over the endpoint
        # table; bulk posts its own path.
        self.assertIn('apiPost("/api/workstreams/" + endpoint', manage)
        for name in ('"state"', '"archive"', '"restore"', '"/api/workstreams/bulk"'):
            self.assertIn(name, manage, f"manage.js never posts {name}")
        self.assertIn("X-AgentDash-Action", (STATIC / "core" / "api.js").read_text())

    def test_pins_live_in_local_storage_behind_try_catch(self):
        src = (STATIC / "core" / "switcher.js").read_text()
        self.assertIn("agent-dash-pins", src)
        self.assertIn("localStorage", src)
        # Both the read and the write guard storage access.
        self.assertGreaterEqual(src.count("try {"), 2)
        self.assertIn("up to three", src)

    def test_switcher_keyboard_contract(self):
        src = (STATIC / "core" / "switcher.js").read_text()
        for key in ('ev.key === "/"', '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"',
                    'role: "listbox"', 'role: "option"', 'aria-activedescendant'):
            self.assertIn(key, src, f"the switcher lost its keyboard contract: {key}")
        manage = (STATIC / "core" / "manage.js").read_text()
        for key in ("g then p", "g then c", "g then l", "g then a"):
            self.assertIn(key, manage, f"the Keys popover lost {key}")


if __name__ == "__main__":
    unittest.main()
