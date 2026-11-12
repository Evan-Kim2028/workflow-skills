"""Native agent-session archive: discovery, packing, ingest, reclaim, restore.

Synthetic fixtures copy the key shapes of every tool layout (tiny files).
The store is a fake client; no real root, no live VictoriaLogs and no
launchctl is touched. Real-data runs are dry runs only.
"""

import fcntl
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


def zstd_compress(src, dst):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["zstd", "-q", "-f", "-o", str(dst), str(src)], check=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def unit(self, tool, root):
        units = sa.TOOLS[tool][1](str(root))
        self.assertTrue(units, "no units discovered for %s" % tool)
        return units[0]


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
        sa._extract(str(archive), str(target), records)
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
        sa._extract(str(archive), str(target), records)
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
    def _archived(self, root, days=20):
        path = write(root / "proj" / "a.jsonl", '{"type":"session"}\n')
        age(path, days)
        unit = self.unit("pi", root)
        records = sa.pack(unit, str(self.home / "a.tar.zst"))
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = sa.count_member_rows(member, unit)
        rec = {"unit": unit.uid, "members": records, "status": "archived"}
        return unit, rec, path

    def test_reclaim_deletes_exactly_the_unit(self):
        root = self.home / "pi"
        unit, rec, path = self._archived(root)
        other = write(root / "proj" / "b.jsonl", '{"type":"session"}\n')
        age(other, 20)
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}}
        reason = sa.reclaim_unit(unit, rec, cfg, time.time())
        self.assertIsNone(reason)
        self.assertFalse(path.exists())
        self.assertTrue(other.exists())

    def test_refused_when_too_young(self):
        root = self.home / "pi"
        unit, rec, path = self._archived(root, days=1)
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}}
        reason = sa.reclaim_unit(unit, rec, cfg, time.time())
        self.assertIn("too young", reason)
        self.assertTrue(path.exists())

    def test_refused_when_changed_since_archive(self):
        root = self.home / "pi"
        unit, rec, path = self._archived(root)
        path.write_text('{"type":"session","x":1}\n', encoding="utf-8")
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}}
        reason = sa.reclaim_unit(unit, rec, cfg, time.time())
        self.assertIn("changed since archive", reason)
        self.assertTrue(path.exists())

    def test_refused_when_path_escapes_root(self):
        root = self.home / "pi"
        unit, rec, _path = self._archived(root)
        outside = write(self.home / "outside.jsonl", '{"type":"session"}\n')
        age(outside, 20)
        unit.members[0].path = str(outside)
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {}}
        reason = sa.reclaim_unit(unit, rec, cfg, time.time())
        self.assertIn("escapes root", reason)
        self.assertTrue(outside.exists())

    def test_per_tool_override(self):
        root = self.home / "pi"
        unit, rec, path = self._archived(root, days=3)
        cfg = {"reclaim_days": 14.0, "reclaim_days_by_tool": {"pi": 1.0}}
        self.assertIsNone(sa.reclaim_unit(unit, rec, cfg, time.time()))
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
            # Uninstall removes the plist.
            res = subprocess.run(
                ["sh", str(SCRIPTS / "session_archive_install.sh"), "--uninstall"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main()
