"""Native agent-session archive: discovery, packing, ingest, reclaim, restore.

Synthetic fixtures copy the key shapes of every tool layout (tiny files).
The store is a fake client; no real root, no live VictoriaLogs and no
launchctl is touched. Real-data runs are dry runs only.
"""

import fcntl
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import sessionarchive as sa  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"

_UNIT_RE = re.compile(r'unit:="([^"]*)"')
_REV_RE = re.compile(r'rev:="([^"]*)"')


class FakeClient:
    """In-memory stand-in for ``vlogs.VLogsClient``."""

    def __init__(self, healthy=True):
        self.rows = []
        self.healthy_ = healthy
        self.flushes = 0

    def healthy(self):
        return self.healthy_

    def insert(self, lines):
        sent = 0
        for line in lines:
            self.rows.append(json.loads(line))
            sent += 1
        return sent

    def flush(self):
        self.flushes += 1

    def query(self, query):
        match = _UNIT_RE.search(query)
        if not match:
            return [{"n": "0"}]
        unit = match.group(1)
        rows = [r for r in self.rows if r.get("unit") == unit]
        rev = _REV_RE.search(query)
        if rev:
            rows = [r for r in rows if r.get("rev") == rev.group(1)]
        if "count_uniq" in query:
            pairs = {(r.get("member"), r.get("line")) for r in rows}
            return [{"n": str(len(pairs))}]
        return [{"n": str(len(rows))}]


class BadCountClient(FakeClient):
    """Reports one row fewer than it stored, so verification fails."""

    def query(self, query):
        rows = super().query(query)
        return [{"n": str(max(0, int(rows[0]["n"]) - 1))}]


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def age(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def _archive_diag(rec):
    """Why ``_archive_ok`` would say no: path, size vs recorded, verify error."""
    archive = rec.get("archive")
    try:
        size = os.path.getsize(archive) if archive else None
    except OSError as exc:
        size = "stat failed: %s" % exc
    try:
        sa.verify_archive(archive, rec.get("members") or [])
        why = "verifies"
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        why = "%s: %s" % (type(exc).__name__, exc)
    return "archive=%r size=%r archive_bytes=%r verify=%s" % (
        archive,
        size,
        rec.get("archive_bytes"),
        why,
    )


def zstd_compress(src, dst):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["zstd", "-q", "-f", "-o", str(dst), str(src)], check=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # lsof lives in /usr/sbin on macOS. The tests need it for the
        # open-handle probe, so they add it themselves rather than relying
        # on the caller's PATH.
        old_path = os.environ.get("PATH", "")
        parts = old_path.split(os.pathsep) if old_path else []
        for extra in ("/usr/sbin", "/sbin"):
            if extra not in parts:
                parts.append(extra)
        os.environ["PATH"] = os.pathsep.join(parts)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))

    def unit(self, tool, root):
        units = sa.TOOLS[tool][1](str(root))
        self.assertTrue(units, "no units discovered for %s" % tool)
        return units[0]

    def archive_unit(self, unit, dest, client, status="archived"):
        """Pack a unit into ``dest``, ingest it, and write an archived record."""
        seq = sa.next_archive_seq(dest, unit)
        archive = sa.archive_path(dest, unit, seq)
        archive.parent.mkdir(parents=True, exist_ok=True)
        records = sa.pack(unit, str(archive))
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = sa.count_member_rows(member, unit)
        sa.verify_archive(str(archive), records)
        rev, total, _sent = sa.ingest_unit(unit, records, None, client)
        manifest = sa.Manifest(dest / "state")
        rec = {
            "unit": unit.uid,
            "tool": unit.tool,
            "key": unit.key,
            "session": unit.session,
            "root": unit.root,
            "parent": unit.parent,
            "archive": str(archive),
            "archive_bytes": os.path.getsize(archive),
            "seq": seq,
            "archives": [
                {"seq": seq, "path": str(archive), "bytes": os.path.getsize(archive), "members": records}
            ],
            "raw_bytes": unit.raw_bytes,
            "rev": rev,
            "lines": total,
            "members": records,
            "status": status,
        }
        manifest.write(rec)
        return rec, manifest


# --------------------------------------------------------------- discovery

class DiscoveryTest(Base):
    def test_claude_layout_and_memory_excluded(self):
        root = self.home / "claude"
        proj = root / "-Users-me-proj"
        write(proj / "abc.jsonl", '{"type":"user","timestamp":"2026-11-01T00:00:00.000Z"}\n')
        write(proj / "abc" / "subagents" / "agent-1.jsonl", '{"type":"assistant"}\n')
        write(proj / "abc" / "tool-results" / "r1.txt", "result")
        write(proj / "abc" / "memory" / "note.jsonl", '{"type":"memory"}\n')
        unit = self.unit("claude", root)
        self.assertEqual(unit.key, "-Users-me-proj/abc")
        self.assertEqual(unit.session, "abc")
        self.assertEqual(unit.parent, str(proj))
        rels = {m.rel: m.rows for m in unit.members}
        self.assertEqual(rels["abc.jsonl"], "jsonl")
        self.assertEqual(rels["abc/subagents/agent-1.jsonl"], "jsonl")
        self.assertEqual(rels["abc/tool-results/r1.txt"], "none")
        self.assertNotIn("abc/memory/note.jsonl", rels)

    def test_codex_layout_plain_and_zst(self):
        root = self.home / "codex"
        name = "rollout-2026-11-03T14-18-08-01a1a2d8-ef9e-7771-a442-27b79500e9aa.jsonl"
        write(root / "sessions" / "2026" / "11" / "03" / name, '{"type":"session_meta"}\n')
        zname = "rollout-2026-11-03T11-27-46-01a1a23c-f580-7c42-a723-1b4f18c257f2.jsonl.zst"
        src = write(self.home / "src.jsonl", '{"type":"event_msg"}\n')
        zstd_compress(src, root / "sessions" / "2026" / "11" / "03" / zname)
        aname = "rollout-2026-10-23T09-02-15-01a168da-daeb-7113-a1ac-5f5a8c2e3e3a.jsonl.zst"
        zstd_compress(src, root / "archived_sessions" / aname)
        units = sa.TOOLS["codex"][1](str(root))
        self.assertEqual(len(units), 3)
        sessions = {u.session for u in units}
        self.assertIn("01a1a2d8-ef9e-7771-a442-27b79500e9aa", sessions)
        self.assertIn("01a1a23c-f580-7c42-a723-1b4f18c257f2", sessions)
        self.assertIn("01a168da-daeb-7113-a1ac-5f5a8c2e3e3a", sessions)

    def test_grok_layout(self):
        root = self.home / "grok"
        sdir = root / "%2Fprivate%2Ftmp" / "01a15bcc-1ac0-7c60-90ad-8dca223a1a21"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n')
        write(sdir / "events.jsonl", '{"ts":"2026-10-21T00:11:05.374Z","type":"mcp"}\n')
        write(sdir / "worker.log", "[info] hello\n")
        write(sdir / "summary.json", "{}")
        write(root / "session_search.sqlite", b"x")
        unit = self.unit("grok", root)
        self.assertEqual(unit.session, "01a15bcc-1ac0-7c60-90ad-8dca223a1a21")
        rels = {m.rel: m.rows for m in unit.members}
        self.assertEqual(rels["01a15bcc-1ac0-7c60-90ad-8dca223a1a21/chat_history.jsonl"], "jsonl")
        self.assertEqual(rels["01a15bcc-1ac0-7c60-90ad-8dca223a1a21/worker.log"], "log")
        self.assertEqual(rels["01a15bcc-1ac0-7c60-90ad-8dca223a1a21/summary.json"], "none")

    def test_cursor_chats_and_projects(self):
        root = self.home / "cursor"
        cdir = root / "chats" / "ws1" / "chat1"
        cdir.mkdir(parents=True)
        con = sqlite3.connect(str(cdir / "store.db"))
        con.execute("create table meta (key text primary key, value text)")
        con.execute("create table blobs (id text primary key, data blob)")
        con.execute(
            "insert into meta values (?, ?)",
            ("0", json.dumps({"agentId": "chat1", "name": "New Agent"}).encode().hex()),
        )
        con.execute("insert into blobs values (?, ?)", ("b1", b'{"role":"user","text":"hi"}'))
        con.execute("insert into blobs values (?, ?)", ("b2", b"\x00\x01binary"))
        con.commit()
        con.close()
        write(cdir / "meta.json", '{"schemaVersion":1}')
        pdir = root / "projects" / "proj1"
        write(pdir / "agent-transcripts" / "t1" / "t1.jsonl", '{"role":"user"}\n')
        write(pdir / "worker.log", "[info] x\n")
        units = sa.TOOLS["cursor"][1](str(root))
        self.assertEqual(len(units), 2)
        chat = [u for u in units if u.key.startswith("chats/")][0]
        self.assertEqual(chat.session, "chat1")
        self.assertEqual({m.rows for m in chat.members}, {"cursor-store", "none"})
        proj = [u for u in units if u.key.startswith("projects/")][0]
        rels = {m.rel: m.rows for m in proj.members}
        self.assertEqual(rels["proj1/agent-transcripts/t1/t1.jsonl"], "jsonl")
        self.assertEqual(rels["proj1/worker.log"], "log")

    def test_commandcode_groups_by_stem(self):
        root = self.home / "cc"
        pdir = root / "proj"
        write(pdir / "uuid1.jsonl", '{"type":"session"}\n')
        write(pdir / "uuid1.checkpoints.jsonl", '{"type":"checkpoint"}\n')
        write(pdir / "uuid1.meta.json", "{}")
        write(pdir / "uuid2.jsonl", '{"type":"session"}\n')
        units = sa.TOOLS["commandcode"][1](str(root))
        self.assertEqual(len(units), 2)
        first = [u for u in units if u.session == "uuid1"][0]
        self.assertEqual(len(first.members), 3)
        self.assertEqual(sum(1 for m in first.members if m.rows == "jsonl"), 2)

    def test_pi_layout(self):
        root = self.home / "pi"
        name = "2026-11-03T18-25-46-002Z_01a1a2a8-fc12-70f0-bea8-ace37f68b55b.jsonl"
        write(root / "--Users-me-proj--" / name, '{"type":"session"}\n')
        unit = self.unit("pi", root)
        self.assertEqual(unit.session, "01a1a2a8-fc12-70f0-bea8-ace37f68b55b")
        self.assertEqual(unit.members[0].rows, "jsonl")

    def test_muse_layout_and_views_ignored(self):
        root = self.home / "muse"
        sdir = root / "2026" / "11" / "03" / "6211d5b4-ae34-4a25-b95c-e95d4a465dcd"
        write(sdir / "session.jsonl", '{"retained_frame":"x","children":[]}\n')
        write(sdir / "cron.db", b"x")
        write(sdir / "tool-outputs" / "o1.txt", "out")
        write(root / ".msp-view-g6" / "HEAD.json", "{}")
        write(root / "session-index.db", b"x")
        units = sa.TOOLS["muse"][1](str(root))
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit.session, "6211d5b4-ae34-4a25-b95c-e95d4a465dcd")
        rels = {m.rel: m.rows for m in unit.members}
        self.assertEqual(rels["6211d5b4-ae34-4a25-b95c-e95d4a465dcd/session.jsonl"], "jsonl")
        self.assertEqual(rels["6211d5b4-ae34-4a25-b95c-e95d4a465dcd/cron.db"], "none")

    def test_absent_root_reported(self):
        with mock.patch.dict(sa.TOOLS, {"pi": (str(self.home / "nope"), sa.discover_pi)}):
            units, meta = sa.discover(["pi"])
        self.assertEqual(units, [])
        self.assertEqual(meta["absent"], ["pi"])

    def test_symlinks_are_skipped(self):
        root = self.home / "pi"
        real = write(self.home / "outside.jsonl", '{"type":"session"}\n')
        pdir = root / "proj"
        pdir.mkdir(parents=True)
        os.symlink(real, pdir / "link.jsonl")
        self.assertEqual(sa.TOOLS["pi"][1](str(root)), [])


