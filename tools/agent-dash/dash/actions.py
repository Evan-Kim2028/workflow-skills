"""The one write action the dashboard performs: archive/restore from the page.

Every other dashboard surface is read-only: the page copies CLI commands
and the server answers GET. These two POST endpoints
(``/api/workstreams/archive`` and ``/api/workstreams/restore``) are the
only writers, and they write only bundle lifecycle fields (``state``,
``state_changed_at``, ``archived_at``, the frozen ``summary.json``):
never sources, never plan content, never approvals. They run the SAME
``dash.lifecycle`` functions the CLI uses (``archive_bundle``,
``restore_bundle``: per-bundle file lock, atomic temp-file-plus-replace
writes, ``{kind: state}`` history line), so the page and the CLI cannot
drift apart.

Guards, all fail-closed:

- read-only switch: ``AGENT_DASH_READONLY=1`` in the environment or
  ``readonly: true`` in the server config answers 403 ``read_only``.
- local-only: ``Host`` must be ``127.0.0.1:<port>`` or
  ``localhost:<port>`` for the serving port.
- cross-site posts are refused: a present ``Origin`` must match the
  local origin, and every call must carry ``X-AgentDash-Action: 1``,
  which cross-site forms and simple fetches cannot set.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
from urllib.parse import urlparse

from dash import errors

LOG = logging.getLogger("agent_dash.actions")

#: Custom header every archive/restore POST must carry. Cross-site form
#: posts and no-cors fetches cannot set a custom header, so its presence
#: proves the call came from script on the dashboard origin (or curl).
ACTION_HEADER = "X-AgentDash-Action"
ACTION_HEADER_VALUE = "1"

#: Workstream id shape, the same rule as ``dash.server._WS``.
_WS = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

#: Cap on an action body: a workstream id is at most 64 chars.
_MAX_BODY = 8192

#: Bundle kinds (and the ids) the page may never archive.
_PROGRAM_KINDS = ("program",)
_NEVER_ARCHIVE_IDS = ("OVERVIEW",)


def actions_enabled(cfg=None) -> bool:
    """True when the page may offer real archive/restore buttons.

    Disabled with 403 ``read_only`` when ``AGENT_DASH_READONLY=1`` is set
    or the server config carries ``readonly: true``; the page then falls
    back to copy-command buttons. ``GET /api/project`` and
    ``GET /api/state`` expose this as ``{"actions": {"archive": bool}}``.
    """
    if (os.environ.get("AGENT_DASH_READONLY") or "").strip() == "1":
        return False
    if isinstance(cfg, dict) and cfg.get("readonly") in (True, 1, "1", "true", "yes"):
        return False
    return True


def _local_hosts(port: int) -> tuple[str, str]:
    return (f"127.0.0.1:{port}", f"localhost:{port}")


def guard(handler, cfg=None) -> tuple[int, dict] | None:
    """Fail-closed request guards. None when the call may proceed, else
    the ``(status, body)`` refusal to serve."""
    if not actions_enabled(cfg):
        return (
            403,
            {
                **api_error_compat(
                    "archive actions are disabled: the dashboard is read-only",
                    "read_only",
                    "Unset AGENT_DASH_READONLY (or config readonly) and restart, "
                    "or archive from the CLI: python3 agent_dash.py ws archive <ID>",
                ),
            },
        )
    try:
        port = int(handler.server.server_address[1])
    except (AttributeError, TypeError, ValueError):
        port = 0
    host = (handler.headers.get("Host") or "").strip()
    if host.lower() not in _local_hosts(port):
        return (
            403,
            {
                **api_error_compat(
                    f"refused: Host {host!r} is not this dashboard",
                    "forbidden",
                    f"Archive from the dashboard page on 127.0.0.1:{port}; "
                    "cross-site posts are refused.",
                ),
            },
        )
    origin = (handler.headers.get("Origin") or "").strip()
    if origin:
        try:
            parts = urlparse(origin)
            ok = (
                parts.scheme == "http"
                and (parts.hostname or "").lower() in ("127.0.0.1", "localhost")
                and parts.port == port
            )
        except ValueError:
            ok = False
        if not ok:
            return (
                403,
                {
                    **api_error_compat(
                        f"refused: cross-site Origin {origin!r}",
                        "forbidden",
                        f"Archive from the dashboard page on 127.0.0.1:{port}; "
                        "cross-site posts are refused.",
                    ),
                },
            )
    if (handler.headers.get(ACTION_HEADER) or "").strip() != ACTION_HEADER_VALUE:
        return (
            403,
            {
                **api_error_compat(
                    f"refused: missing {ACTION_HEADER} header",
                    "forbidden",
                    "Archive from the dashboard page (its script sends the "
                    "header); cross-site form posts and simple fetches are refused.",
                ),
            },
        )
    return None


def api_error_compat(error: str, code: str, hint: str) -> dict:
    """The house error shape (mirrors ``dash.errors.api_error``)."""
    return errors.api_error(error, code, hint)


def read_ws_body(handler) -> str:
    """The ``{"ws": "<ID>"}`` body. Raises ValueError naming the problem.

    The body carries the workstream id and nothing else: extra fields,
    a missing id, a bad id shape, or unparseable JSON are all a 400.
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise ValueError("bad body")
    if length <= 0 or length > _MAX_BODY:
        raise ValueError("bad body")
    try:
        raw = handler.rfile.read(length)
    except (OSError, ValueError):
        raise ValueError("bad body")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("bad body")
    if not isinstance(body, dict):
        raise ValueError("bad body")
    ws = body.get("ws")
    if not isinstance(ws, str) or not ws:
        raise ValueError("ws required")
    if set(body) != {"ws"}:
        raise ValueError("bad body")
    if not _WS.match(ws):
        raise ValueError("bad ws")
    return ws


