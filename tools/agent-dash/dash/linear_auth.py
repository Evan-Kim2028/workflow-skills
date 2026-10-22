"""Linear sign-in through the browser, the same flow an MCP client uses.

OAuth 2.0 with PKCE S256 against Linear's MCP authorization server. The
client registers itself on each login (RFC 7591, public client, no secret)
for the loopback redirect of that login, so no OAuth app is needed. The
token is stored in ``<state_dir>/linear-token.json`` with mode 0600 and
refreshes on its own. ``LINEAR_API_KEY`` stays the headless fallback.
Nothing here logs or prints a token.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

from dash import dataroot, errors, linear

ISSUER = "https://mcp.linear.app"
AUTHORIZE_URL = ISSUER + "/authorize"
TOKEN_URL = ISSUER + "/token"
REGISTER_URL = ISSUER + "/register"
RESOURCE = ISSUER + "/mcp"
SCOPE = "read write"
CLIENT_NAME = "agent-dash"
CALLBACK_PATH = "/callback"
TOKEN_FILE = "linear-token.json"
LOCK_FILE = ".linear-token.lock"
REFRESH_SKEW_S = 60.0
DEFAULT_EXPIRES_S = 3600.0
TIMEOUT_S = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")
_SAFE_TEXT = re.compile(r"[^A-Za-z0-9_ .:,-]")


class AuthError(Exception):
    """A sign-in step failed. The message is safe to print and never holds a token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a token never follows a redirect to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _state_dir() -> Path:
    return dataroot.state_dir()


def _token_path() -> Path:
    return _state_dir() / TOKEN_FILE