# --------------------------------------------------------------- gating

class GatingTest(Base):
    def test_idle_and_open(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        unit = self.unit("pi", root)
        cfg = {"min_idle_hours": 6.0, "reclaim_days": 14.0, "reclaim_days_by_tool": {}}
        now = time.time()
        self.assertFalse(sa._idle(unit, cfg, now))
        age(path, 1)
        unit = self.unit("pi", root)
        self.assertTrue(sa._idle(unit, cfg, now))
        self.assertTrue(sa._is_open(unit, {os.path.realpath(str(path))}))
        self.assertFalse(sa._is_open(unit, set()))


# --------------------------------------------------------------- pack

class PackTest(Base):
    def test_roundtrip_byte_identical_dir_unit(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n{"type":"user"}\n')
        write(sdir / "blob.bin", bytes(range(256)) * 10)
        write(sdir / "empty" / ".keep", "")
        unit = self.unit("grok", root)
        archive = self.home / "out.tar.zst"
        records = sa.pack(unit, str(archive))
        sa.verify_archive(str(archive), records)
        target = self.home / "restore"
        target.mkdir()
        fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY)
        try:
            sa._extract(str(archive), fd, records)
        finally:
            os.close(fd)
        for member in unit.members:
            original = Path(member.path).read_bytes()
            restored = (target / member.rel).read_bytes()
            self.assertEqual(original, restored, member.rel)

    def test_codex_zst_member_roundtrip_and_rows(self):
        root = self.home / "codex"
        src = write(self.home / "src.jsonl", '{"type":"event_msg","timestamp":"2026-11-03T00:00:00.000Z"}\n')
        zname = "rollout-2026-11-03T11-27-46-01a1a23c-f580-7c42-a723-1b4f18c257f2.jsonl.zst"
        zstd_compress(src, root / "sessions" / "2026" / "11" / "03" / zname)
        unit = self.unit("codex", root)
        archive = self.home / "out.tar.zst"
        records = sa.pack(unit, str(archive))
        sa.verify_archive(str(archive), records)
        target = self.home / "restore"
        target.mkdir()
        fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY)
        try:
            sa._extract(str(archive), fd, records)
        finally:
            os.close(fd)
        self.assertEqual(
            (target / unit.members[0].rel).read_bytes(),
            (root / "sessions" / "2026" / "11" / "03" / zname).read_bytes(),
        )
        rows = list(sa.iter_member_rows(unit.members[0], unit))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "event_msg")

    def test_verify_detects_corruption(self):
        root = self.home / "pi"
        write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        unit = self.unit("pi", root)
        archive = self.home / "out.tar.zst"
        records = sa.pack(unit, str(archive))
        records[0]["sha256"] = "0" * 64
        with self.assertRaises(sa.ArchiveError):
            sa.verify_archive(str(archive), records)


# --------------------------------------------------------------- ingest

class IngestTest(Base):
    def _records(self, unit, archive):
        records = sa.pack(unit, str(archive))
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = sa.count_member_rows(member, unit)
        return records

    def test_ingest_counts_and_growth(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        unit = self.unit("pi", root)
        client = FakeClient()
        records = self._records(unit, self.home / "a.tar.zst")
        rev, total, sent = sa.ingest_unit(unit, records, None, client)
        self.assertEqual((rev, total, sent), ("0", 2, 2))
        self.assertEqual(len(client.rows), 2)
        self.assertEqual(client.rows[0]["ws"], "native/pi")
        self.assertEqual(client.rows[0]["kind"], "native")
        self.assertEqual(client.rows[0]["unit"], unit.uid)
        self.assertEqual(client.rows[0]["rev"], "0")

        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type":"message"}\n')
        unit2 = self.unit("pi", root)
        records2 = self._records(unit2, self.home / "b.tar.zst")
        prior = {"unit": unit.uid, "rev": rev, "members": records}
        rev2, total2, sent2 = sa.ingest_unit(unit2, records2, prior, client)
        self.assertEqual((rev2, total2, sent2), ("0", 3, 1))
        self.assertEqual(len(client.rows), 3)

    def test_rewrite_bumps_rev_and_resends(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        unit = self.unit("pi", root)
        client = FakeClient()
        records = self._records(unit, self.home / "a.tar.zst")
        rev, _total, _sent = sa.ingest_unit(unit, records, None, client)
        # Rewrite the first line in place (same size, different bytes).
        path.write_text('{"type":"other"}\n{"type":"message"}\n', encoding="utf-8")
        unit2 = self.unit("pi", root)
        records2 = self._records(unit2, self.home / "b.tar.zst")
        prior = {"unit": unit.uid, "rev": rev, "members": records}
        rev2, total2, sent2 = sa.ingest_unit(unit2, records2, prior, client)
        self.assertEqual(rev2, "1")
        self.assertEqual(total2, 2)
        self.assertEqual(sent2, 2)
        self.assertEqual(len([r for r in client.rows if r["rev"] == "1"]), 2)

    def test_store_mismatch_is_an_error(self):
        root = self.home / "pi"
        write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        unit = self.unit("pi", root)
        client = FakeClient()
        records = self._records(unit, self.home / "a.tar.zst")
        # One row already stored but two lines expected, and no manifest record.
        client.rows.append({"unit": unit.uid, "rev": "0"})
        with self.assertRaises(sa.ArchiveError):
            sa.ingest_unit(unit, records, None, client)

    def test_rerun_without_manifest_record_skips_post(self):
        root = self.home / "pi"
        write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        unit = self.unit("pi", root)
        client = FakeClient()
        records = self._records(unit, self.home / "a.tar.zst")
        sa.ingest_unit(unit, records, None, client)
        rev, total, sent = sa.ingest_unit(unit, records, None, client)
        self.assertEqual((rev, total, sent), ("0", 2, 0))
        self.assertEqual(len(client.rows), 2)

    def test_cursor_store_rows(self):
        root = self.home / "cursor"
        cdir = root / "chats" / "ws1" / "chat1"
        cdir.mkdir(parents=True)
        con = sqlite3.connect(str(cdir / "store.db"))
        con.execute("create table meta (key text primary key, value text)")
        con.execute("create table blobs (id text primary key, data blob)")
        con.execute("insert into meta values (?, ?)", ("0", json.dumps({"agentId": "chat1"}).encode().hex()))
        con.execute("insert into blobs values (?, ?)", ("b1", b'{"role":"user"}'))
        con.execute("insert into blobs values (?, ?)", ("b2", b"\x00\x01"))
        con.commit()
        con.close()
        unit = self.unit("cursor", root)
        member = [m for m in unit.members if m.rows == "cursor-store"][0]
        rows = list(sa.iter_member_rows(member, unit))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "raw")
        self.assertEqual(rows[1][1], "user")

    def test_muse_nested_record_fields(self):
        root = self.home / "muse"
        sdir = root / "2026" / "11" / "03" / "sid1"
        inner = json.dumps(
            {"recorded_at": 1793760857594213, "record_type": "event", "payload_type": "runtime.session.x"}
        )
        frame = json.dumps({"retained_frame": "f", "children": [{"record_json": inner}]})
        write(sdir / "session.jsonl", frame + "\n")
        unit = self.unit("muse", root)
        rows = list(sa.iter_member_rows(unit.members[0], unit))
        self.assertEqual(rows[0][1], "event")
        self.assertEqual(rows[0][5], "runtime.session.x")
        self.assertEqual(sa.iso_from_ms(rows[0][2]), "2026-11-04T02:54:17.594Z")


# --------------------------------------------------------------- live

class LiveTest(Base):
    def test_offsets_growth_and_shrink(self):
        path = write(self.home / ".claude" / "history.jsonl", '{"sessionId":"s1","display":"a"}\n')
        manifest = sa.Manifest(self.home / "state")
        client = FakeClient()
        with mock.patch.object(sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p):
            sent = sa.ingest_live("claude", str(path), manifest, client)
        self.assertEqual(sent, 1)
        rec = manifest.latest["live/claude/.claude/history.jsonl"]
        self.assertEqual(rec["offset"], len(path.read_bytes()))

        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"sessionId":"s1","display":"b"}\n')
        with mock.patch.object(sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p):
            sent = sa.ingest_live("claude", str(path), manifest, client)
        self.assertEqual(sent, 1)
        self.assertEqual(len(client.rows), 2)

        # Shrink: restart from 0 with rev+1.
        path.write_text('{"sessionId":"s1","display":"c"}\n', encoding="utf-8")
        with mock.patch.object(sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p):
            sent = sa.ingest_live("claude", str(path), manifest, client)
        self.assertEqual(sent, 1)
        rec = manifest.latest["live/claude/.claude/history.jsonl"]
        self.assertEqual(rec["rev"], "1")

    def test_partial_line_is_left_for_next_run(self):
        path = write(self.home / ".codex" / "history.jsonl", '{"session_id":"s","text":"a"}\n{"session_id":"s"')
        manifest = sa.Manifest(self.home / "state")
        client = FakeClient()
        with mock.patch.object(sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p):
            sent = sa.ingest_live("codex", str(path), manifest, client)
        self.assertEqual(sent, 1)
        rec = manifest.latest["live/codex/.codex/history.jsonl"]
        self.assertEqual(rec["offset"], len('{"session_id":"s","text":"a"}\n'))


# --------------------------------------------------------------- reclaim

