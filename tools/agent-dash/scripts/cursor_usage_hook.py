#!/usr/bin/python3
"""Append one cursor-agent stop-hook usage line. Always exits 0.

The payload arrives on stdin (or as a file path in argv). Only the chat id,
model, workspace, and token counts are kept. Prompt text and account fields
are dropped.
"""

import json
import os
import sys
import time

DEST = os.path.join(os.path.expanduser("~/.local/share/agent-dash"), "cursor-usage.jsonl")
MAX_BYTES = 1024 * 1024
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def main():
    try:
        raw = _read_raw()
        if not raw or not raw.strip():
            return
        obj = json.loads(raw.decode("utf-8"))
        row = build_row(obj)
        if row is None:
            return
        append_row(row, _dest())
    except Exception:
        return


def _dest():
    override = os.environ.get("AGENT_DASH_CURSOR_USAGE")
    if override:
        return os.path.expanduser(override)
    state = os.environ.get("AGENT_DASH_STATE_DIR")
    if state:
        return os.path.join(os.path.expanduser(state), "cursor-usage.jsonl")
    return DEST


def _read_raw():
    if len(sys.argv) > 1 and sys.argv[1] and os.path.isfile(sys.argv[1]):
        try:
            if os.path.getsize(sys.argv[1]) > MAX_BYTES:
                return b""
            with open(sys.argv[1], "rb") as fh:
                return fh.read(MAX_BYTES)
        except OSError:
            return b""
    try:
        return sys.stdin.buffer.read(MAX_BYTES)
    except Exception:
        return b""


def build_row(obj):
    if not isinstance(obj, dict):
        return None
    event = obj.get("hook_event_name")
    if event not in (None, "stop"):
        return None
    chat_id = _uuid(obj.get("conversation_id")) or _uuid(obj.get("session_id"))
    if not chat_id:
        return None
    counted = {}
    any_num = False
    for key in TOKEN_KEYS:
        if key in obj:
            counted[key] = _nat(obj.get(key))
        else:
            counted[key] = None
        if counted[key] is not None:
            any_num = True
    if not any_num:
        return None
    return {
        "ts": round(time.time(), 3),
        "chat_id": chat_id,
        "model": _text(obj.get("model"), 200),
        "cwd": _cwd(obj),
        "input_tokens": counted["input_tokens"],
        "output_tokens": counted["output_tokens"],
        "cache_read_tokens": counted["cache_read_tokens"],
        "cache_write_tokens": counted["cache_write_tokens"],
        "reasoning_tokens": counted["reasoning_tokens"],
    }


def append_row(row, path):
    line = json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _cwd(obj):
    roots = obj.get("workspace_roots")
    if isinstance(roots, list) and roots:
        text = _path(roots[0])
        if text:
            return text
    text = _path(obj.get("cwd"))
    if text:
        return text
    return _path(os.environ.get("CURSOR_PROJECT_DIR"))


def _path(value):
    text = _text(value, 1024)
    if text and text.startswith("/"):
        return text
    return None


def _uuid(value):
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return None
    hexdigits = "0123456789abcdefABCDEF"
    if not all(ch in hexdigits for part in parts for ch in part):
        return None
    return value


def _nat(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _text(value, limit):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or "\n" in text or "\r" in text:
        return None
    return text


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
