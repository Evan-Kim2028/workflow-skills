"""The write actions the dashboard performs: lifecycle controls from the page.

Most dashboard surfaces are read-only: the page copies CLI commands
and the server answers GET. The POST endpoints under
``/api/workstreams/`` (``archive``, ``restore``, ``state``, ``bulk``)
are the only writers, and they write only bundle lifecycle fields
(``state``, ``state_changed_at``, ``archived_at``, the frozen
``summary.json``): never sources, never plan content, never approvals.
They run the SAME ``dash.lifecycle`` functions the CLI uses
(``archive_bundle``, ``restore_bundle``, ``set_state``: per-bundle file
lock, atomic temp-file-plus-replace writes, ``{kind: state}`` history
line), so the page and the CLI cannot drift apart.

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

#: A stalled POST body must not hold a handler thread forever.
_BODY_READ_TIMEOUT_S = 10


class BadContentType(ValueError):
    """The POST did not carry ``Content-Type: application/json`` (a 415)."""


def _readonly_value(value) -> bool:
    """True when a config ``readonly`` value means read-only.

    Strings compare case-insensitively (``"True"``, ``"YES"``, ``"on"``,
    ``"1"`` all count), so a hand-written config cannot slip through on
    capitalisation.
    """
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return False

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
    if isinstance(cfg, dict) and _readonly_value(cfg.get("readonly")):
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


def _reject_dupes(pairs):
    """``object_pairs_hook`` that refuses duplicate JSON keys with a 400."""
    body = {}
    for key, value in pairs:
        if key in body:
            raise ValueError("bad body")
        body[key] = value
    return body


def _read_body(handler, length: int) -> bytes:
    """Read exactly ``length`` body bytes with a bounded socket timeout.

    A stalled client must not hold a handler thread forever: the read
    runs under a short socket timeout (restored afterwards), and a
    timeout surfaces as ``OSError`` (``socket.timeout`` subclasses it),
    which the caller folds into ``bad body`` like any other short read.
    """
    conn = getattr(handler, "connection", None)
    if conn is None:
        return handler.rfile.read(length)
    try:
        old_timeout = conn.gettimeout()
    except (OSError, ValueError):
        return handler.rfile.read(length)
    try:
        try:
            conn.settimeout(_BODY_READ_TIMEOUT_S)
        except (OSError, ValueError):
            pass
        return handler.rfile.read(length)
    finally:
        try:
            conn.settimeout(old_timeout)
        except (OSError, ValueError):
            pass


def read_ws_body(handler) -> str:
    """The ``{"ws": "<ID>"}`` body. Raises ValueError naming the problem.

    The body carries the workstream id and nothing else: extra fields,
    a missing id, a bad id shape, duplicate JSON keys, or unparseable
    JSON are all a 400. A missing or non-JSON ``Content-Type`` raises
    ``BadContentType`` (served as a 415, not a 400).
    """
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise BadContentType("bad content type")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise ValueError("bad body")
    if length <= 0 or length > _MAX_BODY:
        raise ValueError("bad body")
    try:
        raw = _read_body(handler, length)
    except (OSError, ValueError):
        raise ValueError("bad body")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dupes)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("bad body")
    if not isinstance(body, dict):
        raise ValueError("bad body")
    ws = body.get("ws")
    if not isinstance(ws, str) or not ws:
        raise ValueError("ws required")
    if set(body) != {"ws"}:
        raise ValueError("bad body")
    if ws in (".", "..") or not _WS.match(ws):
        raise ValueError("bad ws")
    return ws


def _bundle_meta(ws: str):
    """``(path, meta)`` for ``ws`` under the bundle root, or ``(None, None)``.

    The resolved bundle must name the requested id exactly: both the
    directory name and the ``bundle.json`` ``id`` must equal ``ws``,
    else the request is an alias miss (a case-insensitive-filesystem
    twin like ``ws1`` for ``WS1``, or a symlink pointing at another
    bundle) and reads as not found. Archiving through an alias would
    write the wrong bundle's lifecycle fields.
    """
    from dash.bundles import _read_json, bundle_dir

    path = bundle_dir(ws)
    if path is None or path.name != ws:
        return None, None
    meta = _read_json(path / "bundle.json")
    if not isinstance(meta, dict) or meta.get("id") != ws:
        return None, None
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
        if "plan.json" in output:
            return (
                500,
                {**errors.api_error(errors.plan_unreadable(ws), "invalid_data", f"Run: python3 agent_dash.py ws validate {ws}"), "ws": ws},
            )
        return (
            500,
            {**errors.api_error(f"bundle.json for {ws} is unreadable", "invalid_data", f"Run: python3 agent_dash.py ws validate {ws}"), "ws": ws},
        )
    if "changed while preparing" in output:
        return (
            409,
            {
                **errors.api_error(
                    f"{ws} changed while the archive was being prepared",
                    "changed",
                    "Reload the workstream and retry the archive.",
                ),
                "ws": ws,
            },
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


#: Targets ``POST /api/workstreams/state`` accepts. ``archived`` is not
#: one: archiving freezes a summary, so it stays on the archive endpoint
#: (``ws state <done> archived`` without a summary remains a CLI-only
#: move). ``restore`` leaves ``archived``; ``dropped`` is terminal.
_STATE_TARGETS = ("planned", "active", "paused", "done", "dropped")

#: Page action names per lifecycle state, the same vocabulary
#: ``GET /api/state`` exposes as ``allowed_actions``. Program bundles
#: and OVERVIEW expose none, and neither do ids with no bundle.
_STATE_ACTIONS: dict[str, tuple[str, ...]] = {
    "planned": ("start", "drop"),
    "active": ("pause", "mark_done", "drop"),
    "paused": ("resume", "drop"),
    "done": ("archive", "reopen"),
    "archived": ("restore",),
    "dropped": (),
}

#: Page action name to the lifecycle state it moves to (``archive``
#: and ``restore`` map to their own endpoints, not ``/state``).
_ACTION_TARGETS = {
    "start": "active",
    "pause": "paused",
    "resume": "active",
    "mark_done": "done",
    "reopen": "active",
    "drop": "dropped",
}

#: Bulk operations ``POST /api/workstreams/bulk`` accepts.
_BULK_ACTIONS = ("archive", "restore", "state")

#: Cap on ids per bulk call: one confirm covers the whole batch, so the
#: batch stays reviewable.
_BULK_MAX = 50


def allowed_actions_for(ws: str, state: str, kind: str | None = "workstream") -> list:
    """Page action names the dashboard may offer for one workstream.

    Pure: no I/O, so the page payloads (``/api/state``,
    ``/api/workstreams``) and the tests share it. Program bundles and
    OVERVIEW expose none, as do unknown ids (``kind`` None) and stored
    states outside the table.
    """
    if ws in _NEVER_ARCHIVE_IDS or kind in _PROGRAM_KINDS or kind != "workstream":
        return []
    return list(_STATE_ACTIONS.get(state, ()))


def _read_json_body(handler, limit: int = _MAX_BODY):
    """Parse a JSON object body. Raises ValueError naming the problem."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise ValueError("bad body")
    if length <= 0 or length > limit:
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
    return body