class ReclaimTest(Base):
    def _archived(self, root, days=20, client=None):
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        age(path, days)
        unit = self.unit("pi", root)
        dest = self.home / "dest"
        client = client or FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        return unit, rec, path, dest, manifest, client

    def _cfg(self, dest, **extra):
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}, "dest": str(dest)}
        cfg.update(extra)
        return cfg

    def test_reclaim_deletes_exactly_the_unit(self):
        root = self.home / "pi"
        unit, rec, path, dest, manifest, client = self._archived(root)
        other = write(root / "proj" / "b.jsonl", '{"type":"session"}\n')
        age(other, 20)
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIsNone(reason)
        self.assertFalse(path.exists())
        self.assertTrue(other.exists())
        self.assertEqual(sa.Manifest(dest / "state").latest[unit.uid]["status"], "reclaimed")

    def test_refused_when_too_young(self):
        root = self.home / "pi"
        unit, rec, path, dest, manifest, client = self._archived(root, days=1)
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("too young", reason)
        self.assertTrue(path.exists())

    def test_refused_when_changed_since_archive(self):
        root = self.home / "pi"
        unit, rec, path, dest, manifest, client = self._archived(root)
        path.write_text('{"type":"session","x":1}\n', encoding="utf-8")
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("changed since archive", reason)
        self.assertTrue(path.exists())

    def test_refused_when_path_escapes_root(self):
        root = self.home / "pi"
        unit, rec, _path, dest, manifest, client = self._archived(root)
        outside = write(self.home / "outside.jsonl", '{"type":"session"}\n')
        age(outside, 20)
        unit.members[0].path = str(outside)
        with self.assertRaises(sa.ArchiveError) as ctx:
            sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("not under the tool root", str(ctx.exception))
        self.assertTrue(outside.exists())

    def test_refused_when_path_under_dest_nested_in_root(self):
        root = self.home / "pi"
        unit, rec, _path, _dest, manifest, client = self._archived(root)
        dest = root / "archive-dest"
        nested = write(dest / "proj" / "a.jsonl", '{"type":"session"}\n')
        age(nested, 20)
        unit.members[0].path = str(nested)
        with self.assertRaises(sa.ArchiveError) as ctx:
            sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("under the archive dest", str(ctx.exception))
        self.assertTrue(nested.exists())

    def test_refused_when_unit_is_a_symlink_into_dest(self):
        root = self.home / "pi"
        unit, rec, path, _dest, manifest, client = self._archived(root)
        dest = root / "store"
        real = write(dest / "sessions" / "a.jsonl", '{"type":"session"}\n')
        age(real, 20)
        os.remove(path)
        os.symlink(real, path)
        unit.members[0].path = str(path)
        with self.assertRaises(sa.ArchiveError) as ctx:
            sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("under the archive dest", str(ctx.exception))
        self.assertTrue(real.exists())
        self.assertTrue(path.is_symlink())

    def test_per_tool_override(self):
        root = self.home / "pi"
        unit, rec, path, dest, manifest, client = self._archived(root, days=3)
        cfg = self._cfg(dest, reclaim_days_by_tool={"pi": 1.0})
        self.assertIsNone(sa.reclaim_unit(unit, rec, cfg, time.time(), manifest, dest, client))
        self.assertFalse(path.exists())


# --------------------------------------------------------------- restore

class RestoreTest(Base):
    def test_roundtrip_and_refusal(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        unit = self.unit("pi", root)
        dest = self.home / "dest"
        archive = sa.archive_path(dest, unit)
        archive.parent.mkdir(parents=True)
        records = sa.pack(unit, str(archive))
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = sa.count_member_rows(member, unit)
        manifest = sa.Manifest(dest / "state")
        manifest.write(
            {
                "unit": unit.uid,
                "tool": unit.tool,
                "key": unit.key,
                "session": unit.session,
                "parent": unit.parent,
                "archive": str(archive),
                "members": records,
                "status": "archived",
            }
        )
        target = self.home / "restored"
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), path.read_bytes())
        # A second restore refuses because the target exists.
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 2)


# --------------------------------------------------------------- scheduled

class ScheduledTest(Base):
    def test_skip_reasons(self):
        state = self.home / "state"
        state.mkdir()
        now = time.time()
        (state / "last_success.json").write_text(json.dumps({"ts": now - 3600}), encoding="utf-8")
        self.assertIn("last success", sa.scheduled_skip(state, now))
        (state / "last_success.json").unlink()
        with mock.patch.object(sa, "_memory_free_pct", lambda: 10):
            self.assertIn("memory free", sa.scheduled_skip(state, now))
        with mock.patch.object(sa, "_memory_free_pct", lambda: 80), mock.patch.object(sa, "_on_battery", lambda: True):
            self.assertIn("battery", sa.scheduled_skip(state, now))
        with mock.patch.object(sa, "_memory_free_pct", lambda: 80), mock.patch.object(sa, "_on_battery", lambda: False):
            self.assertIsNone(sa.scheduled_skip(state, now))

    def test_free_disk_probe_finds_the_volume(self):
        self.assertGreater(sa._free_disk_gb(self.home), 0)
        self.assertGreater(sa._free_disk_gb(self.home / "does-not-exist" / "yet"), 0)


# --------------------------------------------------------------- driver

class DriverTest(Base):
    def _fixture(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        age(path, 30)
        return root, path

    def _run(self, root, client, **opts):
        tools = {"pi": (str(root), sa.discover_pi)}
        base = {"apply": False, "no_reclaim": False, "scheduled": False, "tools": ["pi"], "limit": None,
                "max_minutes": None, "url": None, "dest": str(self.home / "dest")}
        base.update(opts)
        with mock.patch.dict(sa.TOOLS, tools), mock.patch.object(
            sa.vlogs, "VLogsClient", lambda url=None: client
        ), mock.patch.object(sa, "live_files", lambda names: []):
            return sa.run(base, out=open(os.devnull, "w"), state=self.home / "state")

    def test_dry_run_census_plans_actions_and_writes_nothing(self):
        root, _path = self._fixture()
        code = self._run(root, FakeClient())
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "dest").exists())

    def test_apply_archives_and_reclaims(self):
        root, path = self._fixture()
        client = FakeClient()
        code = self._run(root, client, apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(path.exists())
        manifest = sa.Manifest(self.home / "dest" / "state")
        rec = manifest.latest["pi/proj/a.jsonl"]
        self.assertEqual(rec["status"], "reclaimed")
        self.assertTrue(Path(rec["archive"]).exists())
        self.assertTrue((self.home / "dest" / ".metadata_never_index").exists())
        self.assertTrue((self.home / "dest" / "state" / "last_success.json").exists())

    def test_no_reclaim_keeps_original(self):
        root, path = self._fixture()
        code = self._run(root, FakeClient(), apply=True, no_reclaim=True)
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())

    def test_store_unreachable_returns_5(self):
        root, path = self._fixture()
        code = self._run(root, FakeClient(healthy=False), apply=True)
        self.assertEqual(code, 5)
        self.assertTrue(path.exists())

    def test_ingest_failure_keeps_original(self):
        root, path = self._fixture()
        code = self._run(root, BadCountClient(), apply=True)
        self.assertEqual(code, 1)
        self.assertTrue(path.exists())
        manifest = sa.Manifest(self.home / "dest" / "state")
        self.assertEqual(manifest.latest["pi/proj/a.jsonl"]["status"], "error")

    def test_budget_stop_leaves_last_success_unset(self):
        root, path = self._fixture()
        code = self._run(root, FakeClient(), apply=True, max_minutes=1e-9)
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())
        self.assertFalse((self.home / "dest" / "state" / "last_success.json").exists())

    def test_lock_held_returns_3(self):
        root, _path = self._fixture()
        state = self.home / "dest" / "state"
        state.mkdir(parents=True)
        lock = open(state / "archive.lock", "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code = self._run(root, FakeClient(), apply=True)
        finally:
            lock.close()
        self.assertEqual(code, 3)

    def test_scheduled_skips_before_work(self):
        root, path = self._fixture()
        state = self.home / "dest" / "state"
        state.mkdir(parents=True)
        (state / "last_success.json").write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
        code = self._run(root, FakeClient(), apply=True, scheduled=True)
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())

    def test_scheduled_low_disk_warns_but_still_archives(self):
        root, path = self._fixture()
        with mock.patch.object(sa, "_free_disk_gb", lambda _path: 5.0):
            code = self._run(root, FakeClient(), apply=True, scheduled=True)
        self.assertEqual(code, 0)
        self.assertFalse(path.exists())
        warn = self.home / "dest" / "state" / "disk_warning.json"
        self.assertTrue(warn.exists())
        self.assertIn("free_gb", json.loads(warn.read_text(encoding="utf-8")))
        import io

        buf = io.StringIO()
        self.assertEqual(sa.status(self.home / "dest" / "state", buf), 0)
        self.assertIn("dest free disk", buf.getvalue())

    def test_scheduled_plenty_of_disk_clears_warning(self):
        root, _path = self._fixture()
        state = self.home / "dest" / "state"
        state.mkdir(parents=True)
        (state / "disk_warning.json").write_text(json.dumps({"free_gb": 5.0}), encoding="utf-8")
        with mock.patch.object(sa, "_free_disk_gb", lambda _path: 500.0):
            code = self._run(root, FakeClient(), apply=True, scheduled=True)
        self.assertEqual(code, 0)
        self.assertFalse((state / "disk_warning.json").exists())

    def test_delete_guard_error_is_recorded_and_pass_continues(self):
        root = self.home / "pi"
        dest = root / "store"
        path = write(dest / "a.jsonl", '{"type":"session"}\n')
        age(path, 30)
        unit = self.unit("pi", root)
        records = sa.pack(unit, str(self.home / "a.tar.zst"))
        manifest = sa.Manifest(dest / "state")
        manifest.write({"unit": unit.uid, "tool": "pi", "key": unit.key, "status": "archived", "members": records})
        cfg = {
            "dest": str(dest),
            "reclaim_days": 1.0,
            "reclaim_days_by_tool": {},
            "min_idle_hours": 6.0,
            "max_minutes": 90.0,
        }
        results = sa.apply_pass(
            [unit],
            cfg,
            FakeClient(),
            manifest,
            dest,
            set(),
            open(os.devnull, "w"),
            no_reclaim=False,
            limit=None,
            max_minutes=90.0,
            now=time.time(),
        )
        self.assertEqual(results["errors"], 1)
        self.assertTrue(path.exists())
        rec = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertEqual(rec["status"], "error")
        self.assertIn("under the archive dest", rec["error"])


class StatusTest(Base):
    def test_status_counts_by_tool(self):
        state = self.home / "state"
        manifest = sa.Manifest(state)
        manifest.write({"unit": "pi/a", "tool": "pi", "status": "archived", "archive_bytes": 100, "raw_bytes": 200})
        manifest.write({"unit": "pi/b", "tool": "pi", "status": "reclaimed", "archive_bytes": 50, "raw_bytes": 80})
        manifest.write({"unit": "claude/c", "tool": "claude", "status": "error", "error": "boom"})
        import io

        buf = io.StringIO()
        self.assertEqual(sa.status(state, buf), 0)
        text = buf.getvalue()
        self.assertIn("pi", text)
        self.assertIn("claude", text)
        self.assertIn("boom", text)


# --------------------------------------------------------------- installers

