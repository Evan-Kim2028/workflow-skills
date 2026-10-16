"""Cursor-agent source tests. Synthetic chats plus a read-only smoke on a local home."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dash.sources import cursor as mod
from dash.store import Store

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
PARENT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
SECRET = "SECRETTOKEN_BLOB_KEY_99"
SENTINEL = "PROMPT_BODY_SENTINEL_zz"
NOW = 2_000_000.0
def blob_id(seed):
    return (seed * 32)[:64]


def assistant(model, pad=b""):
    raw = json.dumps(
        {"role": "assistant", "providerOptions": {"cursor": {"modelName": model}}}
    ).encode()
    if not pad:
        return raw
    return raw[:-1] + b',"pad":"' + pad + b'"}'


def billed(model):
    return json.dumps(
        {
            "role": "assistant",
            "providerOptions": {"cursor": {"modelName": model}},
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 3,
                "cache_write_tokens": 2,
                "reasoning_tokens": 5,
            },
        }
    ).encode()


class Mem:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.sessions = {}
        self.usage = []

    def get_offset(self, path):
        row = self.offsets.get(path)
        if not row:
            return None
        return (row[0], row[1])

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (int(inode), int(offset), int(size), float(mtime))

    def upsert_run(self, row):
        current = dict(self.runs.get(row["run_id"]) or {})
        current.update(row)
        self.runs[row["run_id"]] = current

    def get_run(self, run_id):
        row = self.runs.get(run_id)
        return dict(row) if row else None

    def upsert_session(self, row):
        current = dict(self.sessions.get(row["session_id"]) or {})
        current.update(row)
        self.sessions[row["session_id"]] = current

    def add_usage(self, row):
        self.usage.append(dict(row))


def dumped(store):
    return json.dumps(
        {"runs": store.runs, "sessions": store.sessions, "usage": store.usage},
        default=str,
    )


def make_cfg(home, scratch, **extra):
    cfg = {
        "roots": {"cursor_home": home, "scratchpads": scratch},
        "tmp_dir": os.path.join(home, "tmp"),
        "read_budget_per_file": 4 * 1024 * 1024,
        "read_budget_per_tick": 8 * 1024 * 1024,
    }
    cfg.update(extra)
    return cfg


def write_manifest(scratch, rows, session=None):
    if session:
        pad = os.path.join(scratch, "encoded", session, "scratchpad")
    else:
        pad = scratch
    os.makedirs(pad, exist_ok=True)
    path = os.path.join(pad, "cursor-sessions.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        for label, epoch in rows:
            fh.write("%s\t-\t%s\n" % (label, epoch))
    return path


def write_db(path, meta_obj, blobs, keep_open=False):
    con = sqlite3.connect(path)
    if keep_open:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute(
        "INSERT INTO meta VALUES ('0', ?)",
        (json.dumps(meta_obj).encode().hex(),),
    )
    for ident, data in blobs:
        con.execute("INSERT INTO blobs VALUES (?, ?)", (ident, data))
    con.commit()
    if keep_open:
        return con
    con.close()
    return None


def pb_varint(n):
    out = bytearray()
    while True:
        piece = n & 0x7F
        n >>= 7
        if n:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def pb_var(field_no, n):
    return pb_varint((field_no << 3) | 0) + pb_varint(n)


def pb_bytes(field_no, payload):
    return pb_varint((field_no << 3) | 2) + pb_varint(len(payload)) + payload


def context_root(used, maximum):
    details = pb_var(1, used) + pb_var(2, maximum)
    secret = b"SECRET_PATH_DO_NOT_STORE"
    return pb_bytes(1, b"x" * 32) + pb_bytes(5, details) + pb_bytes(9, secret)


def hook_script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "cursor_usage_hook.py")
    spec = importlib.util.spec_from_file_location("cursor_usage_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def add_chat(home, uuid, cwd, created_ms, updated_ms, label, blobs, transcript=True, encoded=None, root_data=None, parent_agent=None):
    chat_dir = os.path.join(home, "chats", "bucket", uuid)
    os.makedirs(chat_dir, exist_ok=True)
    with open(os.path.join(chat_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "schemaVersion": 1,
                "cwd": cwd,
                "createdAtMs": created_ms,
                "updatedAtMs": updated_ms,
                "hasConversation": True,
            },
            fh,
        )
    root = blob_id("11")
    asst = blob_id("22")
    user = blob_id("33")
    meta_obj = {
        "agentId": uuid,
        "latestRootBlobId": root,
        "createdAt": created_ms,
        "name": "New Agent",
        "blobEncryptionKey": SECRET,
    }
    if parent_agent:
        meta_obj["parentAgentId"] = parent_agent
    body = [(root, b"\n\x02" if root_data is None else root_data), (user, ('{"role":"user","text":"%s"}' % SENTINEL).encode())]
    body.extend(blobs or [(asst, assistant("grok-4.7-xhigh"))])
    db = os.path.join(chat_dir, "store.db")
    write_db(db, meta_obj, body)
    if transcript and label:
        enc = encoded if encoded is not None else mod.encode_cwd(cwd)
        tdir = os.path.join(home, "projects", enc, "agent-transcripts", uuid)
        os.makedirs(tdir, exist_ok=True)
        text = json.dumps(
            {
                "role": "user",
                "message": {
                    "content": [{"type": "text", "text": "[label: %s]\n%s" % (label, SENTINEL)}]
                },
            }
        )
        with open(os.path.join(tdir, uuid + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return db


def snap(path):
    out = {}
    for name in os.listdir(path):
        st = os.stat(os.path.join(path, name))
        out[name] = (st.st_size, getattr(st, "st_mtime_ns", st.st_mtime))
    return out


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "cursor")
        self.scratch = os.path.join(self.tmp.name, "scratch")
        os.makedirs(self.home)
        os.makedirs(self.scratch)
        self._live = mod.live_label_pids
        mod.live_label_pids = lambda: {}
        mod._LABEL_CACHE.clear()
        mod._TRANSCRIPTS.clear()
        mod._MODEL_COUNTS.clear()
        mod.last_error = None

    def tearDown(self):
        mod.live_label_pids = self._live
        self.tmp.cleanup()

    def cfg(self, **extra):
        return make_cfg(self.home, self.scratch, **extra)

    def test_encode_cwd(self):
        self.assertEqual(
            mod.encode_cwd("/home/ada/projects/demo-app"),
            "home-ada-projects-demo-app",
        )
        self.assertEqual(
            mod.encode_cwd("/tmp/agent-scratch/-home-ada-x/.claude"),
            "tmp-agent-scratch-home-ada-x-claude",
        )

    def test_label_model_and_no_secret(self):
        created = int((NOW - 100) * 1000)
        updated = int((NOW - 10) * 1000)
        add_chat(self.home, A, "/work/alpha", created, updated, "alpha", [(blob_id("22"), billed("grok-4.7-xhigh"))])
        write_manifest(self.scratch, [("alpha", NOW - 100)])
        store = Mem()
        before = snap(os.path.join(self.home, "chats", "bucket", A))
        mod.live_label_pids = lambda: {"alpha": 7}
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(snap(os.path.join(self.home, "chats", "bucket", A)), before)
        self.assertIsNone(mod.last_error)
        run = store.runs["grok:%s:alpha" % A]
        self.assertEqual(run["source"], "grok")
        self.assertEqual(run["tier"], "grok")
        self.assertEqual(run["model"], "grok-4.7-xhigh")
        self.assertEqual(run["label"], "alpha")
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["ended_at"])
        self.assertEqual(run["clock_source"], "transcript")
        self.assertAlmostEqual(run["started_at"], created / 1000.0)
        self.assertEqual(run["session_id"], A)
        self.assertEqual(len(store.usage), 1)
        usage = store.usage[0]
        self.assertEqual(usage["tool"], "cursor")
        self.assertEqual(usage["input"], 11)
        self.assertEqual(usage["output"], 7)
        self.assertEqual(usage["cache_read"], 3)
        self.assertEqual(usage["cache_write"], 2)
        self.assertEqual(usage["reasoning"], 5)
        self.assertEqual(usage["message_id"], blob_id("22"))
        text = dumped(store)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("blobEncryptionKey", text)

    def test_done_when_idle(self):
        updated = int((NOW - 120) * 1000)
        add_chat(self.home, A, "/work/alpha", updated - 1000, updated, "alpha", None)
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        run = store.runs["grok:%s:alpha" % A]
        self.assertEqual(run["status"], "done")
        self.assertAlmostEqual(run["ended_at"], updated / 1000.0)
        self.assertIsNone(run["pid"])

    def test_live_process_marks_newest_chat(self):
        older = int((NOW - 5000) * 1000)
        newer = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", older, older, "alpha", None)
        add_chat(self.home, B, "/work/alpha", newer, newer, "alpha", None)
        mod.live_label_pids = lambda: {"alpha": 42}
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        fresh = store.runs["grok:%s:alpha" % B]
        stale = store.runs["grok:%s:alpha" % A]
        self.assertEqual(fresh["status"], "running")
        self.assertEqual(fresh["pid"], 42)
        self.assertIsNone(fresh["ended_at"])
        self.assertEqual(stale["status"], "done")
        self.assertIsNone(stale["pid"])

    def test_transcript_label_beats_manifest(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)
        write_manifest(self.scratch, [("beta", created / 1000.0)])
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        self.assertIn("grok:%s:alpha" % A, store.runs)
        self.assertNotIn("beta", [run.get("label") for run in store.runs.values()])

    def test_manifest_assigns_unique_time(self):
        created = int(1_000_000 * 1000 + 1200)
        add_chat(self.home, A, "/work/alpha", created, created, None, None, transcript=False)
        write_manifest(self.scratch, [("beta", 1_000_000)])
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        run = store.runs["grok:%s:beta" % A]
        self.assertEqual(run["label"], "beta")
        self.assertEqual(run["clock_source"], "transcript")

    def test_shared_epoch_is_not_guessed(self):
        base = 1_000_000 * 1000
        add_chat(self.home, A, "/work/alpha", base + 1187, base + 1187, None, None, transcript=False)
        add_chat(self.home, B, "/work/alpha", base + 1177, base + 1177, None, None, transcript=False)
        write_manifest(self.scratch, [("one", 1_000_000), ("two", 1_000_000)])
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs, {})

    def test_manifest_respects_workspace(self):
        write_manifest(
            self.scratch,
            [("known", 1_000), ("later", 5_000)],
            session=PARENT,
        )
        add_chat(self.home, A, "/work/alpha", 1_000_500, 1_000_500, "known", None)
        add_chat(self.home, B, "/work/beta", 5_000_100, 5_000_100, None, None, transcript=False)
        add_chat(self.home, C, "/work/alpha", 5_000_200, 5_000_200, None, None, transcript=False)
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs["grok:%s:known" % A]["parent_session_id"], PARENT)
        self.assertEqual(store.runs["grok:%s:later" % C]["label"], "later")
        self.assertNotIn(B, [run["session_id"] for run in store.runs.values()])

    def test_fallback_transcript_dir(self):
        created = int((NOW - 4000) * 1000)
        add_chat(
            self.home,
            A,
            "/work/alpha",
            created,
            created,
            "alpha",
            None,
            encoded="somewhere-else",
        )
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs["grok:%s:alpha" % A]["label"], "alpha")

    def test_second_poll_skips_store_and_sees_new_blobs(self):
        created = int((NOW - 4000) * 1000)
        db = add_chat(self.home, A, "/work/alpha", created, created, "alpha", [(blob_id("22"), assistant("grok-4.7-high"))])
        store = Mem()
        opens = []
        real = mod.open_store

        def wrapped(path, tmp_root, budget, tick_budget):
            opens.append(path)
            return real(path, tmp_root, budget, tick_budget)

        mod.open_store = wrapped
        try:
            mod.poll(store, self.cfg(), NOW)
            self.assertEqual(store.runs["grok:%s:alpha" % A]["model"], "grok-4.7-high")
            self.assertEqual(len(opens), 1)
            opens.clear()
            mod.poll(store, self.cfg(), NOW)
            self.assertEqual(opens, [])
        finally:
            mod.open_store = real
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (blob_id("44"), assistant("grok-4.7-xhigh")),
        )
        con.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (blob_id("55"), assistant("grok-4.7-xhigh")),
        )
        con.commit()
        con.close()
        updated = created + 5000
        meta_path = os.path.join(self.home, "chats", "bucket", A, "meta.json")
        meta = json.loads(Path(meta_path).read_text())
        meta["updatedAtMs"] = updated
        Path(meta_path).write_text(json.dumps(meta))
        mod._LABEL_CACHE.clear()
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs["grok:%s:alpha" % A]["model"], "grok-4.7-xhigh")

    def test_tick_budget_defers_large_blob(self):
        recent = int(NOW * 1000)
        old = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", recent, recent, "small", [(blob_id("22"), assistant("grok-4.7-high"))])
        add_chat(
            self.home,
            B,
            "/work/alpha",
            old,
            old,
            "big",
            [(blob_id("22"), assistant("grok-4.7-xhigh", pad=b"x" * 500_000))],
        )
        store = Mem()
        cfg = self.cfg()
        cfg["read_budget"] = {"per_file": 4 * 1024 * 1024, "per_tick": 80_000}
        mod.poll(store, cfg, NOW)
        self.assertEqual(store.runs["grok:%s:small" % A]["model"], "grok-4.7-high")
        big = store.runs["grok:%s:big" % B]
        self.assertIsNone(big.get("model"))
        self.assertIsNone(store.get_offset(os.path.realpath(os.path.join(self.home, "chats", "bucket", B, "store.db"))))

    def test_wal_copy_reads_uncheckpointed_rows(self):
        created = int((NOW - 4000) * 1000)
        chat_dir = os.path.join(self.home, "chats", "bucket", A)
        os.makedirs(chat_dir)
        with open(os.path.join(chat_dir, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {"cwd": "/work/alpha", "createdAtMs": created, "updatedAtMs": created},
                fh,
            )
        db = os.path.join(chat_dir, "store.db")
        root = blob_id("11")
        con = write_db(
            db,
            {
                "agentId": A,
                "latestRootBlobId": root,
                "createdAt": created,
                "blobEncryptionKey": SECRET,
            },
            [(root, b"\n\x02"), (blob_id("22"), assistant("grok-4.7-xhigh"))],
            keep_open=True,
        )
        self.addCleanup(con.close)
        enc = mod.encode_cwd("/work/alpha")
        tdir = os.path.join(self.home, "projects", enc, "agent-transcripts", A)
        os.makedirs(tdir)
        with open(os.path.join(tdir, A + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write('[label: alpha]\n')
        before = snap(chat_dir)
        uris = []
        real = mod.sqlite3.connect

        def wrapped(database, *args, **kwargs):
            uris.append(database)
            return real(database, *args, **kwargs)

        mod.sqlite3.connect = wrapped
        try:
            store = Mem()
            mod.poll(store, self.cfg(), NOW)
        finally:
            mod.sqlite3.connect = real
        self.assertEqual(snap(chat_dir), before)
        self.assertEqual(store.runs["grok:%s:alpha" % A]["model"], "grok-4.7-xhigh")
        self.assertTrue(uris)
        self.assertTrue(all("/chats/" not in uri for uri in uris))
        self.assertNotIn(SECRET, dumped(store))

    def test_immutable_open_creates_no_sidecar(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)
        chat_dir = os.path.join(self.home, "chats", "bucket", A)
        before = snap(chat_dir)
        mod.poll(Mem(), self.cfg(), NOW)
        self.assertEqual(snap(chat_dir), before)
        self.assertNotIn("store.db-shm", before)
        self.assertNotIn("store.db-wal", before)

    def test_bad_db_does_not_block_the_sibling(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)
        bad = os.path.join(self.home, "chats", "bucket", B)
        os.makedirs(bad)
        with open(os.path.join(bad, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"cwd": "/work/alpha", "createdAtMs": created, "updatedAtMs": created}, fh)
        with open(os.path.join(bad, "store.db"), "wb") as fh:
            fh.write(b"not a database")
        enc = mod.encode_cwd("/work/alpha")
        tdir = os.path.join(self.home, "projects", enc, "agent-transcripts", B)
        os.makedirs(tdir)
        with open(os.path.join(tdir, B + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write("[label: beta]\n")
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs["grok:%s:alpha" % A]["model"], "grok-4.7-xhigh")
        self.assertIn("grok:%s:beta" % B, store.runs)

    def test_backfill_limits_labels(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "keep", None)
        add_chat(self.home, B, "/work/alpha", created, created, "other", None)
        write_manifest(self.scratch, [("keep", created / 1000.0)], session=PARENT)
        write_manifest(self.scratch, [("other", created / 1000.0)], session=OTHER)
        store = Mem()
        mod.poll(store, self.cfg(backfill_session=PARENT), NOW)
        labels = [run.get("label") for run in store.runs.values()]
        self.assertEqual(labels, ["keep"])
        self.assertEqual(store.runs["grok:%s:keep" % A]["parent_session_id"], PARENT)

    def test_poll_survives_store_errors(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)

        class Boom(Mem):
            def upsert_run(self, row):
                raise RuntimeError("boom")

        mod.poll(Boom(), self.cfg(), NOW)
        self.assertEqual(mod.last_error, "run")

    def test_zero_budget_reads_nothing(self):
        created = int((NOW - 10) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)
        store = Mem()
        mod.poll(store, self.cfg(read_budget_per_tick=0), NOW)
        self.assertEqual(store.runs, {})

    def test_real_store_accepts_the_row(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", [(blob_id("22"), billed("grok-4.7-xhigh"))])
        path = os.path.join(self.tmp.name, "dash.sqlite")
        store = Store(path)
        try:
            mod.poll(store, self.cfg(), NOW)
            row = dict(store._conn.execute("SELECT * FROM runs").fetchone())
            usage = dict(store._conn.execute("SELECT * FROM usage_events").fetchone())
        finally:
            store.close()
        self.assertEqual(row["source"], "grok")
        self.assertEqual(row["tier"], "grok")
        self.assertEqual(row["model"], "grok-4.7-xhigh")
        self.assertEqual(row["status"], "done")
        self.assertEqual(usage["input_tokens"], 11)
        self.assertEqual(usage["output_tokens"], 7)
        self.assertEqual(usage["cache_read"], 3)
        self.assertEqual(usage["cache_write"], 2)
        self.assertIsNone(mod.last_error)

    def test_fresh_update_without_process_is_not_running(self):
        updated = int(NOW * 1000)
        add_chat(self.home, A, "/work/alpha", updated, updated, "alpha", None)
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        run = store.runs["grok:%s:alpha" % A]
        self.assertEqual(run["status"], "done")
        self.assertIsNone(run["pid"])

    def test_child_chat_links_to_parent_and_is_not_running(self):
        updated = int(NOW * 1000)
        add_chat(self.home, A, "/work/alpha", updated - 5000, updated - 5000, "alpha", None)
        add_chat(self.home, B, "/work/alpha", updated, updated, None, None, transcript=False, parent_agent=A)
        mod.live_label_pids = lambda: {"alpha": 42}
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        parent = store.runs["grok:%s:alpha" % A]
        child = store.runs["grok:%s:%s" % (B, B)]
        self.assertEqual(parent["status"], "running")
        self.assertEqual(parent["pid"], 42)
        self.assertEqual(child["status"], "done")
        self.assertEqual(child["parent_session_id"], A)
        self.assertIsNone(child.get("pid"))

    def test_unlabeled_shadow_links_to_labeled_run(self):
        updated = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", updated, updated, "alpha", None)
        store = Mem()
        shadow = "grok:%s:%s" % (A, A)
        store.runs[shadow] = {
            "run_id": shadow,
            "session_id": A,
            "status": "running",
            "started_at": updated / 1000.0,
        }
        mod.poll(store, self.cfg(), NOW)
        self.assertEqual(store.runs[shadow]["status"], "unknown")
        self.assertEqual(store.runs[shadow]["parent_session_id"], "grok:%s:alpha" % A)
        self.assertEqual(store.runs["grok:%s:alpha" % A]["status"], "done")

    def test_hook_log_joins_chat_and_ignores_prompt_text(self):
        created = int((NOW - 4000) * 1000)
        add_chat(self.home, A, "/work/alpha", created, created, "alpha", None)
        log = os.path.join(self.tmp.name, "cursor-usage.jsonl")
        hook, _path = hook_script()
        payload = {
            "hook_event_name": "stop",
            "conversation_id": A,
            "generation_id": "gen-1",
            "model": "grok-4.7",
            "workspace_roots": ["/work/alpha"],
            "text": SENTINEL,
            "user_email": SECRET,
            "transcript_path": "/tmp/%s" % SENTINEL,
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_tokens": 3,
            "cache_write_tokens": 2,
        }
        row = hook.build_row(payload)
        self.assertNotIn(SENTINEL, json.dumps(row))
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertIsNone(row["reasoning_tokens"])
        hook.append_row(row, log)
        partial = b'{"chat_id":"%s","input_tokens":1}' % A.encode()
        with open(log, "ab") as fh:
            fh.write(partial)
        store = Mem()
        mod.poll(store, self.cfg(cursor_usage=log), NOW)
        usage = [item for item in store.usage if item["tool"] == "cursor"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["input"], 6)
        self.assertEqual(usage[0]["output"], 7)
        self.assertNotIn(SENTINEL, dumped(store))
        self.assertNotIn(SECRET, dumped(store))
        mod.poll(store, self.cfg(cursor_usage=log), NOW)
        self.assertEqual(len([item for item in store.usage if item["tool"] == "cursor"]), 1)
        with open(log, "ab") as fh:
            fh.write(b"\n")
        mod.poll(store, self.cfg(cursor_usage=log), NOW)
        nums = [item["input"] for item in store.usage if item["tool"] == "cursor"]
        self.assertEqual(nums, [6, 1])

    def test_hook_script_exits_zero_and_drops_other_events(self):
        hook, path = hook_script()
        self.assertIsNone(hook.build_row({"hook_event_name": "afterAgentResponse", "conversation_id": A, "text": SENTINEL, "input_tokens": 4}))
        log = os.path.join(self.tmp.name, "from-stdin.jsonl")
        env = dict(os.environ)
        env["AGENT_DASH_CURSOR_USAGE"] = log
        proc = subprocess.run(
            [sys.executable, path],
            input=json.dumps(
                {
                    "hook_event_name": "stop",
                    "conversation_id": A,
                    "model": "grok-4.7",
                    "workspace_roots": ["/work/alpha"],
                    "user_email": SECRET,
                    "text": SENTINEL,
                    "input_tokens": 9,
                    "output_tokens": 1,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 4,
                }
            ).encode(),
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        line = Path(log).read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, line)
        self.assertNotIn(SECRET, line)
        saved = json.loads(line)
        self.assertEqual(saved["chat_id"], A)
        self.assertEqual(saved["reasoning_tokens"], 4)
        self.assertEqual(saved["cwd"], "/work/alpha")
        bad = subprocess.run([sys.executable, path], input=b"not-json", capture_output=True, env=env, check=False)
        self.assertEqual(bad.returncode, 0)
        self.assertEqual(Path(log).read_text(encoding="utf-8").count("\n"), 1)

    def test_context_window_is_a_separate_event(self):
        created = int((NOW - 4000) * 1000)
        add_chat(
            self.home,
            A,
            "/work/alpha",
            created,
            created,
            "alpha",
            [(blob_id("22"), billed("grok-4.7-xhigh"))],
            root_data=context_root(114442, 500000),
        )
        store = Mem()
        mod.poll(store, self.cfg(), NOW)
        kinds = {}
        for item in store.usage:
            kinds.setdefault(item["tool"], []).append(item)
        self.assertEqual(len(kinds["cursor"]), 1)
        self.assertEqual(kinds["cursor"][0]["input"], 11)
        ctx = kinds["grok-context"][0]
        self.assertEqual(ctx["input"], 114442)
        self.assertEqual(ctx["output"], 500000)
        self.assertEqual(ctx["reasoning"], 0)
        self.assertEqual(ctx["run_id"], "grok:%s:alpha" % A)
        text = dumped(store)
        self.assertNotIn("SECRET_PATH_DO_NOT_STORE", text)
        self.assertNotIn(SENTINEL, text)

    def test_registry_discovers_cursor(self):
        from dash.sources import discover

        names = [source.NAME for source in discover()]
        self.assertIn("cursor", names)


class SmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local cursor home")
    def test_manifest_from_env(self):
        manifest = os.environ.get("AGENT_DASH_SMOKE_MANIFEST")
        home = os.environ.get("AGENT_DASH_SMOKE_CURSOR_HOME")
        if not manifest or not home:
            self.skipTest("AGENT_DASH_SMOKE_MANIFEST and AGENT_DASH_SMOKE_CURSOR_HOME are not set")
        self.assertTrue(os.path.isfile(manifest))
        self.assertTrue(os.path.isdir(home))
        text = Path(manifest).read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertGreaterEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