def read_state_body(handler) -> tuple[str, str, str | None]:
    """The ``{"ws", "state"}`` body (plus an optional ``note``).

    ``state`` must be one of the state-transition targets; ``archived``
    is refused here (archive freezes a summary, so it stays on the
    archive endpoint). ``note`` is a non-empty string when present: it
    is what lets a reopen (``done`` -> ``active``) through, the same
    ``--force --note`` rule the CLI enforces. Anything else is a 400.
    """
    body = _read_json_body(handler)
    ws = body.get("ws")
    target = body.get("state")
    note = body.get("note")
    allowed_keys = {"ws", "state"} | ({"note"} if "note" in body else set())
    if set(body) != allowed_keys:
        raise ValueError("bad body")
    if not isinstance(ws, str) or not ws or not _WS.match(ws):
        raise ValueError("bad ws" if isinstance(ws, str) and ws else "ws required")
    if target == "archived":
        raise ValueError("archived needs the archive endpoint")
    if not isinstance(target, str) or target not in _STATE_TARGETS:
        raise ValueError("bad state")
    if "note" in body and (not isinstance(note, str) or not note.strip()):
        raise ValueError("bad note")
    return ws, target, note.strip() if isinstance(note, str) else None


def read_bulk_body(handler) -> tuple[str, str | None, list, str | None]:
    """The bulk body: ``action``, ``ws`` ids, ``state`` for state moves.

    ``{"action": "archive"|"restore"|"state", "ws": [<ids, 1..50>]}``
    plus ``state`` (a transition target) when the action is ``state``
    and an optional ``note`` for reopens. Well-formed ids that name no
    bundle are per-id 404s, not body errors; anything misshapen is a
    400.
    """
    body = _read_json_body(handler, _MAX_BODY * 8)
    action = body.get("action")
    ids = body.get("ws")
    target = body.get("state")
    note = body.get("note")
    if not isinstance(action, str) or action not in _BULK_ACTIONS:
        raise ValueError("bad action")
    allowed_keys = {"action", "ws"}
    if action == "state":
        allowed_keys |= {"state"}
    if "note" in body:
        allowed_keys |= {"note"}
    if set(body) != allowed_keys:
        raise ValueError("bad body")
    if action == "state":
        if target == "archived" or not isinstance(target, str) or target not in _STATE_TARGETS:
            raise ValueError("bad state")
    elif "state" in body:
        raise ValueError("bad body")
    if "note" in body and (not isinstance(note, str) or not note.strip()):
        raise ValueError("bad note")
    if not isinstance(ids, list) or not ids or len(ids) > _BULK_MAX:
        raise ValueError("bad ws list")
    seen: list = []
    for item in ids:
        if not isinstance(item, str) or not item or not _WS.match(item):
            raise ValueError("bad ws")
        if item not in seen:
            seen.append(item)
    return action, target, seen, note.strip() if isinstance(note, str) else None