class InstallerTest(unittest.TestCase):
    def test_shell_syntax(self):
        for name in ("vlogs_install.sh", "session_archive_install.sh"):
            res = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_session_archive_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = dict(os.environ)
            env.update(
                {
                    "HOME": str(home),
                    "SESSION_ARCHIVE_PLIST_DIR": str(home / "LaunchAgents"),
                    "SESSION_ARCHIVE_LOG_DIR": str(home / "Logs"),
                    "SESSION_ARCHIVE_NO_LOAD": "1",
                }
            )
            res = subprocess.run(
                ["sh", str(SCRIPTS / "session_archive_install.sh")],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            plist = home / "LaunchAgents" / "com.agent-dash.session-archive.plist"
            self.assertTrue(plist.exists())
            self.assertTrue((home / "Logs").is_dir())
            lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
            text = plist.read_text(encoding="utf-8")
            self.assertIn("<string>--scheduled</string>", text)
            self.assertIn("<integer>3600</integer>", text)
            self.assertIn("<key>LowPriorityIO</key>", text)
            self.assertIn("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", text)
            # The job runs the deployed release, never a symlink into
            # ~/Documents (a launchd job cannot read that under TCC).
            code = home / ".local" / "share" / "agent-dash" / "releases" / "CURRENT" / "tools" / "agent-dash"
            self.assertIn("<string>%s/scripts/memcap.sh</string>" % code, text)
            self.assertIn("<string>%s/agent_dash.py</string>" % code, text)
            self.assertNotIn(".claude/tools", text)
            self.assertNotIn(".claude/scripts", text)
            # Uninstall removes the plist.
            res = subprocess.run(
                ["sh", str(SCRIPTS / "session_archive_install.sh"), "--uninstall"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(plist.exists())

    def test_session_archive_code_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code = home / "release" / "tools" / "agent-dash"
            env = dict(os.environ)
            env.update(
                {
                    "HOME": str(home),
                    "SESSION_ARCHIVE_PLIST_DIR": str(home / "LaunchAgents"),
                    "SESSION_ARCHIVE_LOG_DIR": str(home / "Logs"),
                    "SESSION_ARCHIVE_NO_LOAD": "1",
                    "SESSION_ARCHIVE_CODE_DIR": str(code),
                }
            )
            res = subprocess.run(
                ["sh", str(SCRIPTS / "session_archive_install.sh")],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            text = (home / "LaunchAgents" / "com.agent-dash.session-archive.plist").read_text(
                encoding="utf-8"
            )
            self.assertIn("<string>%s/scripts/memcap.sh</string>" % code, text)
            self.assertIn("<string>%s/agent_dash.py</string>" % code, text)

    def _install(self, home, **env_extra):
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "SESSION_ARCHIVE_PLIST_DIR": str(home / "LaunchAgents"),
                "SESSION_ARCHIVE_LOG_DIR": str(home / "Logs"),
                "SESSION_ARCHIVE_NO_LOAD": "1",
            }
        )
        env.update(env_extra)
        return subprocess.run(
            ["sh", str(SCRIPTS / "session_archive_install.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_refuses_code_dir_under_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            res = self._install(home, SESSION_ARCHIVE_CODE_DIR=str(home / "Documents" / "code"))
            self.assertEqual(res.returncode, 2, res.stdout)
            self.assertIn("refusing", res.stderr)
            self.assertIn("Documents", res.stderr)
            self.assertFalse((home / "LaunchAgents" / "com.agent-dash.session-archive.plist").exists())

    def test_refuses_dest_in_every_tcc_folder(self):
        for folder in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                res = self._install(
                    home,
                    SESSION_ARCHIVE_CODE_DIR=str(home / "release"),
                    SESSION_ARCHIVE_DEST=str(home / folder / "agent-sessions"),
                )
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertIn("refusing", res.stderr)
                self.assertIn("archive dest", res.stderr)

    def test_refuses_code_dir_that_symlinks_into_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            real = home / "Documents" / "projects" / "workflow-skills" / "tools" / "agent-dash"
            real.mkdir(parents=True)
            link = home / ".claude" / "tools" / "agent-dash"
            link.parent.mkdir(parents=True)
            os.symlink(real, link)
            res = self._install(home, SESSION_ARCHIVE_CODE_DIR=str(link))
            self.assertEqual(res.returncode, 2, res.stdout)
            self.assertIn("refusing", res.stderr)

    def test_symlinked_dest_out_of_documents_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            real = home / ".local" / "share" / "agent-sessions"
            real.mkdir(parents=True)
            link = home / "Documents" / "data" / "agent-sessions"
            link.parent.mkdir(parents=True)
            os.symlink(real, link)
            res = self._install(
                home,
                SESSION_ARCHIVE_CODE_DIR=str(home / "release"),
                SESSION_ARCHIVE_DEST=str(link),
            )
            self.assertEqual(res.returncode, 0, res.stderr)


# --------------------------------------------------------------- review regressions

def make_tar_zst(path, entries):
    """Build a ``tar.zst`` from ``(name, data, kind)`` entries for restore tests."""
    import io
    import tarfile

    tar_path = str(path) + ".tar"
    with tarfile.open(tar_path, "w") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    zstd_compress(tar_path, path)
    os.remove(tar_path)


class ReviewRegressionTest(Base):
    """One test per finding in the independent review of d045281."""

    def _cfg(self, dest, **extra):
        cfg = {
            "reclaim_days": 14.0,
            "reclaim_days_by_tool": {},
            "min_idle_hours": 6.0,
            "max_minutes": 90.0,
            "dest": str(dest),
        }
        cfg.update(extra)
        return cfg

    def _grok_unit(self, days=20):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n')
        write(sdir / "blob.bin", bytes(range(256)) * 32)
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, days)
        return root, self.unit("grok", root)

    # -- finding 1: a crash mid-reclaim must never destroy the only archive
    def test_review_f1_crash_mid_reclaim_keeps_complete_archive(self):
        root, unit = self._grok_unit()
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        archive0 = Path(rec["archive"])
        real_rename = os.rename
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated crash after the first rename")
            return real_rename(src, dst)

        with mock.patch.object(sa.os, "rename", flaky):
            reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("rename failed", reason)
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)
        # The complete archive is still there and still verifies.
        sa.verify_archive(str(archive0), rec["members"])
        self.assertEqual(len(rec["members"]), 2)
        # A later pack gets a new sequence and never replaces archive 0.
        self.assertEqual(sa.next_archive_seq(dest, unit), 1)
        self.assertTrue(archive0.exists())

    # -- finding 2: reclaim must check the archive exists and verifies
    def test_review_f2_reclaim_refuses_missing_or_truncated_archive(self):
        root, unit = self._grok_unit()
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        archive = Path(rec["archive"])
        archive.write_bytes(b"truncated")
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("archive size changed", reason)
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)
        archive.unlink()
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("archive missing", reason)
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)

    # -- finding 3: a mutation between hashing and the move is not deleted
    def test_review_f3_mutation_after_hash_is_rolled_back(self):
        root, unit = self._grok_unit()
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        real_rename = os.rename
        calls = {"n": 0}
        changed = b"changed after the hash\n"

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                Path(src).write_bytes(changed)
            return real_rename(src, dst)

        with mock.patch.object(sa.os, "rename", flaky):
            reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIn("trash hash mismatch", reason)
        # The changed bytes are back at the original path, not silently gone.
        self.assertEqual(Path(unit.members[0].path).read_bytes(), changed)
        self.assertEqual(sa.Manifest(dest / "state").latest[unit.uid]["status"], "archived")

    # -- finding 4: codex .jsonl.zst line counts come from the decompressed stream
    def test_review_f4_codex_zst_line_count_is_decompressed(self):
        root = self.home / "codex"
        src = write(self.home / "src.jsonl", '{"type":"a"}\n{"type":"b"}\n')
        zname = "rollout-2026-11-03T11-27-46-01a1a23c-f580-7c42-a723-1b4f18c257f2.jsonl.zst"
        zstd_compress(src, root / "sessions" / "2026" / "11" / "03" / zname)
        unit = self.unit("codex", root)
        records = sa.pack(unit, str(self.home / "a.tar.zst"))
        self.assertEqual(records[0]["lines"], 2)
        client = FakeClient()
        rev, total, sent = sa.ingest_unit(unit, records, None, client)
        self.assertEqual((rev, total, sent), ("0", 2, 2))
        self.assertEqual(len(client.rows), 2)

    # -- finding 5: an error record keeps the confirmed members so retry resumes
    def test_review_f5_error_record_keeps_confirmed_members(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        age(path, 20)
        unit = self.unit("pi", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type":"message"}\n')
        age(path, 20)
        unit2 = self.unit("pi", root)

        class Flaky(FakeClient):
            def __init__(self, rows):
                super().__init__()
                self.rows = rows
                self.fail = 1
                self.inserted = False

            def insert(self, lines):
                sent = super().insert(lines)
                self.inserted = True
                return sent

            def query(self, query):
                # Fail the verification query after the POST, not the pre-check.
                if "count_uniq" in query and self.inserted and self.fail:
                    self.fail -= 1
                    return [{"n": "0"}]
                return super().query(query)

        flaky = Flaky(client.rows)
        out = open(os.devnull, "w")
        results = sa.apply_pass(
            [unit2], self._cfg(dest), flaky, manifest, dest, set(), out,
            no_reclaim=True, limit=None, max_minutes=90.0, now=time.time(),
        )
        self.assertEqual(results["errors"], 1)
        err = sa.Manifest(dest / "state").latest[unit2.uid]
        self.assertEqual(err["status"], "error")
        self.assertEqual(err["lines"], 2)
        self.assertTrue(err["members"][0]["sha256"])
        # Retry with a healthy store: at-least-once resend verifies.
        results2 = sa.apply_pass(
            [unit2], self._cfg(dest), client, sa.Manifest(dest / "state"), dest, set(), out,
            no_reclaim=True, limit=None, max_minutes=90.0, now=time.time(),
        )
        self.assertEqual(results2["errors"], 0)
        rec2 = sa.Manifest(dest / "state").latest[unit2.uid]
        self.assertEqual(rec2["status"], "archived")
        self.assertEqual(rec2["lines"], 3)
        self.assertEqual(sa.count_uniq_rows(client, unit2.uid, rec2["rev"]), 3)

    # -- finding 6: append growth works after a rewrite (old rev rows ignored)
    def test_review_f6_append_growth_after_rewrite(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        unit = self.unit("pi", root)
        client = FakeClient()
        records = sa.pack(unit, str(self.home / "a.tar.zst"))
        rev, total, _sent = sa.ingest_unit(unit, records, None, client)
        self.assertEqual((rev, total), ("0", 2))
        path.write_text('{"type":"other"}\n{"type":"message"}\n', encoding="utf-8")
        unit2 = self.unit("pi", root)
        records2 = sa.pack(unit2, str(self.home / "b.tar.zst"))
        prior = {"unit": unit.uid, "rev": rev, "members": records}
        rev2, total2, _sent2 = sa.ingest_unit(unit2, records2, prior, client)
        self.assertEqual((rev2, total2), ("1", 2))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type":"message"}\n')
        unit3 = self.unit("pi", root)
        records3 = sa.pack(unit3, str(self.home / "c.tar.zst"))
        prior2 = {"unit": unit.uid, "rev": rev2, "members": records2}
        rev3, total3, sent3 = sa.ingest_unit(unit3, records3, prior2, client)
        self.assertEqual((rev3, total3, sent3), ("1", 3, 1))
        self.assertEqual(sa.count_uniq_rows(client, unit.uid, "1"), 3)

    # -- finding 7: restore rejects traversal, symlinks and unlisted entries
    def test_review_f7_restore_rejects_traversal_and_symlinks(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        unit = self.unit("pi", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        archive = Path(rec["archive"])
        victim = write(self.home / "victim.txt", "original\n")
        make_tar_zst(
            archive,
            [
                ("a.jsonl", path.read_bytes(), "file"),
                ("../victim.txt", b"pwned\n", "file"),
            ],
        )
        target = self.home / "restored"
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 2)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((target / "a.jsonl").exists())
        # A symlink entry is refused too.
        make_tar_zst(archive, [("a.jsonl", b"x", "symlink")])
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 2)
        self.assertFalse((target / "a.jsonl").exists())

    # -- finding 8: a live-history resend after a crash is a harmless duplicate
    def test_review_f8_live_retry_duplicates_are_harmless(self):
        path = write(
            self.home / ".claude" / "history.jsonl",
            '{"sessionId":"s1","display":"a"}\n{"sessionId":"s1","display":"b"}\n',
        )
        manifest = sa.Manifest(self.home / "state")
        client = FakeClient()
        real_write = sa.Manifest.write
        calls = {"n": 0}

        def flaky(self_, rec):
            if rec.get("status") == "live" and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("crash after the POST, before the manifest write")
            return real_write(self_, rec)

        with mock.patch.object(sa.Manifest, "write", flaky), mock.patch.object(
            sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p
        ):
            with self.assertRaises(OSError):
                sa.ingest_live("claude", str(path), manifest, client)
        with mock.patch.object(
            sa.os.path, "expanduser", lambda p: str(self.home) if p == "~" else p
        ):
            sa.ingest_live("claude", str(path), manifest, client)
        uid = "live/claude/.claude/history.jsonl"
        self.assertEqual(sa.count_uniq_rows(client, uid, "0"), 2)
        self.assertGreaterEqual(len(client.rows), 2)
        self.assertEqual(manifest.latest[uid]["line"], 2)

    # -- finding 9: a long line is truncated and the buffer stays bounded
    def test_review_f9_long_line_is_bounded(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", b"x" * (4 << 20) + b"\n" + b'{"type":"a"}\n')
        unit = self.unit("pi", root)
        with mock.patch.object(sa, "MAX_LINE_BYTES", 1 << 20):
            lines = list(sa._iter_lines(str(path)))
            self.assertEqual(len(lines), 2)
            self.assertEqual(len(lines[0]), sa.vlogs.MAX_MSG_BYTES + 1)
            records = sa.pack(unit, str(self.home / "a.tar.zst"))
            client = FakeClient()
            sa.ingest_unit(unit, records, None, client)
        self.assertEqual(len(client.rows), 2)
        self.assertEqual(client.rows[0]["truncated"], "1")
        self.assertLessEqual(len(client.rows[0]["_msg"].encode("utf-8")), sa.vlogs.MAX_MSG_BYTES)
        self.assertNotIn("truncated", client.rows[1])

    @unittest.skipUnless(
        os.environ.get("SESSION_ARCHIVE_BIG") == "1",
        "set SESSION_ARCHIVE_BIG=1 for the 200 MiB single-line RSS run",
    )
    def test_review_f9_200mib_line_bounded(self):
        script = r"""
import os, resource, sys, tempfile
sys.path.insert(0, %r)
from dash import sessionarchive as sa
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "long.jsonl")
    with open(path, "wb") as fh:
        chunk = b"x" * (1 << 20)
        for _ in range(200):
            fh.write(chunk)
        fh.write(b"\n")
    n = 0
    for line in sa._iter_lines(path):
        n += 1
        assert len(line) <= sa.vlogs.MAX_MSG_BYTES + 1, len(line)
    assert n == 1, n
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
""" % str(ROOT)
        res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=900)
        self.assertEqual(res.returncode, 0, res.stderr)
        maxrss = int(res.stdout.strip().splitlines()[-1])
        # macOS reports ru_maxrss in bytes, Linux in KiB.
        maxrss_kb = maxrss // 1024 if sys.platform == "darwin" else maxrss
        self.assertLess(maxrss_kb, 500 * 1024, "peak RSS %d KB" % maxrss_kb)

    # -- finding 10: reclaim never touches an excluded claude memory/ tree
    def test_review_f10_reclaim_keeps_claude_memory_tree(self):
        root = self.home / "claude"
        proj = root / "-Users-me-proj"
        write(proj / "abc.jsonl", '{"type":"user"}\n')
        write(proj / "abc" / "subagents" / "a.jsonl", '{"type":"assistant"}\n')
        (proj / "abc" / "memory" / "empty" / "nested").mkdir(parents=True)
        for path in proj.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("claude", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        reason = sa.reclaim_unit(unit, rec, self._cfg(dest), time.time(), manifest, dest, client)
        self.assertIsNone(reason)
        self.assertFalse((proj / "abc.jsonl").exists())
        self.assertFalse((proj / "abc" / "subagents").exists())
        self.assertTrue((proj / "abc" / "memory" / "empty" / "nested").is_dir())
        self.assertTrue((proj / "abc").is_dir())

    # -- finding 11: codex discovery is exactly the specified layout
    def test_review_f11_codex_discovery_is_exact(self):
        root = self.home / "codex"
        day = root / "sessions" / "2026" / "11" / "03"
        write(day / "rollout-2026-11-03T00-00-00-01a1a2d8-ef9e-7771-a442-27b79500e9aa.jsonl", "{}\n")
        src = write(self.home / "src.jsonl", "{}\n")
        zstd_compress(src, day / "rollout-2026-11-03T00-00-01-01a1a23c-f580-7c42-a723-1b4f18c257f2.jsonl.zst")
        zstd_compress(src, root / "archived_sessions" / "rollout-2026-10-23T00-00-00-01a168da-daeb-7113-a1ac-5f5a8c2e3e3a.jsonl.zst")
        # Not units: nested, uncompressed archived, and a non-rollout name.
        write(root / "archived_sessions" / "nested" / "rollout-2026-10-24T00-00-00-01a168da-daeb-7113-a1ac-5f5a8c2e3e3b.jsonl", "{}\n")
        write(root / "archived_sessions" / "rollout-2026-10-25T00-00-00-01a168da-daeb-7113-a1ac-5f5a8c2e3e3c.jsonl", "{}\n")
        write(day / "notes.jsonl", "{}\n")
        units = sa.TOOLS["codex"][1](str(root))
        self.assertEqual(len(units), 3)
        keys = sorted(u.key for u in units)
        self.assertEqual(
            keys,
            [
                "archived_sessions/rollout-2026-10-23T00-00-00-01a168da-daeb-7113-a1ac-5f5a8c2e3e3a.jsonl.zst",
                "sessions/2026/11/03/rollout-2026-11-03T00-00-00-01a1a2d8-ef9e-7771-a442-27b79500e9aa.jsonl",
                "sessions/2026/11/03/rollout-2026-11-03T00-00-01-01a1a23c-f580-7c42-a723-1b4f18c257f2.jsonl.zst",
            ],
        )


class ReclaimCrashTest(Base):
    """Crash injection at every reclaim step: never neither."""

    def _setup(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n')
        write(sdir / "blob.bin", bytes(range(256)) * 32)
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}, "dest": str(dest)}
        return unit, dest, client, rec, manifest, cfg

    def _crash(self, point):
        unit, dest, client, rec, manifest, cfg = self._setup()
        real_rename = os.rename
        real_write = sa.Manifest.write
        real_mismatch = sa._trash_mismatch
        real_rmdir_empty = sa._rmdir_empty
        state = {"renames": 0}

        def flaky_rename(src, dst):
            state["renames"] += 1
            if point == "rename1" and state["renames"] == 1:
                raise OSError("crash")
            if point == "rename2" and state["renames"] == 2:
                raise OSError("crash")
            return real_rename(src, dst)

        def flaky_write(self_, r):
            real_write(self_, r)
            if point == "journal" and r.get("status") == "reclaiming":
                raise OSError("crash")

        def flaky_mismatch(trash, members):
            result = real_mismatch(trash, members)
            if point == "rehash":
                raise OSError("crash")
            return result

        def flaky_rmdir_empty(path):
            real_rmdir_empty(path)
            if point == "trashdelete" and str(path).startswith(str(dest / "state" / "trash")):
                raise OSError("crash")

        with mock.patch.object(sa.os, "rename", flaky_rename), mock.patch.object(
            sa.Manifest, "write", flaky_write
        ), mock.patch.object(sa, "_trash_mismatch", flaky_mismatch), mock.patch.object(
            sa, "_rmdir_empty", flaky_rmdir_empty
        ):
            try:
                sa.reclaim_unit(unit, rec, cfg, time.time(), manifest, dest, client)
            except OSError:
                pass

        sa.recover_trash(dest, sa.Manifest(dest / "state"), open(os.devnull, "w"))
        final = sa.Manifest(dest / "state").latest[unit.uid]
        originals = [Path(m.path).exists() for m in unit.members]
        self.assertTrue(
            sa._archive_ok(final),
            "archive must verify after %s: %s" % (point, _archive_diag(final)),
        )
        if final["status"] == "reclaimed":
            self.assertFalse(any(originals), "reclaimed but originals remain after %s" % point)
        else:
            self.assertTrue(all(originals), "not reclaimed but originals missing after %s" % point)
        # Never neither: either the originals are in place or the archive is.
        self.assertTrue(any(originals) or final["status"] == "reclaimed")

    def test_crash_after_reclaiming_write(self):
        self._crash("journal")

    def test_crash_after_first_rename(self):
        self._crash("rename1")

    def test_crash_after_second_rename(self):
        self._crash("rename2")

    def test_crash_after_rehash(self):
        self._crash("rehash")

    def test_crash_after_trash_delete(self):
        self._crash("trashdelete")


# --------------------------------------------------------------- round 4

class ApplyPassRecoveryTest(Base):
    """Round 4 finding 1: a reclaim crash through apply_pass keeps its journal.

    The review of e6e145d showed that an exception raised after the
    ``reclaiming`` journal is written made ``apply_pass`` record an error
    from the stale prior record, dropping ``trash_members`` and stranding
    the originals in trash forever.
    """

    def _setup(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n')
        write(sdir / "blob.bin", bytes(range(256)) * 32)
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec, manifest = self.archive_unit(unit, dest, client)
        cfg = {
            "reclaim_days": 14.0,
            "reclaim_days_by_tool": {},
            "min_idle_hours": 6.0,
            "max_minutes": 90.0,
            "dest": str(dest),
        }
        return unit, dest, client, manifest, cfg

    def _apply_crash(self, point):
        """Run apply_pass and crash at one reclaim step (journal/rename/...)."""
        unit, dest, client, manifest, cfg = self._setup()
        real_rename = os.rename
        real_write = sa.Manifest.write
        real_mismatch = sa._trash_mismatch
        real_rmdir_empty = sa._rmdir_empty
        state = {"renames": 0}

        def flaky_rename(src, dst):
            state["renames"] += 1
            if point == "rename1" and state["renames"] == 1:
                raise RuntimeError("simulated crash")
            if point == "rename2" and state["renames"] == 2:
                raise RuntimeError("simulated crash")
            return real_rename(src, dst)

        def flaky_write(self_, r):
            real_write(self_, r)
            if point == "journal" and r.get("status") == "reclaiming":
                raise OSError("simulated crash")

        def flaky_mismatch(trash, members):
            result = real_mismatch(trash, members)
            if point == "rehash":
                raise OSError("simulated crash")
            return result

        def flaky_rmdir_empty(path):
            real_rmdir_empty(path)
            if point == "trashdelete" and str(path).startswith(str(dest / "state" / "trash")):
                raise OSError("simulated crash")

        with mock.patch.object(sa.os, "rename", flaky_rename), mock.patch.object(
            sa.Manifest, "write", flaky_write
        ), mock.patch.object(sa, "_trash_mismatch", flaky_mismatch), mock.patch.object(
            sa, "_rmdir_empty", flaky_rmdir_empty
        ):
            results = sa.apply_pass(
                [unit],
                cfg,
                client,
                manifest,
                dest,
                set(),
                open(os.devnull, "w"),
                no_reclaim=False,
                limit=None,
                max_minutes=90.0,
                now=time.time(),
            )
        self.assertEqual(results["errors"], 1, point)
        return unit, dest

    def _recover(self, unit, dest):
        sa.recover_trash(dest, sa.Manifest(dest / "state"), open(os.devnull, "w"))
        return sa.Manifest(dest / "state").latest[unit.uid]

    def test_crash_after_journal_write_keeps_trash_members(self):
        unit, dest = self._apply_crash("journal")
        crashed = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertEqual(crashed["status"], "error")
        self.assertTrue(crashed.get("trash_members"), "error record dropped the reclaim journal")
        self.assertTrue(crashed.get("trash"))
        final = self._recover(unit, dest)
        self.assertEqual(final["status"], "archived")
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)
        self.assertTrue(sa._archive_ok(final))

    def test_crash_before_first_rename_recovers_through_apply_pass(self):
        unit, dest = self._apply_crash("rename1")
        final = self._recover(unit, dest)
        self.assertEqual(final["status"], "archived")
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)
        self.assertTrue(sa._archive_ok(final))

    def test_crash_mid_rename_recovers_through_apply_pass(self):
        unit, dest = self._apply_crash("rename2")
        final = self._recover(unit, dest)
        self.assertEqual(final["status"], "archived")
        for member in unit.members:
            self.assertTrue(Path(member.path).exists(), member.rel)
        self.assertTrue(sa._archive_ok(final))

    def test_crash_after_rehash_recovers_through_apply_pass(self):
        unit, dest = self._apply_crash("rehash")
        final = self._recover(unit, dest)
        self.assertEqual(final["status"], "reclaimed")
        for member in unit.members:
            self.assertFalse(Path(member.path).exists(), member.rel)
        self.assertTrue(sa._archive_ok(final))

    def test_crash_after_trash_delete_recovers_through_apply_pass(self):
        unit, dest = self._apply_crash("trashdelete")
        final = self._recover(unit, dest)
        self.assertEqual(final["status"], "reclaimed")
        self.assertFalse(any(Path(m.path).exists() for m in unit.members))
        self.assertTrue(sa._archive_ok(final))

    def test_error_record_without_trash_members_is_left_and_reported(self):
        """An e6e145d-style record without trash_members is never guessed at."""
        unit, dest, _client, manifest, _cfg = self._setup()
        rec = manifest.latest[unit.uid]
        trash = sa._trash_dir(dest, unit.uid)
        for member in unit.members:
            dst = trash / member.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(member.path, dst)
        # The pre-fix apply_pass wrote the error record from the stale prior,
        # so it carries neither trash nor trash_members.
        manifest.write(dict(rec, status="error", error="simulated stale record"))
        crashed = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertFalse(crashed.get("trash_members"))
        out = io.StringIO()
        sa.recover_trash(dest, sa.Manifest(dest / "state"), out)
        final = sa.Manifest(dest / "state").latest[unit.uid]
        # Without trash_members the trash contents cannot be proven, so
        # nothing is deleted and the unit is not marked reclaimed.
        self.assertEqual(final["status"], "error")
        for member in unit.members:
            self.assertTrue((trash / member.rel).exists(), member.rel)
        self.assertIn("no reclaim journal", out.getvalue())

    def test_unmatched_trash_dir_is_left_and_reported(self):
        unit, dest, _client, _manifest, _cfg = self._setup()
        orphan = sa._trash_root(dest) / "unknown%2Funit"
        write(orphan / "keep.txt", "not ours\n")
        sa.recover_trash(dest, sa.Manifest(dest / "state"), open(os.devnull, "w"))
        self.assertEqual((orphan / "keep.txt").read_text(encoding="utf-8"), "not ours\n")
        out = io.StringIO()
        self.assertEqual(sa.status(dest / "state", out), 0)
        self.assertIn("trash without a manifest record", out.getvalue())
        self.assertIn(str(orphan), out.getvalue())
        sa.recover_trash(dest, sa.Manifest(dest / "state"), open(os.devnull, "w"))
        self.assertTrue((orphan / "keep.txt").exists())


class RestoreRound4Test(Base):
    """Round 4 findings 2 and 3: symlinked ancestors and archive selection."""

    def test_restore_refuses_symlinked_ancestor(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "sub" / "a.jsonl", '{"type":"system"}\n')
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        _rec, _manifest = self.archive_unit(unit, dest, FakeClient())
        outside = self.home / "outside"
        outside.mkdir()
        target = self.home / "restored"
        target.mkdir()
        os.symlink(outside, target / "sid")
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 2)
        self.assertEqual(list(outside.rglob("*")), [], "restore wrote outside the target")
        self.assertTrue((target / "sid").is_symlink())

    def _pack(self, unit, dest, client, prior=None):
        seq = sa.next_archive_seq(dest, unit)
        archive = sa.archive_path(dest, unit, seq)
        archive.parent.mkdir(parents=True, exist_ok=True)
        records = sa.pack(unit, str(archive))
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = sa.count_member_rows(member, unit)
        sa.verify_archive(str(archive), records)
        rev, total, _sent = sa.ingest_unit(unit, records, prior, client)
        rec = {
            "unit": unit.uid,
            "tool": unit.tool,
            "key": unit.key,
            "session": unit.session,
            "root": unit.root,
            "parent": unit.parent,
            "archive": str(archive),
            "archive_bytes": os.path.getsize(archive),
            "seq": seq,
            "raw_bytes": unit.raw_bytes,
            "rev": rev,
            "lines": total,
            "members": records,
            "status": "archived",
            "archives": list((prior or {}).get("archives") or [])
            + [{"seq": seq, "path": str(archive), "bytes": os.path.getsize(archive), "members": records}],
        }
        sa.Manifest(dest / "state").write(rec)
        return rec, archive

    def _versions(self):
        """Two archive sequences for one unit: seq 0 ``old``, seq 1 ``new``."""
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", "old\n")
        age(path, 20)
        unit = self.unit("pi", root)
        dest = self.home / "dest"
        client = FakeClient()
        rec0, archive0 = self._pack(unit, dest, client)
        path.write_text("new\n", encoding="utf-8")
        age(path, 20)
        unit2 = self.unit("pi", root)
        rec1, archive1 = self._pack(unit2, dest, client, prior=rec0)
        return unit2, dest, rec0, archive0, rec1, archive1

    def test_explicit_older_archive_restores_that_seq(self):
        _unit, dest, _rec0, archive0, _rec1, _archive1 = self._versions()
        target = self.home / "r0"
        self.assertEqual(sa.restore(str(archive0), str(target), dest / "state", sys.stdout), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), b"old\n")

    def test_restore_by_session_uses_newest_verifying_seq(self):
        unit, dest, _rec0, _archive0, _rec1, _archive1 = self._versions()
        target = self.home / "new"
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", sys.stdout), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), b"new\n")

    def test_restore_by_session_falls_back_to_older_intact_seq(self):
        unit, dest, _rec0, _archive0, _rec1, archive1 = self._versions()
        Path(archive1).write_bytes(b"corrupt")
        out = io.StringIO()
        target = self.home / "fallback"
        self.assertEqual(sa.restore(unit.session, str(target), dest / "state", out), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), b"old\n")
        self.assertIn("restoring seq 0", out.getvalue())
        # An explicit corrupt path never silently substitutes another seq.
        self.assertEqual(
            sa.restore(str(archive1), str(self.home / "bad"), dest / "state", sys.stdout), 2
        )
        self.assertFalse((self.home / "bad").exists())


