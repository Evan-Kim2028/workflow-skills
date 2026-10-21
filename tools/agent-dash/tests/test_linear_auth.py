import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import errors, linear_auth as la

CLEAN_ENV = {"LINEAR_API_KEY": ""}


class FakeAuthServer(ThreadingHTTPServer):
    """Local stand-in for the MCP authorization server: register, token and refresh."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.server_address[1]}"
        self.challenge: str | None = None
        self.registrations: list[dict] = []
        self.token_forms: list[dict] = []
        self.refresh_fails = False
        self.counter = 0
        self.last_issued: str | None = None

    def issue(self) -> dict:
        self.counter += 1
        self.last_issued = f"tok-{self.counter}"
        return {
            "access_token": self.last_issued,
            "refresh_token": f"ref-{self.counter}",
            "expires_in": 86100,
            "token_type": "bearer",
            "scope": "read write",
        }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        fake: FakeAuthServer = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/register":
            payload = json.loads(body.decode("utf-8"))
            fake.registrations.append(payload)
            self._send(201, {"client_id": f"reg-{len(fake.registrations)}", "token_endpoint_auth_method": "none"})
            return
        if self.path == "/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()}
            fake.token_forms.append(form)
            if form.get("grant_type") == "refresh_token":
                if fake.refresh_fails:
                    self._send(400, {"error": "invalid_grant"})
                    return
                self._send(200, fake.issue())
                return
            digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            if form.get("code") != "auth-code" or digest != fake.challenge or form.get("client_id") != f"reg-{len(fake.registrations)}":
                self._send(400, {"error": "invalid_grant"})
                return
            self._send(200, fake.issue())
            return
        self._send(404, {"error": "not_found"})


def _browser(fake: FakeAuthServer, state: str | None = None):
    """A fake browser: reads the authorize URL, then GETs the loopback callback with code and state."""

    def open_url(url: str) -> bool:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        fake.authorize_query = query
        fake.challenge = query["code_challenge"]
        with urllib.request.urlopen(f"{query['redirect_uri']}?code=auth-code&state={state or query['state']}", timeout=5) as reply:
            reply.read()
        return True

    return open_url


class LinearAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeAuthServer()
        self.thread = threading.Thread(target=self.fake.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, dict(CLEAN_ENV, AGENT_DASH_STATE_DIR=str(self.state)))
        self.env.start()
        base = self.fake.base
        self.patches = [
            mock.patch.object(la, "AUTHORIZE_URL", base + "/authorize"),
            mock.patch.object(la, "TOKEN_URL", base + "/token"),
            mock.patch.object(la, "REGISTER_URL", base + "/register"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.env.stop()
        self.fake.shutdown()
        self.fake.server_close()
        self.tmp.cleanup()

    def _login(self, verify=None, **kwargs) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = la.login(
                timeout_s=5,
                opener=kwargs.pop("opener", _browser(self.fake)),
                allow_local_http=True,
                verify=verify,
                **kwargs,
            )
        return rc, out.getvalue() + err.getvalue()

    def _token_path(self) -> Path:
        return self.state / la.TOKEN_FILE

    def _write_token(self, expires_at: float, refresh: str | None = "ref-old", client_id: str | None = "reg-1") -> None:
        record = {
            "access_token": "tok-old",
            "refresh_token": refresh,
            "expires_at": expires_at,
            "token_type": "bearer",
            "client_id": client_id,
            "token_endpoint": self.fake.base + "/token",
            "issuer": la.ISSUER,
            "resource": la.RESOURCE,
            "scope": "read write",
        }
        la._write_private(self._token_path(), record)

    def test_login_registers_runs_pkce_and_stores_private_token(self) -> None:
        rc, output = self._login(verify=lambda token: "Test User")
        self.assertEqual(rc, 0, output)
        self.assertIn("Signed in to Linear as Test User", output)
        registration = self.fake.registrations[-1]
        self.assertEqual(registration["token_endpoint_auth_method"], "none")
        self.assertEqual(len(registration["redirect_uris"]), 1)
        self.assertRegex(registration["redirect_uris"][0], r"^http://127\.0\.0\.1:\d+/callback$")
        exchange = self.fake.token_forms[-1]
        self.assertEqual(exchange["grant_type"], "authorization_code")
        self.assertEqual(exchange["client_id"], "reg-1")
        self.assertEqual(exchange["redirect_uri"], registration["redirect_uris"][0])
        self.assertEqual(exchange["resource"], la.RESOURCE)
        self.assertEqual(self.fake.authorize_query["resource"], la.RESOURCE)
        self.assertEqual(self.fake.authorize_query["code_challenge_method"], "S256")
        self.assertEqual(self.fake.authorize_query["scope"], "read write")
        self.assertEqual((self._token_path().stat().st_mode & 0o777), 0o600)
        stored = json.loads(self._token_path().read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], self.fake.last_issued)
        self.assertEqual(stored["client_id"], "reg-1")
        self.assertEqual(stored["refresh_token"], "ref-1")
        self.assertEqual(stored["resource"], la.RESOURCE)
        self.assertEqual(stored["token_endpoint"], self.fake.base + "/token")
        self.assertGreater(stored["expires_at"], time.time() + 80000)

    def test_no_token_in_captured_output(self) -> None:
        rc, output = self._login(verify=lambda token: "Test User")
        self.assertEqual(rc, 0)
        self.assertNotIn(self.fake.last_issued, output)
        self.assertNotIn("ref-", output)

    def test_state_mismatch_is_refused(self) -> None:
        rc, output = self._login(opener=_browser(self.fake, state="forged"))
        self.assertEqual(rc, errors.CHECK_FAILED)
        self.assertIn("state mismatch", output)
        self.assertFalse(self._token_path().exists())
        self.assertEqual(self.fake.token_forms, [])

    def test_failed_verify_removes_the_token(self) -> None:
        def reject(token: str) -> str:
            raise RuntimeError("rejected")

        rc, output = self._login(verify=reject)
        self.assertEqual(rc, errors.CHECK_FAILED)
        self.assertIn("token check failed", output)
        self.assertFalse(self._token_path().exists())

    def test_login_without_verify_prints_plain_success(self) -> None:
        rc, output = self._login()
        self.assertEqual(rc, 0)
        self.assertIn("Signed in to Linear.", output)

    def test_refresh_near_expiry_saves_new_pair(self) -> None:
        now = time.time()
        self._write_token(expires_at=now + 10)
        token = la.access_token(now=now, allow_local_http=True)
        self.assertIsNotNone(token)
        self.assertNotEqual(token, "tok-old")
        refresh = self.fake.token_forms[-1]
        self.assertEqual(refresh["grant_type"], "refresh_token")
        self.assertEqual(refresh["refresh_token"], "ref-old")
        self.assertEqual(refresh["client_id"], "reg-1")
        self.assertEqual(refresh["resource"], la.RESOURCE)
        stored = json.loads(self._token_path().read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], token)
        self.assertEqual((self._token_path().stat().st_mode & 0o777), 0o600)

    def test_fresh_token_is_returned_without_network(self) -> None:
        self._write_token(expires_at=time.time() + 3600)
        self.assertEqual(la.access_token(allow_local_http=True), "tok-old")
        self.assertEqual(self.fake.token_forms, [])

    def test_refresh_failure_returns_none(self) -> None:
        self.fake.refresh_fails = True
        self._write_token(expires_at=time.time() + 5)
        self.assertIsNone(la.access_token(allow_local_http=True))

    def test_expired_without_refresh_token_returns_none(self) -> None:
        self._write_token(expires_at=time.time() - 5, refresh=None)
        self.assertIsNone(la.access_token(allow_local_http=True))

    def test_credential_prefers_oauth_then_key(self) -> None:
        self._write_token(expires_at=time.time() + 3600)
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "lin_api_key_value"}):
            self.assertEqual(la.credential(), ("mcp", "tok-old"))
            self._token_path().unlink()
            self.assertEqual(la.credential(), ("key", "lin_api_key_value"))
        self.assertIsNone(la.credential())

    def test_status_reports_method_without_token(self) -> None:
        self._write_token(expires_at=1234.0)
        status = la.status()
        self.assertEqual(status, {"method": "oauth", "issuer": la.ISSUER, "expires_at": 1234.0})
        self.assertNotIn("tok-old", json.dumps(status))

    def test_logout_deletes_file(self) -> None:
        self._write_token(expires_at=time.time() + 3600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = la.logout()
        self.assertEqual(rc, errors.OK)
        self.assertFalse(self._token_path().exists())
        self.assertNotIn("tok-old", out.getvalue())

    def test_https_only_by_default(self) -> None:
        with self.assertRaises(la.AuthError):
            la._check_url("http://example.invalid/token", allow_local_http=False)
        with self.assertRaises(la.AuthError):
            la._check_url("http://127.0.0.1/token", allow_local_http=False)
        la._check_url("https://mcp.linear.app/token", allow_local_http=False)

    def test_production_endpoints(self) -> None:
        self.assertEqual(la.RESOURCE, "https://mcp.linear.app/mcp")
        self.assertEqual(la.ISSUER, "https://mcp.linear.app")


if __name__ == "__main__":
    unittest.main()
