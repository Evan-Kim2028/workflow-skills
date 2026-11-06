"""Redaction, row building and the loopback VictoriaLogs client.

Covers ``dash/vlogs.py``: every secret shape named in the log-archive
contract is redacted before a row can leave the process, ordinary words
that look like keys are not, rows over the text cap are truncated and
flagged while staying valid JSON, the client refuses non-loopback hosts,
and inserts carry ``Content-Type: application/stream+json`` (the pilot
found that a wrong type can ingest zero rows, so it is pinned here).

A loopback fake server records requests; a live store is only touched
when ``VLOGS_LIVE_TEST=1`` is set (read-only check against 127.0.0.1:19428).
"""

import http.server
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import vlogs  # noqa: E402


class RedactTest(unittest.TestCase):
    def assertRedacted(self, text, secret):
        out, n = vlogs.redact(text)
        self.assertNotIn(secret, out)
        self.assertGreaterEqual(n, 1)

    def test_api_key_shapes(self):
        self.assertRedacted("key sk-abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnopqrstuvwxyz0123")
        self.assertRedacted("sk-api03-AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertRedacted("token ghs_abcdefghijklmnopqrstuvwx12", "abcdefghijklmnopqrstuvwx12")
        # Built at run time: the privacy audit greps for the literal prefix. The value is fake.
        fake_pat = "github" + "_pat_11ABCDEFG0123456789_abcdefgh"
        self.assertRedacted(fake_pat, "11ABCDEFG0123456789_abcdefgh")
        self.assertRedacted("tk_live_9f8e7d6c5b4a", "9f8e7d6c5b4a")

    def test_bearer_token(self):
        self.assertRedacted("Authorization: Bearer abc.def-ghi_jkl123456", "abc.def-ghi_jkl123456")

    def test_userinfo_url_keeps_host_drops_credentials(self):
        out, n = vlogs.redact("clone https://deploy:hunter2pw@git.example.com/repo.git now")
        self.assertNotIn("hunter2pw", out)
        self.assertNotIn("deploy:", out)
        self.assertIn("git.example.com/repo.git", out)
        self.assertEqual(n, 1)

    def test_key_and_token_assignments(self):
        out, n = vlogs.redact('export FOO_KEY=s3cr3tvalue BAR_TOKEN="tok123"; OTHER=ok')
        self.assertNotIn("s3cr3tvalue", out)
        self.assertNotIn("tok123", out)
        self.assertIn("OTHER=ok", out)
        self.assertIn("FOO_KEY=REDACTED", out)
        self.assertEqual(n, 2)

    def test_ordinary_words_are_not_redacted(self):
        text = "risk-management-strategy-2026 task-manager-ops disk-usage-report mask-sk-like-words"
        out, n = vlogs.redact(text)
        self.assertEqual(out, text)
        self.assertEqual(n, 0)


class MakeRowTest(unittest.TestCase):
    def test_row_fields_and_valid_json(self):
        line, truncated, red = vlogs.make_row(
            time_iso="2026-11-01T00:00:00.000Z",
            msg='{"type":"message_end","k":"sk-abcdefghijklmnopqrstuvwxyz"}',
            ws="lor/pi",
            label="hub-x",
            type_="message_end",
            kind="stream",
            path="lor-hub-state/pi/stream-x.ndjson",
        )
        self.assertTrue(line.endswith(b"\n"))
        row = json.loads(line)
        self.assertEqual(set(row), {"_time", "_msg", "ws", "label", "type", "kind", "path"})
        self.assertEqual(row["ws"], "lor/pi")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", row["_msg"])
        self.assertFalse(truncated)
        self.assertEqual(red, 1)

    def test_oversize_row_is_truncated_counted_and_still_valid(self):
        big = "é" * (vlogs.MAX_MSG_BYTES // 2 + 1000)  # multi-byte, so the cut must land on a character
        line, truncated, _ = vlogs.make_row(
            time_iso="2026-11-01T00:00:00.000Z", msg=big, ws="w", label="l", type_="t", kind="stream", path="p"
        )
        self.assertTrue(truncated)
        row = json.loads(line)
        self.assertLessEqual(len(row["_msg"].encode("utf-8")), vlogs.MAX_MSG_BYTES)
        self.assertTrue(row["_msg"].startswith("é"))


class _Fake(http.server.BaseHTTPRequestHandler):
    server_version = "fake-vl"

    def log_message(self, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self.server.calls.append(("GET", self.path, self.headers.get("Content-Type"), b""))
        self._ok(b"OK")

    def do_POST(self):
        body = self._body()
        self.server.calls.append(("POST", self.path, self.headers.get("Content-Type"), body))
        if self.path.startswith("/select/logsql/query"):
            query = parse_qs(body.decode())["query"][0]
            count = self.server.counts.get(query.split('path:="')[1].split('"')[0], 0) if 'path:="' in query else 0
            if "unpack_json" in query:
                self._ok(json.dumps({"n": "0", "i": "NaN", "o": "NaN", "c": "NaN"}).encode() + b"\n")
            else:
                self._ok(json.dumps({"n": str(count)}).encode() + b"\n")
            return
        self._ok(b"")

    def _ok(self, data):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        self.server.calls = []
        self.server.counts = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def test_refuses_non_loopback(self):
        with self.assertRaises(vlogs.VLogsError):
            vlogs.VLogsClient("http://10.0.0.5:19428")
        with self.assertRaises(vlogs.VLogsError):
            vlogs.VLogsClient("https://127.0.0.1:19428")
        vlogs.VLogsClient("http://localhost:19428")

    def test_insert_uses_stream_content_type_and_stream_fields(self):
        client = vlogs.VLogsClient(self.url)
        rows = [b'{"_msg":"a"}\n', b'{"_msg":"b"}\n']
        self.assertEqual(client.insert(rows), 2)
        method, path, ctype, body = self.server.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(path.startswith("/insert/jsonline?"))
        qs = parse_qs(urlsplit(path).query)
        self.assertEqual(qs["_stream_fields"], ["ws,label"])
        self.assertEqual(qs["_msg_field"], ["_msg"])
        self.assertEqual(qs["_time_field"], ["_time"])
        self.assertEqual(ctype, "application/stream+json")
        self.assertEqual(body, b"".join(rows))

    def test_batches_split_at_batch_size(self):
        client = vlogs.VLogsClient(self.url)
        rows = [b"x" * 100 + b"\n" for _ in range(10)]
        with mock.patch.object(vlogs, "BATCH_BYTES", 250):
            sent = client.insert(iter(rows))
        self.assertEqual(sent, 10)
        posts = [c for c in self.server.calls if c[0] == "POST"]
        # 101-byte rows: a batch closes once it reaches 250 bytes, so 3 + 3 + 3 + 1
        self.assertEqual(len(posts), 4)

    def test_flush_is_a_post(self):
        client = vlogs.VLogsClient(self.url)
        client.flush()
        self.assertEqual(self.server.calls[-1][:2], ("POST", "/internal/force_flush"))

    def test_count_quotes_the_path_and_returns_int(self):
        self.server.counts["lor-hub-state/pi/x.ndjson"] = 42
        client = vlogs.VLogsClient(self.url)
        self.assertEqual(client.count("lor-hub-state/pi/x.ndjson"), 42)
        self.assertEqual(client.count("nope"), 0)
        with self.assertRaises(vlogs.VLogsError):
            client.count('bad"path')


@unittest.skipUnless(os.environ.get("VLOGS_LIVE_TEST") == "1", "set VLOGS_LIVE_TEST=1 to query a running store")
class LiveReadTest(unittest.TestCase):
    def test_store_answers_health_and_count(self):
        client = vlogs.VLogsClient()
        self.assertTrue(client.healthy())
        self.assertGreaterEqual(client.count("no-such-path-for-test"), 0)


if __name__ == "__main__":
    unittest.main()
