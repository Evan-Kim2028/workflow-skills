"""Log search view: LogsQL quoting, limits, time window, down and empty
states, snippet caps, the card's no-load rule, and a real VictoriaLogs run.

The unit tests use an injected fake store. The integration class starts a
real VictoriaLogs binary on a free loopback port with a temp storage path
and synthetic rows only; it runs when AGENT_DASH_VLOGS_BIN names the binary
and skips otherwise. No real logs appear anywhere in this file.
"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import logsearch as L  # noqa: E402

NOW = 1_800_000_000.0  # fixed clock for the pure tests (2027-01-15 UTC)


def _ndjson(*rows):
    return "\n".join(json.dumps(r) for r in rows).encode("utf-8")


class FakeStore:
    """Records the LogsQL it was sent and answers with canned bytes."""

    def __init__(self, body=b"", error=None):
        self.body = body
        self.error = error
        self.calls = []

    def __call__(self, path, form):
        self.calls.append((path, dict(form)))
        if self.error is not None:
            raise self.error
        return self.body

    @property
    def query(self):
        return self.calls[-1][1]["query"]


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class QuotingTest(unittest.TestCase):
    def test_quote_escapes_quote_and_backslash(self):
        self.assertEqual(L.quote('a"b'), '"a\\"b"')
        self.assertEqual(L.quote("a\\b"), '"a\\\\b"')
        self.assertEqual(L.quote("a\\\"b"), '"a\\\\\\"b"')

    def test_control_characters_become_spaces(self):
        self.assertEqual(L.quote("a\nb\tc\x00d"), '"a b c d"')

    def test_injection_stays_one_phrase(self):
        hostile = 'x" | delete | stats count() n | "'
        query = L.search_query({"q": hostile}, NOW - 60, NOW, 10)
        # Outside quoted strings the only pipes are the three fixed stages.
        unquoted = re.sub(r'"(?:[^"\\]|\\.)*"', '""', query)
        self.assertEqual(unquoted.count("|"), 3, query)
        self.assertIn('i("x\\" | delete | stats count() n | \\"")', query)
        self.assertTrue(query.endswith("| limit 10 | fields _time, ws, label, type, path, _msg"), query)

    def test_operators_in_q_are_literal(self):
        for hostile in ("PASS OR ws:=WS9", "label:=x", "*", "(a)", "_msg:x", "a AND NOT b"):
            query = L.search_query({"q": hostile}, NOW - 60, NOW, 5)
            self.assertIn("i(%s)" % L.quote(hostile), query)
            self.assertEqual(query.count("i("), 1, query)

    def test_ws_label_type_are_exact_quoted_matches(self):
        query = L.search_query({"ws": 'WS1" OR x', "label": "ws1-WS1.0-probe", "type": "assistant"}, NOW - 60, NOW, 5)
        self.assertIn('ws:="WS1\\" OR x"', query)
        self.assertIn('label:="ws1-WS1.0-probe"', query)
        self.assertIn('type:="assistant"', query)

    def test_long_q_refused(self):
        with self.assertRaises(ValueError):
            L.search({"q": "a" * (L.Q_MAX_CHARS + 1)}, NOW, "http://127.0.0.1:1", FakeStore())


class LimitAndWindowTest(unittest.TestCase):
    def test_limit_default_and_cap(self):
        self.assertEqual(L.limit_of(None), 100)
        self.assertEqual(L.limit_of(""), 100)
        self.assertEqual(L.limit_of("250"), 250)
        self.assertEqual(L.limit_of("5000"), 500)

    def test_limit_rejects_bad_values(self):
        for bad in ("0", "-3", "abc", "1.5"):
            with self.assertRaises(ValueError, msg=bad):
                L.limit_of(bad)

    def test_query_carries_limit(self):
        store = FakeStore(b"")
        L.search({"limit": "9999"}, NOW, "http://127.0.0.1:1", store)
        self.assertIn("| limit 500 ", store.query)
        self.assertIn("| sort by (_time desc)", store.query)

    def test_default_window_is_last_30_days(self):
        since, until = L.window({}, NOW)
        self.assertEqual(until, NOW)
        self.assertEqual(since, NOW - 30 * 86400)
        store = FakeStore(b"")
        L.search({}, NOW, "http://127.0.0.1:1", store)
        self.assertIn("_time:[", store.query)
        self.assertIn("_time:[2026-12-16T", store.query)

    def test_every_search_carries_a_time_filter(self):
        store = FakeStore(b"")
        L.search({"q": "x", "ws": "WS1"}, NOW, "http://127.0.0.1:1", store)
        self.assertTrue(store.query.startswith("_time:["), store.query)

    def test_relative_and_iso_windows(self):
        since, until = L.window({"since": "7d"}, NOW)
        self.assertEqual((since, until), (NOW - 7 * 86400, NOW))
        self.assertEqual(
            L.parse_time("2027-01-10", NOW, 0, "until"),
            datetime(2027, 1, 10, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(L.parse_time("24h", NOW, 0, "since"), NOW - 86400)
        self.assertEqual(
            L.parse_time("2027-01-10T10:00:00+02:00", NOW, 0, "until"),
            datetime(2027, 1, 10, 8, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_bad_or_empty_window_refused(self):
        with self.assertRaises(ValueError):
            L.window({"since": "soon"}, NOW)
        with self.assertRaises(ValueError):
            L.window({"since": "2027-01-10", "until": "2027-01-09"}, NOW)


class DownAndEmptyTest(unittest.TestCase):
    def test_refused_port_answers_down_quickly(self):
        base = "http://127.0.0.1:%d" % _closed_port()
        started = time.monotonic()
        out = L.search({"q": "x"}, NOW, base)
        elapsed = time.monotonic() - started
        self.assertEqual(out["status"], "down")
        self.assertEqual(out["install"], "sh scripts/vlogs_install.sh")
        self.assertEqual(out["results"], [])
        self.assertLess(elapsed, 1.0)

    def test_facets_down_has_same_shape(self):
        base = "http://127.0.0.1:%d" % _closed_port()
        out = L.facets({}, NOW, base)
        self.assertEqual(out["status"], "down")
        self.assertEqual((out["workstreams"], out["labels"], out["types"], out["total"]), ([], [], [], 0))

    def test_unreachable_post_is_down_not_error(self):
        out = L.search({}, NOW, None, FakeStore(error=L.Unreachable("boom")))
        self.assertEqual(out["status"], "down")
        self.assertNotIn("error", out)

    def test_non_loopback_url_refused_as_down(self):
        self.assertIsNone(L.base_url({L.URL_ENV: "http://10.0.0.5:19428"}))
        self.assertIsNone(L.base_url({L.URL_ENV: "https://127.0.0.1:19428"}))
        self.assertEqual(L.base_url({}), L.DEFAULT_URL)
        self.assertEqual(L.base_url({L.URL_ENV: "http://localhost:9"}), "http://localhost:9")
        with mock.patch.dict(os.environ, {L.URL_ENV: "http://10.0.0.5:19428"}):
            out = L.search({}, NOW)
        self.assertEqual(out["status"], "down")

    def test_empty_store_is_ok_with_no_rows(self):
        out = L.search({}, NOW, "http://127.0.0.1:1", FakeStore(b""))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["count"], 0)

    def test_store_refusal_is_reported_without_a_500(self):
        out = L.search({}, NOW, None, FakeStore(error=L.StoreError("store answered 400")))
        self.assertEqual(out["status"], "error")
        self.assertIn("400", out["error"])


class SnippetCapTest(unittest.TestCase):
    def test_short_message_unchanged(self):
        self.assertEqual(L.snippet("PASS done", "pass"), "PASS done")

    def test_long_message_capped_and_keeps_match(self):
        msg = "a" * 2000 + " NEEDLE-7 " + "b" * 2000
        out = L.snippet(msg, "needle-7")
        self.assertLessEqual(len(out), L.SNIPPET_CHARS)
        self.assertIn("NEEDLE-7", out)

    def test_match_at_the_very_start_and_end(self):
        head = L.snippet("NEEDLE " + "z" * 3000, "needle")
        tail = L.snippet("z" * 3000 + " NEEDLE", "needle")
        self.assertIn("NEEDLE", head)
        self.assertIn("NEEDLE", tail)
        self.assertLessEqual(len(head), L.SNIPPET_CHARS)
        self.assertLessEqual(len(tail), L.SNIPPET_CHARS)

    def test_no_match_falls_back_to_the_start(self):
        out = L.snippet("q" * 5000, "absent")
        self.assertLessEqual(len(out), L.SNIPPET_CHARS)
        self.assertTrue(out.startswith("q"))

    def test_search_rows_carry_capped_snippet_and_text(self):
        msg = "x" * 3000 + " hit " + "y" * 3000
        body = _ndjson({"_time": "2027-01-14T10:00:00Z", "_msg": msg, "ws": "WS1",
                        "label": "ws1-WS1.0-probe", "type": "assistant", "path": "hub/a.jsonl"})
        out = L.search({"q": "hit"}, NOW, None, FakeStore(body))
        (row,) = out["results"]
        self.assertLessEqual(len(row["snippet"]), L.SNIPPET_CHARS)
        self.assertIn("hit", row["snippet"])
        self.assertEqual(len(row["text"]), L.DETAIL_CHARS)
        self.assertTrue(row["text_truncated"])
        self.assertEqual(row["path"], "hub/a.jsonl")
        self.assertEqual(row["ws"], "WS1")

    def test_newest_first_is_the_store_order(self):
        body = _ndjson(
            {"_time": "2027-01-14T10:00:00Z", "_msg": "new"},
            {"_time": "2027-01-13T10:00:00Z", "_msg": "old"},
        )
        out = L.search({}, NOW, None, FakeStore(body))
        self.assertEqual([r["snippet"] for r in out["results"]], ["new", "old"])


class FacetTest(unittest.TestCase):
    def test_counts_merge_and_sort(self):
        body = _ndjson(
            {"ws": "WS1", "label": "a", "type": "assistant", "n": "5"},
            {"ws": "WS1", "label": "a", "type": "user", "n": "2"},
            {"ws": "WS2", "label": "b", "type": "assistant", "n": "9"},
        )
        store = FakeStore(body)
        out = L.facets({}, NOW, None, store)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["total"], 16)
        self.assertEqual(out["workstreams"], [{"ws": "WS2", "count": 9}, {"ws": "WS1", "count": 7}])
        self.assertEqual(out["labels"][0], {"label": "b", "ws": "WS2", "count": 9})
        self.assertEqual(out["labels"][1], {"label": "a", "ws": "WS1", "count": 7})
        self.assertEqual(out["types"], [{"type": "assistant", "count": 14}, {"type": "user", "count": 2}])
        self.assertFalse(out["truncated"])
        self.assertIn("| stats by (ws, label, type) count() n | limit %d" % L.FACET_ROWS, store.query)
        self.assertIn("_time:[", store.query)

    def test_empty_store_facets(self):
        out = L.facets({}, NOW, None, FakeStore(b""))
        self.assertEqual((out["total"], out["workstreams"], out["labels"]), (0, [], []))


class RoutingTest(unittest.TestCase):
    def test_unknown_logs_path_is_not_ours(self):
        self.assertIsNone(L.handle("/api/logs/nope", {}, NOW))

    def test_known_paths_answer_200(self):
        with mock.patch.dict(os.environ, {L.URL_ENV: "http://127.0.0.1:%d" % _closed_port()}):
            status, body = L.handle("/api/logs/search", {"q": "x"}, NOW)
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "down")
            status, body = L.handle("/api/logs/facets", {}, NOW)
            self.assertEqual((status, body["status"]), (200, "down"))


class CardNoLoadTest(unittest.TestCase):
    """The card never fetches while the page builds, and its spec is pure."""

    def test_spec_and_closed_card_do_not_fetch(self):
        src = (ROOT / "static" / "cards" / "log-search.js").read_text()
        harness = r"""
