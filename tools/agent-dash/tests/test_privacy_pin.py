"""Privacy pin: no prompt/argv/env/secret reaches any endpoint payload.

Seeds a temp dashboard through the real parsing paths (a claude
transcript fixture, muse/cursor out files via manifests, process samples
parsed from a fake argv line, generic report lines), then GETs every
endpoint and asserts no sentinel appears in any response body. The one
argv value ever kept (the worker's ``--model`` flag value) must still be
retained. A second test pins that no code path under ``dash/sources``
opens a source file for writing.
"""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import http.client
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_dash import default_cfg  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.sources import claude  # noqa: E402
from dash.sources import generic  # noqa: E402
from dash.sources import manifests  # noqa: E402
from dash.sources import process  # noqa: E402
from dash.store import Store  # noqa: E402

# Sentinel secrets. Fake values that live only in temp source files and
# this test: a fake prompt body, a fake argv token, a fake env value,
# and a fake API key. None of them may appear in any endpoint payload.
PROMPT_SENTINEL = "PIN_PROMPT_BODY_sentinel_7f3a2c"
ARGV_SENTINEL = "PIN_ARGV_TOKEN_sentinel_9c1e44"
ENV_SENTINEL = "PIN_ENV_VALUE_sentinel_4b2d91"
APIKEY_SENTINEL = "PIN_API_KEY_sentinel_8e5f07"
SENTINELS = (PROMPT_SENTINEL, ARGV_SENTINEL, ENV_SENTINEL, APIKEY_SENTINEL)

# The one argv value ever kept: the worker's --model flag value.
MODEL_OK = "pin-probe-model"

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PARENT = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _write(path, data, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))