def _str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _check_url(url: str, allow_local_http: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if allow_local_http and parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return
    raise AuthError("OAuth endpoints must be https")


def _send(request: urllib.request.Request) -> bytes:
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            return response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        code = _error_code(exc)
        raise AuthError(f"HTTP {exc.code}{': ' + code if code else ''}") from None
    except (OSError, ValueError) as exc:
        raise AuthError(f"request failed ({type(exc).__name__})") from None


def _error_code(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read(65536).decode("utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("error") if isinstance(data, dict) else None
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else ""


def _post_form(url: str, fields: dict, allow_local_http: bool) -> bytes:
    _check_url(url, allow_local_http)
    body = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    return _send(request)


def _post_json(url: str, payload: dict, allow_local_http: bool) -> bytes:
    _check_url(url, allow_local_http)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    return _send(request)


def _json_object(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AuthError("response was not JSON") from None
    if not isinstance(data, dict):
        raise AuthError("response was not an object")
    return data


def _write_private(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` with mode 0600, through a temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_token() -> dict | None:
    try:
        data = json.loads(_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def _token_lock():
    """Exclusive flock on ``<state_dir>/.linear-token.lock`` around a read-refresh-write cycle."""
    path = _state_dir()
    path.mkdir(parents=True, exist_ok=True)
    with open(path / LOCK_FILE, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record(response: dict, now: float, client_id: str, prior: dict) -> dict:
    """The token record built from a token response. A response without a refresh token keeps the prior one."""
    access = response.get("access_token")
    if not _str(access):
        raise AuthError("token response had no access token")
    expires_in = response.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        seconds = float(expires_in)
    else:
        seconds = DEFAULT_EXPIRES_S
    refresh = response.get("refresh_token")
    if not _str(refresh):
        refresh = prior.get("refresh_token")
    scope = response.get("scope")
    token_type = response.get("token_type")
    return {
        "access_token": access,
        "refresh_token": refresh if _str(refresh) else None,
        "expires_at": now + seconds,
        "token_type": token_type if _str(token_type) else "Bearer",
        "client_id": client_id,
        "token_endpoint": TOKEN_URL,
        "issuer": ISSUER,
        "resource": RESOURCE,
        "scope": scope if _str(scope) else SCOPE,
    }


def _near_expiry(record: dict, now: float) -> bool:
    expires = record.get("expires_at")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return False
    return float(expires) - now <= REFRESH_SKEW_S


def _refresh(current: dict, now: float, allow_local_http: bool) -> str | None:
    """Exchange the refresh token for a new pair and save it. Returns the new access token."""
    response = _json_object(
        _post_form(
            str(current.get("token_endpoint") or TOKEN_URL),
            {
                "grant_type": "refresh_token",
                "client_id": current.get("client_id"),
                "refresh_token": current.get("refresh_token"),
                "resource": current.get("resource") or RESOURCE,
            },
            allow_local_http,
        )
    )
    record = _record(response, now, str(current.get("client_id") or ""), current)
    record["token_endpoint"] = current.get("token_endpoint") or TOKEN_URL
    _write_private(_token_path(), record)
    return record["access_token"]


def _access_token(now: float, allow_local_http: bool) -> str | None:
    record = _read_token()
    if record is None or not _str(record.get("access_token")):
        return None
    if not _near_expiry(record, now):
        return record["access_token"]
    if not _str(record.get("refresh_token")) or not _str(record.get("client_id")):
        return None
    with _token_lock():
        current = _read_token() or {}
        if not _str(current.get("access_token")):
            return None
        if not _near_expiry(current, now):
            return current["access_token"]
        if not _str(current.get("refresh_token")) or not _str(current.get("client_id")):
            return None
        return _refresh(current, now, allow_local_http)


def access_token(now: float | None = None, allow_local_http: bool = False) -> str | None:
    """The OAuth access token, refreshed first when it expires within 60 s.

    Returns None when there is no token, the token cannot be refreshed, or
    anything else fails. Never raises and never logs the token.
    """
    try:
        return _access_token(time.time() if now is None else now, allow_local_http)
    except Exception:
        return None


def credential(allow_local_http: bool = False) -> tuple[str, str] | None:
    """The credential the sync uses: ``("mcp", token)`` from OAuth, else ``("key", key)`` from ``LINEAR_API_KEY``, else None."""
    token = access_token(allow_local_http=allow_local_http)
    if token is not None:
        return ("mcp", token)
    key = linear.api_key()
    if key is not None:
        return ("key", key)
    return None


def status() -> dict:
    """``{'method': 'oauth' | 'key' | None, 'issuer', 'expires_at'}``. Never contains a token."""
    record = _read_token()
    if record is not None and _str(record.get("access_token")):
        return {"method": "oauth", "issuer": record.get("issuer"), "expires_at": record.get("expires_at")}
    if linear.api_key() is not None:
        return {"method": "key", "issuer": None, "expires_at": None}
    return {"method": None, "issuer": None, "expires_at": None}


class _Callback(http.server.HTTPServer):
    """One-shot loopback listener. The first GET on the callback path is the result."""

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.result: dict | None = None
        self.arrived = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        body = b"<!doctype html><title>agent-dash</title><p>Linear sign-in finished. You can close this tab.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if self.server.result is None:
            self.server.result = query
            self.server.arrived.set()

    def log_message(self, format: str, *args: object) -> None:
        return


def _safe(text: object) -> str:
    return _SAFE_TEXT.sub("", str(text))[:120]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _register(redirect: str, allow_local_http: bool) -> str:
    """Register this install as a public client for ``redirect`` (RFC 7591). Returns the client id."""
    data = _json_object(
        _post_json(
            REGISTER_URL,
            {
                "client_name": CLIENT_NAME,
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": SCOPE,
            },
            allow_local_http,
        )
    )
    client_id = data.get("client_id")
    if not _str(client_id):
        raise AuthError("registration returned no client id")
    return client_id


def login(
    open_browser: bool = True,
    timeout_s: float = 180.0,
    opener: Callable[[str], object] | None = None,
    *,
    allow_local_http: bool = False,
    verify: Callable[[str], str] | None = None,
) -> int:
    """Sign in through the browser with PKCE and store the token. Returns the CLI exit code.

    The loopback callback takes a free port (port 0), and the client registers
    the exact redirect for that port on every login, so nothing has to match
    a fixed port. ``verify`` maps the new access token to a display name; when
    it fails, the token is not kept. ``allow_local_http`` is for tests only.
    """
    try:
        server = _Callback(0)
    except OSError:
        print("could not open a local port for the sign-in callback", flush=True)
        return errors.CHECK_FAILED
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        return _run_login(server, timeout_s, opener or webbrowser.open, open_browser, allow_local_http, verify)
    except AuthError as exc:
        print(f"Linear sign-in failed: {_safe(exc)}", flush=True)
        return errors.CHECK_FAILED
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _run_login(server, timeout_s, opener, open_browser, allow_local_http, verify) -> int:
    for url in (AUTHORIZE_URL, TOKEN_URL, REGISTER_URL):
        _check_url(url, allow_local_http)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    client_id = _register(redirect, allow_local_http)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }
    )
    opened = bool(opener(url)) if open_browser else False
    if not opened:
        print(f"Open this link to sign in: {url}", flush=True)
    if not server.arrived.wait(timeout_s):
        raise AuthError("sign-in timed out")
    params = server.result or {}
    if not secrets.compare_digest(str(params.get("state", "")), state):
        raise AuthError("state mismatch; sign-in refused")
    if "error" in params:
        raise AuthError(f"Linear refused sign-in: {_safe(params['error'])}")
    code = params.get("code")
    if not _str(code):
        raise AuthError("no authorization code returned")
    response = _json_object(
        _post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": RESOURCE,
            },
            allow_local_http,
        )
    )
    record = _record(response, time.time(), client_id, {})
    _write_private(_token_path(), record)
    name = None
    if verify is not None:
        try:
            name = verify(record["access_token"])
        except Exception as exc:
            with contextlib.suppress(OSError):
                _token_path().unlink()
            raise AuthError(f"token check failed ({type(exc).__name__})") from None
    print(f"Signed in to Linear as {_safe(name)}" if _str(name) else "Signed in to Linear.", flush=True)
    return errors.OK


def logout() -> int:
    """Delete the token file. The server-side grant stays until it expires. Returns the CLI exit code."""
    with contextlib.suppress(OSError):
        _token_path().unlink()
    print("Signed out of Linear.", flush=True)
    return errors.OK