# --------------------------------------------------------------- round 5

class Round5RecoveryTest(Base):
    """Round 5: recovery acts only on a provable, canonical trash dir."""

    def _setup(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "chat_history.jsonl", '{"type":"system"}\n')
        write(sdir / "blob.bin", bytes(range(256)) * 32)
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        rec, manifest = self.archive_unit(unit, dest, FakeClient())
        return unit, dest, rec, manifest

    def _journal(self, unit, dest, manifest, rec, trash, status="reclaiming"):
        """Move the originals into ``trash`` and write a reclaim journal."""
        by_rel = {m["rel"]: m for m in rec["members"]}
        members = []
        for member in unit.members:
            dst = trash / member.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(member.path, dst)
            members.append(
                {
                    "rel": member.rel,
                    "path": member.path,
                    "sha256": by_rel[member.rel]["sha256"],
                    "size": by_rel[member.rel]["size"],
                }
            )
        manifest.write(dict(rec, status=status, trash=str(trash), trash_members=members))
        return members

    def test_unlisted_file_in_a_matched_trash_dir_survives(self):
        unit, dest, rec, manifest = self._setup()
        trash = sa._trash_dir(dest, unit.uid)
        self._journal(unit, dest, manifest, rec, trash)
        unlisted = trash / "sid" / "unlisted-only-copy.txt"
        write(unlisted, "only here\n")
        out = io.StringIO()
        sa.recover_trash(dest, sa.Manifest(dest / "state"), out)
        final = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertNotEqual(final["status"], "reclaimed")
        self.assertEqual(unlisted.read_text(encoding="utf-8"), "only here\n")
        for member in unit.members:
            self.assertTrue((trash / member.rel).exists(), member.rel)
        self.assertIn("not in the manifest record", out.getvalue())
        status_out = io.StringIO()
        sa.status(dest / "state", status_out)
        self.assertIn("trash left in place", status_out.getvalue())
        self.assertIn(str(trash), status_out.getvalue())
        self.assertIn("unlisted-only-copy.txt", status_out.getvalue())

    def test_unmatched_empty_trash_dir_survives(self):
        _unit, dest, _rec, _manifest = self._setup()
        orphan = sa._trash_root(dest) / "unknown%2Funit"
        orphan.mkdir(parents=True)
        out = io.StringIO()
        sa.recover_trash(dest, sa.Manifest(dest / "state"), out)
        self.assertTrue(orphan.is_dir(), "an unmatched empty trash dir was deleted")
        self.assertIn("trash without a manifest record", out.getvalue())
        status_out = io.StringIO()
        sa.status(dest / "state", status_out)
        self.assertIn("trash without a manifest record", status_out.getvalue())
        self.assertIn(str(orphan), status_out.getvalue())

    def test_non_canonical_trash_dir_is_left_and_not_reclaimed(self):
        unit, dest, rec, manifest = self._setup()
        custom = sa._trash_root(dest) / "legacy-custom-name"
        self._journal(unit, dest, manifest, rec, custom, status="error")
        out = io.StringIO()
        sa.recover_trash(dest, sa.Manifest(dest / "state"), out)
        final = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertNotEqual(final["status"], "reclaimed")
        for member in unit.members:
            self.assertTrue((custom / member.rel).exists(), member.rel)
        self.assertIn("non-canonical", out.getvalue())
        status_out = io.StringIO()
        sa.status(dest / "state", status_out)
        self.assertIn(str(custom), status_out.getvalue())