def perform_state(server, ws: str, target: str, note: str | None = None) -> tuple[int, dict]:
    """Run one lifecycle transition through the CLI's ``set_state``.

    Same rules and writes as ``ws state``: the transition table
    decides, reopening ``done`` to ``active`` needs a note, and the
    write sets ``state_changed_at`` plus one history line under the
    per-bundle file lock. Answers ``{ok, ws, state, prev_state,
    state_changed_at}``; the previous state lets the page offer Undo
    by posting the reverse transition. Errors keep the house shape:
    unknown workstream 404, invalid transition 409 with a hint listing
    the allowed next states, bad body 400 (raised by the reader).
    Program bundles and OVERVIEW never transition (409).
    """
    from dash import lifecycle

    _path, meta = _bundle_meta(ws)
    if meta is None:
        return (
            404,
            {**errors.api_error(errors.bundle_not_found(ws), "not_found", "Run: python3 agent_dash.py ws list"), "ws": ws},
        )
    if ws in _NEVER_ARCHIVE_IDS or meta.get("kind") in _PROGRAM_KINDS:
        return (
            409,
            {
                **errors.api_error(
                    f"{ws} is a program bundle and never changes state from the page",
                    "not_eligible",
                    "Change a workstream bundle instead; list them with: python3 agent_dash.py ws list",
                ),
                "ws": ws,
            },
        )
    frm = lifecycle.current_state(meta)
    if frm == target:
        return (
            200,
            {
                "ok": True,
                "ws": ws,
                "state": frm,
                "prev_state": frm,
                "state_changed_at": meta.get("state_changed_at"),
            },
        )
    if target not in lifecycle.legal_moves(frm):
        moves = ", ".join(lifecycle.legal_moves(frm)) or "none (terminal state)"
        return (
            409,
            {
                **errors.api_error(
                    f"illegal move {ws} {frm} -> {target}. Legal moves from {frm}: {moves}",
                    "invalid_transition",
                    f"Run one of: {', '.join('python3 agent_dash.py ws state ' + ws + ' ' + move for move in lifecycle.legal_moves(frm)) or 'nothing: ' + ws + ' is terminal'}",
                ),
                "ws": ws,
                "allowed": lifecycle.legal_moves(frm),
            },
        )
    force = False
    if frm == "done" and target == "active":
        if not (isinstance(note, str) and note.strip()):
            return (
                409,
                {
                    **errors.api_error(
                        f"refusing to reopen {ws} done -> active without a note",
                        "invalid_transition",
                        f"Reopen with a reason: POST {{\"ws\": \"{ws}\", \"state\": \"active\", \"note\": \"why it reopened\"}}, "
                        f"or run: python3 agent_dash.py ws state {ws} active --force --note \"why it reopened\"",
                    ),
                    "ws": ws,
                    "allowed": lifecycle.legal_moves(frm),
                },
            )
        force = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = lifecycle.set_state(ws, target, force=force, note=note)
    if code == errors.OK:
        _path, fresh = _bundle_meta(ws)
        fresh = fresh if isinstance(fresh, dict) else {}
        try:
            invalidate_caches(server, ws)
        except Exception:
            LOG.warning("cache invalidation failed after state %s", ws, exc_info=True)
        LOG.info("state %s %s -> %s", ws, frm, target)
        return (
            200,
            {
                "ok": True,
                "ws": ws,
                "state": lifecycle.current_state(fresh),
                "prev_state": frm,
                "state_changed_at": fresh.get("state_changed_at"),
            },
        )
    if code == errors.NOT_FOUND:
        return (
            404,
            {**errors.api_error(errors.bundle_not_found(ws), "not_found", "Run: python3 agent_dash.py ws list"), "ws": ws},
        )
    output = buf.getvalue()
    hint = " ".join(output.split()) or "See the CLI for the next step."
    return (
        409,
        {**errors.api_error(output.strip().splitlines()[0] if output.strip() else f"{ws} cannot move to {target}", "invalid_transition", hint), "ws": ws},
    )