const fs = require('fs');
const calls = [];
global.window = globalThis;
global.fetch = function (u) { calls.push(['fetch', u]); return new Promise(function () {}); };
global.api = function (u) { calls.push(['api', u]); return new Promise(function () {}); };
global.keyOf = function (x) { return JSON.stringify(x); };
global.el = function (tag, attrs, kids) {
  var node = {tag: tag, kids: [], appendChild: function (c) { this.kids.push(c); return c; },
    addEventListener: function () {}, setAttribute: function () {}, removeChild: function () {}, firstChild: null};
  (kids || []).forEach(function (k) { if (k) node.kids.push(k); });
  return node;
};
global.document = {getElementById: function () { return null; }};
global.fmtN = function (n) { return String(n); };
global.fmtDate = function () { return ''; };
eval(fs.readFileSync(process.argv[1], 'utf8').replace('})(window);', '})(global);'));
const spec = global.logSearchSpec({}, {});
if (spec[0] !== 'logsearch') throw new Error('bad section name');
global.logSearchCard();
if (calls.length) throw new Error('loaded while closed: ' + JSON.stringify(calls));
console.log('OK');
"""
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        proc = subprocess.run(
            ["node", "-e", harness, str(ROOT / "static" / "cards" / "log-search.js")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)
        self.assertNotIn("innerHTML", src)
        self.assertNotIn("insertAdjacentHTML", src)

    def test_card_is_registered_and_loaded(self):
        page = (ROOT / "static" / "index.html").read_text()
        self.assertIn('<script src="/static/cards/log-search.js"></script>', page)
        self.assertIn('<link rel="stylesheet" href="/static/cards/log-search.css">', page)
        self.assertLess(page.index("/static/cards/log-search.js"), page.index("/static/cards/registry.js"))
        registry = (ROOT / "static" / "cards" / "registry.js").read_text()
        self.assertEqual(registry.count('{id: "logsearch"'), 1)
        self.assertIn('scope: "project"', registry.split('{id: "logsearch"')[1].split("}}")[0])

    def test_card_renders_text_only(self):
        src = (ROOT / "static" / "cards" / "log-search.js").read_text()
        # Results and detail go through textContent; the only html-ish
        # assignments in the file would be a bug.
        self.assertNotRegex(src, r"\.innerHTML\s*=")
        self.assertIn("textContent = row.text", src)


class ServerRouteTest(unittest.TestCase):
    """The one dispatch line in dash/server.py answers the new paths."""

    @classmethod
    def setUpClass(cls):
        from agent_dash import default_cfg
        from dash.server import DashServer
        from dash.sources import Registry
        from dash.store import Store

        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / "dash.sqlite")
        cls.server = DashServer(cls.store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.env = mock.patch.dict(os.environ, {L.URL_ENV: "http://127.0.0.1:%d" % _closed_port()})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.server.stop.set()
        cls.server.shutdown()
        cls.thread.join(3)
        cls.server.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_down_store_is_200_with_state(self):
        status, body = self._get("/api/logs/search?q=needle")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "down")
        self.assertEqual(payload["install"], "sh scripts/vlogs_install.sh")

    def test_facets_route(self):
        status, body = self._get("/api/logs/facets")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "down")

    def test_bad_parameters_are_400(self):
        self.assertEqual(self._get("/api/logs/search?limit=0")[0], 400)
        self.assertEqual(self._get("/api/logs/search?since=soon")[0], 400)

    def test_unknown_logs_path_is_404(self):
        self.assertEqual(self._get("/api/logs/other")[0], 404)


# ----------------------------------------------------------------- real store


VLOGS_BIN = os.environ.get("AGENT_DASH_VLOGS_BIN", "")


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _post(url, data=b"", headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get(url, timeout=1):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


@unittest.skipUnless(VLOGS_BIN and os.path.exists(VLOGS_BIN), "set AGENT_DASH_VLOGS_BIN to a VictoriaLogs binary")
class RealStoreTest(unittest.TestCase):
    """Starts a real store on a free loopback port and ingests synthetic rows."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="logsearch-vl-")
        cls.port = _free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.proc = subprocess.Popen(
            [VLOGS_BIN, "-storageDataPath=%s/data" % cls.tmp, "-httpListenAddr=127.0.0.1:%d" % cls.port,
             "-retentionPeriod=1y", "-loggerOutput=stderr"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls._wait_healthy()
        cls._ingest()

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _wait_healthy(cls):
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if _get(cls.base + "/health") is not None:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("store did not start")

    @classmethod
    def _ingest(cls):
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(120):
            ts = (now - timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            ws = "WS%d" % (i % 3 + 1)
            msg = "PASS synthetic event %d " % i + "filler " * 30
            if i % 10 == 0:
                msg += " needle-%d" % i
            if i == 3:
                msg = 'quoted "value" and back\\slash and pipe | star * OR ws:=WS9'
            rows.append({"_time": ts, "_msg": msg, "ws": ws,
                         "label": "ws%d-WS%d.%d-probe" % (i % 3 + 1, i % 3 + 1, i % 2),
                         "type": ["assistant", "user"][i % 2], "kind": "out",
                         "path": "hub/ws%d/out-%d.jsonl" % (i % 3 + 1, i)})
        old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows.append({"_time": old, "_msg": "ancient needle-old", "ws": "WS1", "label": "old",
                     "type": "assistant", "kind": "out", "path": "hub/old.jsonl"})
        body = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
        _post(cls.base + "/insert/jsonline?" + urllib.parse.urlencode(
            {"_stream_fields": "ws,label", "_msg_field": "_msg", "_time_field": "_time"}),
            data=body, headers={"Content-Type": "application/stream+json"})
        _post(cls.base + "/internal/force_flush")

    def _search(self, **params):
        base_env = mock.patch.dict(os.environ, {L.URL_ENV: self.base})
        base_env.start()
        try:
            return L.search(params, time.time(), self.base)
        finally:
            base_env.stop()

    def test_phrase_match_is_case_insensitive_and_literal(self):
        out = self._search(q="needle-10")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["count"], 1)
        self.assertIn("needle-10", out["results"][0]["snippet"])

    def test_old_rows_excluded_by_default_window(self):
        out = self._search(q="ancient")
        self.assertEqual(out["count"], 0)
        out = self._search(q="ancient", since="90d")
        self.assertEqual(out["count"], 1)

    def test_pass_matches_any_case(self):
        # Row 3 is the hostile-text row and carries no "PASS": 119 of 120.
        out = self._search(q="pass", limit="500")
        self.assertEqual(out["count"], 119)

    def test_newest_first_and_limit(self):
        out = self._search(q="PASS", limit="10")
        self.assertEqual(out["count"], 10)
        stamps = [r["ts"] for r in out["results"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertIn("| sort by (_time desc) | limit 10", L.search_query({"q": "PASS"}, NOW - 60, NOW, 10))

    def test_exact_filters(self):
        out = self._search(ws="WS2", limit="500")
        self.assertTrue(out["results"])
        self.assertTrue(all(r["ws"] == "WS2" for r in out["results"]))
        out = self._search(ws="WS2", type="user", label="ws2-WS2.1-probe", limit="500")
        self.assertTrue(all(r["type"] == "user" and r["label"] == "ws2-WS2.1-probe" for r in out["results"]))

    def test_hostile_q_is_harmless(self):
        for hostile in ['"', "\\", '" | delete', "a\" OR ws:=WS9", "*", "|", "(x"]:
            out = self._search(q=hostile)
            self.assertEqual(out["status"], "ok", hostile)
        out = self._search(q='quoted "value"')
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["count"], 1)
        self.assertIn('back\\slash', out["results"][0]["text"])

    def test_facets_count_the_window(self):
        base_env = mock.patch.dict(os.environ, {L.URL_ENV: self.base})
        base_env.start()
        try:
            out = L.facets({}, time.time(), self.base)
        finally:
            base_env.stop()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["total"], 120)
        self.assertEqual(sum(row["count"] for row in out["workstreams"]), 120)
        self.assertEqual({row["type"] for row in out["types"]}, {"assistant", "user"})

    def test_latency_under_target(self):
        samples = []
        for _ in range(15):
            started = time.monotonic()
            out = self._search(q="needle", limit="100")
            samples.append((time.monotonic() - started) * 1000.0)
            self.assertEqual(out["status"], "ok")
        samples.sort()
        median = samples[len(samples) // 2]
        print("\nlogsearch latency on temp store: median %.1f ms, max %.1f ms" % (median, samples[-1]))
        self.assertLess(median, 300.0)

    def test_store_down_after_stop_is_state_not_error(self):
        self.__class__.proc.terminate()
        self.__class__.proc.wait(10)
        out = self._search(q="needle")
        self.assertEqual(out["status"], "down")
        self.assertEqual(out["results"], [])


if __name__ == "__main__":
    unittest.main()