class Round5RestoreTest(Base):
    """Round 5: restore is fd-relative, so a late symlink is never followed."""

    def test_restore_does_not_follow_a_symlink_swapped_in_after_open(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "sub" / "a.jsonl", '{"type":"system"}\n')
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        self.archive_unit(unit, dest, FakeClient())
        outside = self.home / "outside"
        outside.mkdir()
        target = self.home / "restored"
        target.mkdir()
        real_rename = os.rename
        state = {"swapped": False}

        def swapping_rename(src, dst, *args, **kwargs):
            if not state["swapped"]:
                state["swapped"] = True
                # Swap the already-opened component for a symlink to outside
                # between the dir open and the rename.
                (outside / "sub").mkdir(parents=True, exist_ok=True)
                real_rename(str(target / "sid"), str(target / "sid-real"))
                os.symlink(outside, target / "sid")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(sa.os, "rename", swapping_rename):
            rc = sa.restore(unit.session, str(target), dest / "state", sys.stdout)
        self.assertEqual(rc, 0)
        self.assertEqual([p for p in outside.rglob("*") if p.is_file()], [])
        self.assertEqual(
            (target / "sid-real" / "sub" / "a.jsonl").read_bytes(), b'{"type":"system"}\n'
        )


class Round6CursorStoreTest(Base):
    """Round 6: a WAL-mode Cursor store opens from the private temp copy.

    Every Cursor ``store.db`` is WAL mode. SQLite 3.54 (the system python the
    launchd job runs) refuses a read-only open of a WAL db with no ``-shm``
    (``unable to open database file``); 3.53.1 allowed it. The copy is opened
    read-write so SQLite can create the ``-shm`` and replay a copied ``-wal``.
    """

    def _wal_store(self, cdir, rows=2, checkpoint=True):
        """A WAL-mode store.db in ``cdir``; returns the writer connection."""
        cdir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(cdir / "store.db"))
        con.execute("pragma journal_mode=wal")
        con.execute("pragma wal_autocheckpoint=0")
        con.execute("create table meta (key text primary key, value text)")
        con.execute("create table blobs (id text primary key, data blob)")
        for i in range(rows):
            con.execute(
                "insert into meta values (?, ?)",
                (str(i), json.dumps({"agentId": "chat1", "n": i}).encode().hex()),
            )
            con.execute("insert into blobs values (?, ?)", ("b%d" % i, b'{"role":"user"}'))
        con.commit()
        if checkpoint:
            con.execute("pragma wal_checkpoint(truncate)")
        return con

    def _rows(self, root):
        unit = self.unit("cursor", root)
        member = [m for m in unit.members if m.rows == "cursor-store"][0]
        return unit, member, list(sa.iter_member_rows(member, unit))

    def test_wal_store_copied_without_wal_or_shm(self):
        root = self.home / "cursor"
        cdir = root / "chats" / "ws1" / "chat1"
        con = self._wal_store(cdir)
        con.close()
        # Copy the db alone: no -wal, no -shm.
        for suffix in ("-wal", "-shm"):
            if (cdir / ("store.db" + suffix)).exists():
                (cdir / ("store.db" + suffix)).unlink()
        self.assertEqual((cdir / "store.db").read_bytes()[18:20], b"\x02\x02", "not WAL mode")
        self.assertFalse((cdir / "store.db-wal").exists())
        self.assertFalse((cdir / "store.db-shm").exists())
        _unit, _member, rows = self._rows(root)
        self.assertEqual(len(rows), 4, rows)
        self.assertEqual([r[1] for r in rows], ["raw", "raw", "user", "user"])
        # The original is never opened: no shm appears beside it.
        self.assertFalse((cdir / "store.db-shm").exists())

    def test_wal_store_rows_still_only_in_wal(self):
        root = self.home / "cursor"
        cdir = root / "chats" / "ws1" / "chat1"
        staging = self.home / "staging"
        con = self._wal_store(staging, rows=3, checkpoint=False)
        # Copy db + wal + shm while the writer is still open, as a live Cursor
        # process would leave them.
        cdir.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(str(staging / ("store.db" + suffix)), str(cdir / ("store.db" + suffix)))
        con.close()
        self.assertGreater((cdir / "store.db-wal").stat().st_size, 0)
        # The rows really are only in the WAL: the db alone has none.
        alone = self.home / "alone"
        alone.mkdir()
        shutil.copy2(str(cdir / "store.db"), str(alone / "store.db"))
        bare = sqlite3.connect(str(alone / "store.db"))
        try:
            try:
                n = bare.execute("select count(*) from meta").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0  # the schema itself is still only in the WAL
            self.assertEqual(n, 0)
        finally:
            bare.close()
        _unit, _member, rows = self._rows(root)
        self.assertEqual(len(rows), 6, rows)
        self.assertEqual(sum(1 for r in rows if r[1] == "user"), 3)