def _bundle_meta(ws: str):
    """``(path, meta)`` for ``ws`` under the bundle root, or ``(None, None)``."""
    from dash.bundles import _read_json, bundle_dir

    path = bundle_dir(ws)
    if path is None:
        return None, None
    meta = _read_json(path / "bundle.json")
    if not isinstance(meta, dict):
        return path, None
    return path, meta


def perform(server, action: str, ws: str) -> tuple[int, dict]:
    """Run one archive/restore through the CLI's lifecycle functions.

    Returns the ``(status, body)`` to serve: ``{ok, ws, state,
    archived_at}`` on success, or a house-shape error (unknown ws 404,
    not eligible 409). Archiving an archived workstream (and restoring
    anything but one) is a 409, never a crash; two clicks cannot
    interleave because the lifecycle functions hold the per-bundle file
    lock across each read-modify-write cycle.
    """
    from dash import lifecycle

    _path, meta = _bundle_meta(ws)
    if meta is None:
        return (
            404,
            {**errors.api_error(errors.bundle_not_found(ws), "not_found", "Run: python3 agent_dash.py ws list"), "ws": ws},
        )
    state = lifecycle.current_state(meta)
    if action == "archive":
        if ws in _NEVER_ARCHIVE_IDS or meta.get("kind") in _PROGRAM_KINDS:
            return (
                409,
                {
                    **errors.api_error(
                        f"{ws} is a program bundle and never archives",
                        "not_eligible",
                        "Archive a workstream bundle instead; list them with: python3 agent_dash.py ws list",
                    ),
                    "ws": ws,
                },
            )
        if state == "archived":
            return (
                409,
                {
                    **errors.api_error(
                        f"{ws} is already archived",
                        "not_eligible",
                        f"Run: python3 agent_dash.py ws restore {ws} to reopen it",
                    ),
                    "ws": ws,
                },
            )
        if state == "dropped":
            return (
                409,
                {
                    **errors.api_error(
                        f"{ws} is dropped and cannot be archived",
                        "not_eligible",
                        "Run: python3 agent_dash.py ws list --state all",
                    ),
                    "ws": ws,
                },
            )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = lifecycle.archive_bundle(ws)
        return _lifecycle_result(server, ws, action, code, buf.getvalue())
    if state != "archived":
        return (
            409,
            {
                **errors.api_error(
                    f"only archived workstreams restore ({ws} is {state})",
                    "not_eligible",
                    "Run: python3 agent_dash.py ws list --state archived",
                ),
                "ws": ws,
            },
        )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lifecycle.restore_bundle(ws)
    return _lifecycle_result(server, ws, action, code, buf.getvalue())


def _lifecycle_result(server, ws: str, action: str, code: int, output: str) -> tuple[int, dict]:
    """Translate a lifecycle exit code into the HTTP answer to serve."""
    from dash import lifecycle

    if code == errors.OK:
        _path, meta = _bundle_meta(ws)
        meta = meta if isinstance(meta, dict) else {}
        try:
            invalidate_caches(server, ws)
        except Exception:
            LOG.warning("cache invalidation failed after %s %s", action, ws, exc_info=True)
        LOG.info("%s %s", action, ws)
        return (
            200,
            {
                "ok": True,
                "ws": ws,
                "state": lifecycle.current_state(meta),
                "archived_at": meta.get("archived_at"),
            },
        )
    if code == errors.NOT_FOUND:
        return (
            404,
            {**errors.api_error(errors.bundle_not_found(ws), "not_found", "Run: python3 agent_dash.py ws list"), "ws": ws},
        )
    hint = " ".join(output.split()) or "See the CLI for the next step."
    if code == errors.INVALID_DATA:
        return (
            500,
            {**errors.api_error(f"bundle.json for {ws} is unreadable", "invalid_data", f"Run: python3 agent_dash.py ws validate {ws}"), "ws": ws},
        )
    return (
        409,
        {**errors.api_error(output.strip().splitlines()[0] if output.strip() else f"{ws} is not eligible to {action}", "not_eligible", hint), "ws": ws},
    )


def invalidate_caches(server, ws: str) -> None:
    """Invalidate every cache that depends on bundle lifecycle state.

    The store sequences bump (so the next SSE tick carries a new ``seq``
    and the snapshot cache recomputes instead of serving the stale
    payload), the server's per-plan caches drop this workstream, and the
    bundle/plan reads that already hit disk stay fresh because they are
    never cached. Reads again list the workstream in its new state on
    the very next poll.
    """
    store = getattr(server, "store", None)
    if store is not None and hasattr(store, "note_bundle_change"):
        store.note_bundle_change()
    try:
        from dash import server as server_mod

        server_mod._PLAN_EXTRA_CACHE.pop(ws, None)
        server_mod._PLAN_MAP_CACHE.pop(ws, None)
    except (AttributeError, ImportError):
        pass
