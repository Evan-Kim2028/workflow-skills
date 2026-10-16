#!/usr/bin/env python3
"""Keep the final assistant segment from cursor-agent stream-json.

stdin is ``--output-format stream-json`` without ``--stream-partial-output``.
Each ``assistant`` event is one finished segment. A ``tool_call`` / ``started``
event clears it, matching text mode, which prints only the text after the
last tool call. Thinking, tool payloads, and the user prompt stay off stdout.

The result ``usage`` object is appended as one JSON line. ``inputTokens`` is
already uncached input: the CLI subtracts cache read and cache write before
printing. ``reasoning_tokens`` is null because that printer drops proto field
``reasoning_tokens``.
"""

import argparse
import json
import os
import sys
import time

DEST = os.path.join(os.path.expanduser("~/.local/share/agent-dash"), "cursor-usage.jsonl")
TOKEN_MAP = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_tokens", "cacheReadTokens"),
    ("cache_write_tokens", "cacheWriteTokens"),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--usage-file", default=None)
    args = parser.parse_args()
    segment, usage, session_id = _consume(sys.stdin)
    sys.stdout.write(segment + "\n")
    sys.stdout.flush()
    row = build_row(usage, session_id, args.label, args.cwd, args.model)
    if row is None:
        return
    append_row(row, _dest(args.usage_file))


def _consume(stream):
    segment = ""
    usage = None
    session_id = None
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        sid = _uuid(obj.get("session_id"))
        if sid:
            session_id = sid
        kind = obj.get("type")
        if kind == "assistant":
            segment = _assistant_text(obj.get("message"))
        elif kind == "tool_call" and obj.get("subtype") == "started":
            segment = ""
        elif kind == "result" and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    return segment, usage, session_id


def build_row(usage, session_id, label, cwd, model):
    if not isinstance(usage, dict) or not session_id:
        return None
    label = _text(label, 200)
    cwd = _path(cwd)
    model = _text(model, 200)
    if not label or not cwd or not model:
        return None
    counted = {}
    any_num = False
    for dest, src in TOKEN_MAP:
        counted[dest] = _nat(usage.get(src))
        if counted[dest] is not None:
            any_num = True
    if not any_num:
        return None
    return {
        "ts": round(time.time(), 3),
        "chat_id": session_id,
        "model": model,
        "cwd": cwd,
        "input_tokens": counted["input_tokens"],
        "output_tokens": counted["output_tokens"],
        "cache_read_tokens": counted["cache_read_tokens"],
        "cache_write_tokens": counted["cache_write_tokens"],
        "reasoning_tokens": None,
        "label": label,
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


def _dest(explicit):
    if explicit:
        return explicit
    override = os.environ.get("AGENT_DASH_CURSOR_USAGE")
    if override:
        return override
    return DEST


def _assistant_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


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


def _path(value):
    text = _text(value, 1024)
    if text and text.startswith("/"):
        return text
    return None


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