# --------------------------------------------------------------- round 7

class Round7RestoreStagingTest(Base):
    """Round 7: restore stages outside the target, in a private tree."""

    def _archived(self):
        root = self.home / "grok"
        sdir = root / "enc" / "sid"
        write(sdir / "sub" / "a.jsonl", '{"type":"system"}\n')
        for path in sdir.rglob("*"):
            if path.is_file():
                age(path, 20)
        unit = self.unit("grok", root)
        dest = self.home / "dest"
        self.archive_unit(unit, dest, FakeClient())
        return unit, dest

    def test_staging_symlink_swap_writes_and_deletes_nothing_outside(self):
        """The reviewer's repro: swap the staging pathname at tarfile.open."""
        unit, dest = self._archived()
        outside = self.home / "outside"
        outside.mkdir()
        sentinel = outside / "unarchived-only-copy.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")
        target = self.home / "restored"
        target.mkdir()
        base = dest / "state" / "restore-tmp"
        real_open = sa.tarfile.open
        state = {"swapped": False}

        def staging_dirs():
            """The staging dir, wherever this build puts it."""
            for parent in (base, target):
                if not parent.is_dir():
                    continue
                found = [
                    parent / n
                    for n in os.listdir(str(parent))
                    if n.startswith("restore-") or n.startswith(".restore-")
                ]
                if found:
                    return found
            return []

        def swapping_open(*args, **kwargs):
            if not state["swapped"]:
                found = staging_dirs()
                if found:
                    state["swapped"] = True
                    self.assertEqual(len(found), 1, found)
                    staging = found[0]
                    os.rename(str(staging), str(staging) + "-real")
                    os.symlink(str(outside), str(staging))
            return real_open(*args, **kwargs)

        err = io.StringIO()
        with mock.patch.object(sa.tarfile, "open", swapping_open):
            with mock.patch.object(sys, "stderr", err):
                rc = sa.restore(unit.session, str(target), dest / "state", sys.stdout)
        self.assertTrue(state["swapped"], "the repro never reached tarfile.open")
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(
            sorted(p.name for p in outside.iterdir()), ["unarchived-only-copy.txt"]
        )
        self.assertEqual(
            (target / "sid" / "sub" / "a.jsonl").read_bytes(), b'{"type":"system"}\n'
        )
        self.assertIn("was replaced; leaving it in place", err.getvalue())

    def test_cross_device_staging_is_refused(self):
        unit, dest = self._archived()
        target = self.home / "restored"
        err = io.StringIO()
        with mock.patch.object(sa, "_stage_device", lambda path: -1):
            with mock.patch.object(sys, "stderr", err):
                rc = sa.restore(unit.session, str(target), dest / "state", sys.stdout)
        self.assertEqual(rc, 2)
        self.assertIn("same volume", err.getvalue())
        self.assertEqual(list(target.rglob("*")), [], "target was written on refusal")
        # The private staging dir is discarded on refusal.
        self.assertEqual(list((dest / "state" / "restore-tmp").iterdir()), [])

    def test_discard_tree_refuses_an_arbitrary_path(self):
        victim = self.home / "victim"
        write(victim / "keep.txt", "not ours\n")
        with self.assertRaises(sa.ArchiveError):
            sa._discard_tree(str(victim))
        self.assertEqual((victim / "keep.txt").read_text(encoding="utf-8"), "not ours\n")
        # A private tree this process made is still discarded.
        private = sa._private_mkdtemp("round7-")
        write(Path(private) / "junk.txt", "x\n")
        sa._discard_tree(private)
        self.assertFalse(os.path.exists(private))


# --------------------------------------------------------------- real store e2e

VLOGS_BIN = Path.home() / ".local/share/agent-dash/vlogs/bin/victoria-logs-prod"
VLOGS_E2E_PORT = 19537