def _assistant_line(mid, output, content, model="claude-sonnet-5-5"):
    rec = {
        "type": "assistant",
        "timestamp": "2026-10-16T00:00:00.000Z",
        "sessionId": SID,
        "cwd": "/tmp/pin-probe",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": output,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
        },
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _user_line(text):
    rec = {
        "type": "user",
        "timestamp": "2026-10-16T00:00:01.000Z",
        "sessionId": SID,
        "cwd": "/tmp/pin-probe",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def _start(store, cfg=None):
    registry = Registry([])
    server = DashServer(
        store, registry, "127.0.0.1", 0, cfg=cfg or default_cfg()
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    return server, thread


def _stop(server, thread):
    server.stop.set()
    server.shutdown()
    thread.join(3)
    server.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _first_event(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream")
    response = conn.getresponse()
    status = response.status
    buf = b""
    try:
        while b"\n\n" not in buf:
            chunk = response.read(1)
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    return status, buf


PATHS = [
    "/api/project",
    "/api/snapshot?ws=all",
    "/api/runs?limit=50",
    "/api/runs?limit=50&include=jobs",
    "/api/usage?group=model",
    "/api/concurrency",
    "/api/sessions",
    "/api/workstreams",
    "/api/workstreams?state=all",
    "/api/archive",
    "/api/reconcile?hours=24",
    "/api/plan?ws=WS1",
    "/api/replay?ws=WS1",
    "/api/bundles",
    "/api/bundle?ws=WS1",
    "/api/approvals?ws=all",
    "/api/decisions",
    "/api/risks",
    "/api/project/feed",
    "/api/project/feed?limit=3",
    "/api/health",
    "/api/doctor",
    "/api/state",
    "/",
    "/plan",
    "/static/index.html",
    "/static/tabs.js",
    "/static/taskmap.json",
    "/docs/problems/dep-cycle.md",
]


class PrivacyPinEndpointTest(unittest.TestCase):
    """Every endpoint stays free of prompt/argv/env/secret material."""

    def setUp(self):
        claude.reset_state()
        manifests.reset_state()
        self._live_patch = mock.patch(
            "dash.server._live_workers", return_value=[]
        )
        self._live_patch.start()
        self.addCleanup(self._live_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.root = self.tmp.name
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        # /api/project reads the data root: keep it inside the temp tree
        # so the sweep never touches the machine's real project file.
        self._dataroot_patch = mock.patch.dict(
            os.environ, {"AGENT_DASH_DATA_ROOT": os.path.join(self.root, "dataroot")}
        )
        self._dataroot_patch.start()
        self.addCleanup(self._dataroot_patch.stop)
        self.store = Store(Path(self.root) / "dash.sqlite")
        self._seed()
        self.hub = os.path.join(self.root, "hub")
        os.makedirs(self.hub, exist_ok=True)
        self._seed_hub()
        cfg = default_cfg()
        cfg["hub_dir"] = self.hub
        self.server, self.thread = _start(self.store, cfg)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()
        manifests.reset_state()
        claude.reset_state()

    def _seed(self):
        now = self.now
        # 1. Claude transcript: user prompt body plus assistant text carry
        # sentinels through the real transcript parse.
        projects = os.path.join(self.root, "projects")
        sessions = os.path.join(self.root, "sessions")
        os.makedirs(projects, exist_ok=True)
        os.makedirs(sessions, exist_ok=True)
        user_text = (
            "fake prompt body %s with fake argv --token %s "
            "fake env PROBE_ENV=%s fake key %s"
            % (PROMPT_SENTINEL, ARGV_SENTINEL, ENV_SENTINEL, APIKEY_SENTINEL)
        )
        blob = _user_line(user_text)
        blob += _assistant_line("m1", 10, "reply text %s" % PROMPT_SENTINEL)
        _write(os.path.join(projects, "enc", "%s.jsonl" % SID), blob, now)
        claude.poll(
            self.store,
            {
                "claude_projects": projects,
                "claude_sessions": sessions,
                "dir_scan_s": 0,
                "live_window_s": 36 * 3600,
                "running_mtime_s": 900,
                "read_budgets": {"per_file": 1024 * 1024, "per_tick": 8 * 1024 * 1024},
            },
            now,
        )
        # 2/3. Muse and cursor out files via the manifest parse. The out
        # files hold verdicts plus sentinel junk; only the verdict may land.
        scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")
        epoch = int(now) - 100
        _write(
            os.path.join(scratch, "muse-sessions.tsv"),
            "ws7-WS7.1-pin-muse\t%s\t%d\n" % (SID, epoch),
            now,
        )
        _write(
            os.path.join(scratch, "muse", "out-ws7-WS7.1-pin-muse.md"),
            "PASS\n%s\n%s\n%s\n%s\n"
            % (PROMPT_SENTINEL, ARGV_SENTINEL, ENV_SENTINEL, APIKEY_SENTINEL),
            now,
        )
        _write(
            os.path.join(scratch, "cursor-sessions.tsv"),
            "ws7-WS7.1-pin-cursor\t-\t%d\n" % epoch,
            now,
        )
        _write(
            os.path.join(scratch, "cursor", "out-ws7-WS7.1-pin-cursor.md"),
            "PASS\nnotes %s key %s\n" % (ARGV_SENTINEL, APIKEY_SENTINEL),
            now,
        )
        manifests.poll(
            self.store,
            {
                "scratch_root": self.root,
                "state_dir": self.state_dir,
                "dir_scan_s": 0,
                "live_window_s": 36 * 3600,
                "read_budgets": {"per_file": 1024 * 1024, "per_tick": 8 * 1024 * 1024},
            },
            now,
        )
        # 4. Process sample: argv carries sentinels plus the allowed
        # --model flag. Only the flag value may be retained.
        argv = (
            "78901 /opt/worker/muse-bin exec --trust-workspace "
            "--prompt-file /tmp/pads/scratchpad/muse/ws7-WS7.1-pin-probe.md "
            "--session-id 11111111-1111-4111-8111-111111111111 "
            "--model %s --token %s --api-key %s --env PROBE_ENV=%s note %s "
            "[label: ws7-WS7.1-pin-probe]"
            % (MODEL_OK, ARGV_SENTINEL, APIKEY_SENTINEL, ENV_SENTINEL, PROMPT_SENTINEL)
        )
        label = process.label_from_args(argv)
        self.assertEqual(label, "ws7-WS7.1-pin-probe")
        model = process.model_from_args(argv)
        self.assertEqual(model, MODEL_OK)
        run_id = "muse:11111111-1111-4111-8111-111111111111:ws7-WS7.1-pin-probe"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": MODEL_OK,
                "label": label,
                "status": "running",
                "started_at": now - 5,
            }
        )
        self.store.add_process_sample(
            {
                "ts": now,
                "pid": 78901,
                "comm": "muse-bin",
                "rss_kb": 123,
                "elapsed_s": 5.0,
                "label": label,
                "run_id": run_id,
                "model": model,
            }
        )
        # 5. Generic report lines for a normal run.
        generic_run = "generic:pin-probe-session:ws7-WS7.1-pin-generic"
        lines = [
            json.dumps(
                {
                    "type": "run",
                    "label": "ws7-WS7.1-pin-generic",
                    "status": "running",
                    "model": MODEL_OK,
                    "session_id": "pin-probe-session",
                    "started_at": now - 60.0,
                }
            ),
            json.dumps(
                {
                    "type": "usage",
                    "session_id": "pin-probe-session",
                    "run_id": generic_run,
                    "tool": "generic",
                    "message_id": "turn-1",
                    "model": MODEL_OK,
                    "input": 100,
                    "output": 50,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                }
            ),
        ]
        with open(os.path.join(self.state_dir, "generic.jsonl"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        generic.poll(self.store, {"state_dir": self.state_dir}, now)

    def _seed_hub(self):
        # Hub decisions/risks with sentinels only in cells the parser
        # must drop: an over-long free-text column and a path-like
        # column. Short exposed cells carry no sentinel.
        long_decision = ("rationale %s token %s " % (PROMPT_SENTINEL, ARGV_SENTINEL)) * 12
        with open(os.path.join(self.hub, "decisions.tsv"), "w") as fh:
            fh.write("date\tid\tdecision\twhy\tstatus\tlink\n")
            fh.write(
                "2026-10-18\tD-99\tShort probe title\t%s\tproposed\treports/%s.md\n"
                % (long_decision, ENV_SENTINEL)
            )
        long_risk = ("background %s key %s " % (PROMPT_SENTINEL, APIKEY_SENTINEL)) * 12
        with open(os.path.join(self.hub, "risks.tsv"), "w") as fh:
            fh.write("id\ttitle\tnotes\tlink\n")
            fh.write(
                "R-99\tShort probe risk\t%s\tnotes/%s\n" % (long_risk, ENV_SENTINEL)
            )

    def test_hub_rows_expose_short_cells_only(self):
        status, body = _get(self.port, "/api/decisions")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8", "replace"))
        self.assertEqual(payload["source"], {"present": True})
        (row,) = payload["decisions"]
        self.assertEqual(row["title"], "Short probe title")
        self.assertNotIn("why", row)
        self.assertNotIn("link", row)
        status, body = _get(self.port, "/api/risks")
        self.assertEqual(status, 200)
        risks = json.loads(body.decode("utf-8", "replace"))["risks"]
        filed = [risk for risk in risks if risk["id"] == "R-99"]
        self.assertEqual(len(filed), 1)
        risk = filed[0]
        self.assertEqual(risk["title"], "Short probe risk")
        self.assertNotIn("notes", risk)
        self.assertNotIn("link", risk)

    def test_seed_parsed_through_real_paths(self):
        # The sweep below is only meaningful when the sentinels really
        # passed through the parsers, so pin the parsed rows first.
        rows = self.store.query_runs(self.now, None, None, None, 200, True)
        by_label = {row["label"]: row for row in rows}
        muse_run = by_label.get("ws7-WS7.1-pin-muse")
        self.assertIsNotNone(muse_run)
        self.assertEqual(muse_run.get("verdict"), "PASS")
        cursor_run = by_label.get("ws7-WS7.1-pin-cursor")
        self.assertIsNotNone(cursor_run)
        self.assertEqual(cursor_run.get("verdict"), "PASS")
        generic_run = by_label.get("ws7-WS7.1-pin-generic")
        self.assertIsNotNone(generic_run)
        self.assertEqual(generic_run.get("model"), MODEL_OK)
        claude_run = self.store.get_run("claude-main:%s:main" % SID)
        self.assertIsNotNone(claude_run)

    def test_no_sentinel_in_any_endpoint(self):
        bodies = {}
        for path in PATHS:
            status, body = _get(self.port, path)
            self.assertEqual(status, 200, path)
            bodies[path] = body.decode("utf-8", "replace")
        status, event = _first_event(self.port)
        self.assertEqual(status, 200)
        self.assertIn(b"event: tick\n", event)
        bodies["/api/stream"] = event.decode("utf-8", "replace")
        for path, text in bodies.items():
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, text, "%s leaks into %s" % (sentinel, path))

    def test_allowed_model_value_retained(self):
        status, body = _get(self.port, "/api/runs?limit=50")
        self.assertEqual(status, 200)
        self.assertIn(MODEL_OK, body.decode("utf-8", "replace"))
        status, body = _get(self.port, "/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        self.assertIn(MODEL_OK, text)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)


class ArchivePrivacyTest(unittest.TestCase):
    """Closing-report bodies never reach the API: only basenames do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        dest = os.path.join(self.wsroot, "WS41")
        os.makedirs(os.path.join(dest, "reports"))
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump(
                {"id": "WS41", "title": "Probe WS41", "short": "WS41",
                 "order": 41, "kind": "workstream", "state": "archived",
                 "state_changed_at": int(self.now) - 50,
                 "archived_at": int(self.now) - 5},
                fh,
            )
        with open(os.path.join(dest, "summary.json"), "w") as fh:
            json.dump({"tasks_done": 1, "tasks_total": 1, "report": "reports/2026-10-18-closing.md"}, fh)
        with open(os.path.join(dest, "reports", "2026-10-18-closing.md"), "w") as fh:
            fh.write("closing body with %s and %s and %s and %s\n" % SENTINELS)
        docs_dir = os.path.join(self.wsroot, "WS40", "docs")
        os.makedirs(docs_dir)
        with open(os.path.join(self.wsroot, "WS40", "bundle.json"), "w") as fh:
            json.dump(
                {"id": "WS40", "title": "Probe WS40", "short": "WS40",
                 "order": 40, "kind": "workstream", "state": "archived",
                 "state_changed_at": int(self.now) - 60,
                 "archived_at": int(self.now) - 500},
                fh,
            )
        with open(os.path.join(self.wsroot, "WS40", "summary.json"), "w") as fh:
            json.dump({"tasks_done": 1, "tasks_total": 1, "closing_report": True,
                       "closing_report_name": "closing-report.md"}, fh)
        with open(os.path.join(docs_dir, "closing-report.md"), "w") as fh:
            fh.write("docs body with %s and %s and %s and %s\n" % SENTINELS)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._live = mock.patch("dash.server._live_workers", return_value=[])
        self._live.start()
        self.addCleanup(self._live.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = _start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_archive_serves_basename_but_no_body(self):
        status, body = _get(self.port, "/api/archive")
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        payload = json.loads(text)
        self.assertEqual(payload["archive"][0]["report"], "2026-10-18-closing.md")
        by_id = {card["id"]: card for card in payload["archive"]}
        self.assertEqual(by_id["WS40"]["report"], "closing-report.md")
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
        status, body = _get(self.port, "/api/workstreams?state=all")
        self.assertEqual(status, 200)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, body.decode("utf-8", "replace"))


class SourceWriteTest(unittest.TestCase):
    """No code path under dash/sources writes to a source file."""

    # Documented state-dir writers live outside dash/sources (offsets go
    # through Store; usage appends go through scripts/cursor_usage_hook.py),
    # so this allowlist stays empty: any write-mode open under
    # dash/sources is a privacy bug.
    WRITE_ALLOWLIST = frozenset()

    WRITE_RE = re.compile(
        r"open\s*\([^)]*['\"][^'\"]*[wa][^'\"]*['\"]"
        r"|os\.open\s*\([^)]*O_(WRONLY|RDWR|CREAT|APPEND)"
        r"|write_text\s*\("
        r"|write_bytes\s*\("
    )

    def test_no_write_opens_in_sources(self):
        sources = ROOT / "dash" / "sources"
        hits = []
        for path in sorted(sources.glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if self.WRITE_RE.search(line):
                    rel = "%s:%d" % (path.name, lineno)
                    if rel not in self.WRITE_ALLOWLIST and path.name not in self.WRITE_ALLOWLIST:
                        hits.append("%s: %s" % (rel, line.strip()))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