def perform_bulk(server, action: str, ids: list, target: str | None = None, note: str | None = None) -> dict:
    """Run one bulk batch: per-id results, partial success allowed.

    Every id runs alone through the same single-operation path the
    buttons use (archive/restore via :func:`perform`, transitions via
    :func:`perform_state`), each write atomic under its per-bundle
    lock; one id's failure never aborts the rest. Successful items
    carry ``prev_state`` so one page Undo restores all that changed.
    Caches invalidate once when anything changed. Always answers 200
    with ``{ok, action, results, changed}``.
    """
    results: list = []
    changed: list = []
    prev_of: dict[str, str] = {}
    for ws in ids:
        _path, meta = _bundle_meta(ws)
        if isinstance(meta, dict):
            from dash import lifecycle as _lifecycle

            prev_of[ws] = _lifecycle.current_state(meta)
    for ws in ids:
        if action == "state":
            assert target is not None
            status, body = perform_state(server, ws, target, note)
        else:
            status, body = perform(server, action, ws)
        if status == 200 and isinstance(body, dict):
            item = {
                "ws": ws,
                "ok": True,
                "state": body.get("state"),
                "prev_state": prev_of.get(ws),
            }
            if body.get("archived_at") is not None:
                item["archived_at"] = body.get("archived_at")
            results.append(item)
            if body.get("state") != prev_of.get(ws) or prev_of.get(ws) is None:
                changed.append(ws)
        else:
            item = {"ws": ws, "ok": False}
            if isinstance(body, dict):
                for key in ("error", "code", "hint", "allowed"):
                    if body.get(key) is not None:
                        item[key] = body.get(key)
            results.append(item)
    if changed:
        try:
            store = getattr(server, "store", None)
            if store is not None and hasattr(store, "note_bundle_change"):
                store.note_bundle_change()
        except Exception:
            LOG.warning("bulk cache invalidation failed", exc_info=True)
    return {
        "ok": all(item.get("ok") for item in results),
        "action": action,
        "results": results,
        "changed": changed,
    }