@unittest.skipUnless(os.environ.get("VLOGS_E2E") == "1", "set VLOGS_E2E=1 for the real-binary run")
class VLogsEndToEndTest(Base):
    """Archive -> ingest -> crash -> rerun -> verify -> reclaim -> restore."""

    def setUp(self):
        super().setUp()
        if not VLOGS_BIN.exists():
            self.skipTest("victoria-logs binary not installed")
        import urllib.request

        url = "http://127.0.0.1:%d" % VLOGS_E2E_PORT
        try:
            urllib.request.urlopen(url + "/health", timeout=1).read()
            self.skipTest("port %d already in use" % VLOGS_E2E_PORT)
        except Exception:
            pass
        data = self.home / "vlogs-data"
        data.mkdir()
        self.proc = subprocess.Popen(
            [
                str(VLOGS_BIN),
                "-storageDataPath=%s" % data,
                "-httpListenAddr=127.0.0.1:%d" % VLOGS_E2E_PORT,
                "-retentionPeriod=100y",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url + "/health", timeout=1).read()
                return
            except Exception:
                time.sleep(0.3)
        self.fail("throwaway VictoriaLogs did not become healthy")

    def _stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def _run(self, root, dest, **opts):
        base = {
            "apply": True,
            "no_reclaim": False,
            "scheduled": False,
            "tools": ["pi"],
            "limit": None,
            "max_minutes": None,
            "url": "http://127.0.0.1:%d" % VLOGS_E2E_PORT,
            "dest": str(dest),
        }
        base.update(opts)
        with mock.patch.dict(sa.TOOLS, {"pi": (str(root), sa.discover_pi)}), mock.patch.object(
            sa, "live_files", lambda names: []
        ):
            return sa.run(base, out=open(os.devnull, "w"), state=self.home / "state")

    def test_archive_ingest_crash_rerun_verify_reclaim_restore(self):
        from dash import vlogs

        root = self.home / "pi"
        path = write(
            root / "proj" / "a.jsonl",
            '{"type":"session","timestamp":"2026-11-01T00:00:00.000Z"}\n'
            '{"type":"message","timestamp":"2026-11-01T00:00:01.000Z"}\n'
            '{"type":"message","timestamp":"2026-11-01T00:00:02.000Z"}\n',
        )
        original = path.read_bytes()
        age(path, 20)
        dest = self.home / "dest"
        client = vlogs.VLogsClient("http://127.0.0.1:%d" % VLOGS_E2E_PORT)
        uid = "pi/proj/a.jsonl"

        # Crash after the POST but before the archived record is written.
        real_write = sa.Manifest.write
        crashed = {"done": False}

        def flaky(self_, rec):
            if rec.get("status") == "archived" and not crashed["done"]:
                crashed["done"] = True
                raise OSError("simulated crash after ingest")
            return real_write(self_, rec)

        with mock.patch.object(sa.Manifest, "write", flaky):
            self.assertEqual(self._run(root, dest), 1)
        self.assertTrue(path.exists())
        self.assertEqual(sa.count_uniq_rows(client, uid, "0"), 3)

        # Rerun: at-least-once resend verifies, then reclaim.
        self.assertEqual(self._run(root, dest), 0)
        self.assertFalse(path.exists())
        rec = sa.Manifest(dest / "state").latest[uid]
        self.assertEqual(rec["status"], "reclaimed")
        self.assertEqual(sa.count_uniq_rows(client, uid, rec["rev"]), 3)
        self.assertTrue(sa._archive_ok(rec))

        # Restore is byte-identical.
        target = self.home / "restored"
        self.assertEqual(sa.restore(uid, str(target), dest / "state", sys.stdout), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), original)

    def test_crash_mid_reclaim_recovers_with_the_real_store(self):
        from dash import vlogs

        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        age(path, 20)
        dest = self.home / "dest"
        client = vlogs.VLogsClient("http://127.0.0.1:%d" % VLOGS_E2E_PORT)
        uid = "pi/proj/a.jsonl"
        self.assertEqual(self._run(root, dest, no_reclaim=True), 0)
        self.assertTrue(path.exists())
        rec = sa.Manifest(dest / "state").latest[uid]
        self.assertEqual(rec["status"], "archived")

        real_rename = os.rename
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated crash during reclaim")
            return real_rename(src, dst)

        with mock.patch.object(sa.os, "rename", flaky):
            self.assertEqual(self._run(root, dest), 0)
        # The failed move rolled back: the original is still in place.
        self.assertTrue(path.exists())
        self.assertEqual(sa.Manifest(dest / "state").latest[uid]["status"], "archived")
        # The next pass reclaims it.
        self.assertEqual(self._run(root, dest), 0)
        final = sa.Manifest(dest / "state").latest[uid]
        self.assertEqual(final["status"], "reclaimed")
        self.assertFalse(path.exists())
        self.assertTrue(sa._archive_ok(final))
        self.assertEqual(sa.count_uniq_rows(client, uid, final["rev"]), 2)


# --------------------------------------------------------------- dest move

class DestMoveTest(Base):
    """Records keep working when the archive dest moves.

    New records carry ``archive_rel``/``rel``; older ones carry only an
    absolute path, which is re-anchored at read time when it is gone.
    """

    def _unit(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n{"type":"message"}\n')
        age(path, 30)
        return root, path, self.unit("pi", root)

    def test_legacy_absolute_record_follows_the_move(self):
        root, _path, unit = self._unit()
        old = self.home / "old"
        rec, _manifest = self.archive_unit(unit, old, FakeClient())
        # Exactly the old shape: no relative fields at all.
        self.assertNotIn("archive_rel", rec)
        self.assertNotIn("rel", rec["archives"][0])
        self.assertTrue(rec["archive"].startswith(str(old) + os.sep))

        new = self.home / "new"
        os.rename(old, new)
        moved = sa.Manifest(new / "state")
        rec = moved.latest[unit.uid]

        entries = sa._seq_entries(rec, new)
        self.assertEqual(len(entries), 1)
        archive = entries[0]["path"]
        self.assertTrue(archive.startswith(str(new) + os.sep), archive)
        self.assertIn("/archive/", archive)
        self.assertTrue(os.path.exists(archive))
        self.assertIsNone(sa._entry_error(entries[0]))
        # The manifest was not rewritten: the absolute path is still the old one.
        self.assertTrue(rec["archive"].startswith(str(old) + os.sep))
        self.assertTrue(sa._archive_ok(rec, new))

        target = self.home / "restored"
        buf = io.StringIO()
        self.assertEqual(sa.restore(unit.uid, str(target), new / "state", buf), 0)
        self.assertEqual((target / "a.jsonl").read_bytes(), Path(unit.members[0].path).read_bytes())
        self.assertEqual(sa.Manifest(new / "state").latest[unit.uid]["status"], "restored")

    def test_legacy_trash_path_follows_the_move(self):
        root, _path, unit = self._unit()
        dest = self.home / "old"
        rec, manifest = self.archive_unit(unit, dest, FakeClient())
        trash = sa._trash_dir(dest, unit.uid)
        trash.mkdir(parents=True, exist_ok=True)
        journal = dict(
            rec,
            status="reclaiming",
            trash=str(trash),
            trash_members=[
                {"rel": m["rel"], "path": member.path, "sha256": m["sha256"], "size": m["size"]}
                for m, member in zip(rec["members"], unit.members)
            ],
        )
        manifest.write(journal)
        new = self.home / "new"
        os.rename(dest, new)
        moved = sa.Manifest(new / "state")
        rec = moved.latest[unit.uid]
        self.assertNotIn("trash_rel", rec)
        named = sa.resolve_stored_path(moved.dest, rec.get("trash_rel"), rec["trash"])
        self.assertEqual(os.path.abspath(named), os.path.abspath(str(sa._trash_dir(new, unit.uid))))
        # Recovery therefore sees a canonical trash dir, not a stale path.
        action, why = sa._trash_plan(moved, sa._trash_dir(new, unit.uid))
        self.assertNotIn("non-canonical", why)
        self.assertIn(action, ("archive", "finish"), why)

    def test_new_records_store_relative_paths(self):
        root, _path, unit = self._unit()
        dest = self.home / "dest"
        client = FakeClient()
        code = process_one(unit, dest, client)
        self.assertEqual(code, 0)
        rec = sa.Manifest(dest / "state").latest[unit.uid]
        self.assertEqual(rec["archive_rel"], "archive/pi/proj/a.jsonl.0.tar.zst")
        self.assertEqual(rec["archives"][0]["rel"], rec["archive_rel"])
        os.rename(dest, self.home / "moved")
        entries = sa._seq_entries(rec, self.home / "moved")
        self.assertTrue(entries[0]["path"].startswith(str(self.home / "moved") + os.sep))
        self.assertIsNone(sa._entry_error(entries[0]))


def process_one(unit, dest, client):
    """Run one ``--apply --no-reclaim`` pass for a single patched tool."""
    out = io.StringIO()
    opts = {
        "apply": True,
        "no_reclaim": True,
        "scheduled": False,
        "tools": [unit.tool],
        "limit": None,
        "max_minutes": None,
        "url": None,
        "dest": str(dest),
    }
    with mock.patch.dict(sa.TOOLS, {unit.tool: (unit.root, sa.TOOLS[unit.tool][1])}), mock.patch.object(
        sa.vlogs, "VLogsClient", lambda url=None: client
    ), mock.patch.object(sa, "live_files", lambda names: []):
        return sa.run(opts, out=out, state=Path(dest).parent / "config-state")


# --------------------------------------------------------------- access preflight

class PreflightTest(Base):
    def _run_scheduled(self, root, dest, buf):
        opts = {
            "apply": True,
            "no_reclaim": False,
            "scheduled": True,
            "tools": ["pi"],
            "limit": None,
            "max_minutes": None,
            "url": None,
            "dest": str(dest),
        }
        with mock.patch.dict(sa.TOOLS, {"pi": (str(root), sa.discover_pi)}), mock.patch.object(
            sa.vlogs, "VLogsClient", lambda url=None: FakeClient()
        ), mock.patch.object(sa, "live_files", lambda names: []):
            started = time.monotonic()
            code = sa.run(opts, out=buf, state=self.home / "state")
            elapsed = time.monotonic() - started
        return code, elapsed

    def test_unwritable_dest_exits_6_with_one_line(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        age(path, 30)
        dest = self.home / "locked"
        dest.mkdir()
        os.chmod(dest, 0o500)
        self.addCleanup(os.chmod, str(dest), 0o700)
        buf = io.StringIO()
        code, elapsed = self._run_scheduled(root, dest, buf)
        self.assertEqual(code, 6)
        self.assertLess(elapsed, 30, "preflight must not wait")
        text = buf.getvalue().strip()
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertIn("ERROR: no access to", text)
        self.assertIn("macOS privacy", text)
        self.assertTrue(path.exists())

    def test_unreadable_tool_root_exits_6(self):
        root = self.home / "pi"
        _path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        buf = io.StringIO()
        real_scandir = os.scandir

        def denied(path):
            if os.path.abspath(str(path)) == os.path.abspath(str(root)):
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_scandir(path)

        with mock.patch.object(sa.os, "scandir", denied):
            code, _elapsed = self._run_scheduled(root, self.home / "dest", buf)
        self.assertEqual(code, 6)
        self.assertIn("ERROR: no access to %s" % root, buf.getvalue())

    def test_writable_dest_passes_preflight(self):
        root = self.home / "pi"
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        age(path, 30)
        buf = io.StringIO()
        with mock.patch.object(sa, "scheduled_skip", lambda _state, _now: None):
            code, _elapsed = self._run_scheduled(root, self.home / "dest", buf)
        self.assertEqual(code, 0, buf.getvalue())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
