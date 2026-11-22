"""Tests for ``dash/storebuild.py``: rebuild from the archives, then cut over.

A fake store (no process) and an in-memory client stand in for VictoriaLogs,
so the whole rebuild and cutover run without a real store. The archive units
are synthetic: a real ``tar.zst`` from ``sessionarchive.pack`` and a real
``zst`` from the log archive's own compressor.
"""

import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from dash import logarchive, sessionarchive, storebuild

from tests import fake_vlogs

B64 = fake_vlogs.B64


def _parse_in(logsql):
    inner = logsql.split("path:in(", 1)[1].split(")", 1)[0]
    return {part.strip().strip('"') for part in inner.split(",")}


class FakeClient:
    """The slice of ``VLogsClient`` storebuild uses, in memory."""

    def __init__(self):
        self.rows = []
        self.inserts = 0

    def healthy(self):
        return True

    def flush(self):
        pass

    def insert(self, lines):
        sent = 0
        for line in lines:
            self.rows.append(json.loads(line))
            sent += 1
        self.inserts += 1
        return sent

    def count(self, path):
        return sum(1 for row in self.rows if row.get("path") == path)

    def query(self, logsql):
        if logsql.startswith("* | stats by (path)"):
            counts = {}
            for row in self.rows:
                path = row.get("path") or ""
                counts[path] = counts.get(path, 0) + 1
            return [{"path": path, "n": str(n)} for path, n in counts.items()]
        if logsql.startswith("path:in("):
            paths = _parse_in(logsql)
            rows = [row for row in self.rows if row.get("path") in paths]
            if "stats by (path)" in logsql:
                counts = {}
                for row in rows:
                    path = row.get("path") or ""
                    counts[path] = counts.get(path, 0) + 1
                return [{"path": path, "n": str(n)} for path, n in counts.items()]
            match = re.search(r"\| limit (\d+)", logsql)
            if match:
                rows = rows[: int(match.group(1))]
            return rows
        if logsql.startswith("* | stats by (ws, path)"):
            counts = {}
            for row in self.rows:
                key = (row.get("ws") or "", row.get("path") or "")
                counts[key] = counts.get(key, 0) + 1
            return [{"ws": ws, "path": path, "n": str(n)} for (ws, path), n in counts.items()]
        if logsql.startswith("* | stats by (ws)"):
            counts = {}
            for row in self.rows:
                ws = row.get("ws") or ""
                counts[ws] = counts.get(ws, 0) + 1
            return [{"ws": ws, "n": str(n)} for ws, n in counts.items()]
        if logsql.startswith('path:="'):
            path = logsql.split('path:="', 1)[1].split('"', 1)[0]
            if "count_uniq(member, line)" in logsql:
                rev = logsql.split('rev:="', 1)[1].split('"', 1)[0]
                seen = {
                    (row.get("member"), row.get("line"))
                    for row in self.rows
                    if row.get("path") == path and row.get("rev") == rev
                }
                return [{"n": str(len(seen))}]
            return [{"n": str(self.count(path))}]
        if logsql.startswith('ws:="'):
            ws = logsql.split('ws:="', 1)[1].split('"', 1)[0]
            n = i = o = c = 0
            for row in self.rows:
                if row.get("ws") != ws:
                    continue
                try:
                    obj = json.loads(row.get("_msg") or "")
                except ValueError:
                    continue
                message = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                n += 1
                i += usage.get("input") or 0
                o += usage.get("output") or 0
                c += usage.get("cacheRead") or 0
            return [
                {
                    "n": str(n),
                    "i": str(i) if n else "NaN",
                    "o": str(o) if n else "NaN",
                    "c": str(c) if n else "NaN",
                }
            ]
        raise AssertionError("unexpected query: %s" % logsql)


class FakeStore:
    def __init__(self, url="http://127.0.0.1:19429"):
        self.url = url
        self.started = 0
        self.stopped = 0

    def start(self, wait_s=0):
        self.started += 1

    def stop(self, timeout=0):
        self.stopped += 1


class FakeService:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")
        return 0

    def start(self):
        self.calls.append("start")
        return 0

    def running(self):
        return True


SESSION_BODY = b"".join(
    [
        b'{"type":"user","message":{"role":"user","content":"hi"}}\n',
        (
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "image", "data": B64}],
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode(),
        b'{"type":"assistant","message":{"role":"assistant","usage":{"input":10,"output":5,"cacheRead":100}}}\n',
    ]
)

LOG_BODY = (
    b'{"type":"message_end","message":{"role":"assistant","usage":{"input":3,"output":2,"cacheRead":7}}}\n'
)


def make_session_unit(dest, tool="claude", key="proj/sess-1.jsonl", body=None, ts=None, rev="1", seq=0):
    """Pack a synthetic unit into ``<dest>/archive`` and return its manifest record."""
    src = Path(dest) / "src" / tool / key
    src.parent.mkdir(parents=True, exist_ok=True)
    body = SESSION_BODY if body is None else body
    src.write_bytes(body)
    st = src.stat()
    member = sessionarchive.Member(
        path=str(src), rel=key, rows="jsonl", size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    unit = sessionarchive.Unit(
        tool=tool,
        key=key,
        root=str(src.parent),
        parent=str(src.parent),
        session="sess-1",
        members=[member],
        newest_mtime=st.st_mtime,
    )
    archive = Path(dest) / "archive" / tool / ("%s.%d.tar.zst" % (key, seq))
    archive.parent.mkdir(parents=True, exist_ok=True)
    records = sessionarchive.pack(unit, str(archive))
    return {
        "unit": unit.uid,
        "tool": tool,
        "key": key,
        "session": "sess-1",
        "root": str(src.parent),
        "parent": str(src.parent),
        "archive": str(archive),
        "archive_rel": os.path.relpath(archive, dest).replace(os.sep, "/"),
        "archive_bytes": archive.stat().st_size,
        "seq": seq,
        "raw_bytes": st.st_size,
        "newest_mtime": st.st_mtime,
        "rev": rev,
        "lines": len(body.splitlines()),
        "members": records,
        "status": "archived",
        "ts": time.time() if ts is None else ts,
    }


def write_session_manifest(dest, recs):
    state = Path(dest) / "state"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "manifest.jsonl", "a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def make_log_file(state, rel="agentq/jobs/j1/runs/r1/pi/stream-x.ndjson", body=None, ts=None, extra=None):
    """Write a real zst under the log-archive root and its manifest record."""
    root = logarchive.archive_root(state)
    zst = root / (rel + ".zst")
    zst.parent.mkdir(parents=True, exist_ok=True)
    body = LOG_BODY if body is None else body
    subprocess.run(
        [logarchive._zstd_path(), "-q", "-f", "-o", str(zst)], input=body, check=True
    )
    rec = {
        "path": rel,
        "source": "/tmp/source",
        "ws": "agentq/ws",
        "label": "lbl",
        "kind": "stream",
        "tool": "other",
        "extra": {"job_id": "j1"} if extra is None else extra,
        "size": len(body),
        "mtime_ns": 1700000000000000000,
        "stripped_lines": len(body.splitlines()),
        "status": "reclaimed",
        "ts": time.time() if ts is None else ts,
    }
    with open(root / "manifest.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "sessions"
        self.state = self.dest / "state"
        self.data = self.root / "rebuilt"
        self.out = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _jobs(self):
        return storebuild.session_jobs(self.dest, self.state) + storebuild.log_jobs(self.state)

    def _reference(self):
        """A client holding exactly what the archives say, as the live store would."""
        client = FakeClient()
        for job in self._jobs():
            storebuild.ingest_job(job, client)
        return client

    def test_rebuild_strips_base64_and_drops_duplicate_fields(self):
        rec = make_session_unit(self.dest)
        write_session_manifest(self.dest, [rec])
        make_log_file(self.state)
        new = FakeClient()
        code = storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=self._reference(),
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertTrue(new.rows)
        session_rows = [r for r in new.rows if r.get("path") == rec["unit"]]
        self.assertEqual(len(session_rows), rec["lines"])
        image_row = [r for r in session_rows if "<img sha256=" in r["_msg"]]
        self.assertEqual(len(image_row), 1, "the base64 image must be stripped in the store")
        self.assertNotIn(B64[:40], image_row[0]["_msg"])
        for row in new.rows:
            self.assertNotIn("unit", row)
            self.assertNotIn("session", row)
        self.assertEqual(session_rows[0]["label"], "sess-1")
        log_rows = [r for r in new.rows if r.get("path", "").startswith("agentq/")]
        self.assertEqual(len(log_rows), 1)
        self.assertEqual(log_rows[0]["job_id"], "j1")

    def test_rebuild_writes_checkpoint_and_snapshot(self):
        rec = make_session_unit(self.dest)
        write_session_manifest(self.dest, [rec])
        new = FakeClient()
        storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=self._reference(),
        )
        side = storebuild.sidecar_dir(self.data)
        meta = json.loads((side / "meta.json").read_text())
        self.assertIn("snapshot_ts", meta)
        lines = (side / "checkpoint.jsonl").read_text().splitlines()
        keys = {json.loads(line)["key"] for line in lines}
        self.assertIn("session:%s:%s" % (rec["unit"], rec["rev"]), keys)

    def test_rebuild_resumes_and_skips_checkpointed_jobs(self):
        rec = make_session_unit(self.dest)
        write_session_manifest(self.dest, [rec])
        new = FakeClient()
        kwargs = dict(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=self._reference(),
        )
        storebuild.rebuild(**kwargs)
        first = len(new.rows)
        self.assertGreater(first, 0)
        # A second run with the same checkpoint must not re-ingest the unit.
        storebuild.rebuild(**kwargs)
        self.assertEqual(len(new.rows), first)

    def test_rebuild_fails_verification_when_a_ws_is_missing(self):
        rec = make_session_unit(self.dest)
        write_session_manifest(self.dest, [rec])
        old = self._reference()

        class LossyClient(FakeClient):
            """Drops one row of the archived unit, as a short rebuild would."""

            def insert(self, lines):
                lines = list(lines)
                return super().insert(lines[1:] if len(lines) > 1 else [])

        new = LossyClient()
        code = storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=old,
        )
        self.assertEqual(code, 1)
        self.assertIn("NO", self.out.getvalue())

    def test_every_revision_is_ingested_not_just_the_newest(self):
        # A unit that grew after it was packed is archived again with a higher
        # rev, and the store keeps both revisions. A rebuild that read only
        # the manifest's newest record would halve that unit's rows.
        rev0 = make_session_unit(self.dest, rev="0")
        rev1 = make_session_unit(self.dest, rev="1", seq=1)
        write_session_manifest(self.dest, [rev0, rev1])
        jobs = storebuild.session_jobs(self.dest)
        self.assertEqual(len(jobs), 2, [j.key for j in jobs])
        self.assertEqual({j.unit.get("rev") for j in jobs}, {"0", "1"})
        new = FakeClient()
        code = storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=self._reference(),
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(new.count(rev0["unit"]), 2 * rev0["lines"])
        self.assertEqual({r.get("rev") for r in new.rows}, {"0", "1"})

    def test_a_packed_member_is_decompressed(self):
        # Codex keeps some histories as .jsonl.zst; the tar holds the packed
        # bytes losslessly, so the row reader must decompress it. Reading the
        # frame as text yields a couple of garbage rows instead of the real
        # ones (a live-store defect this rebuild repairs).
        plain = b"".join(b'{"type":"user","message":{"role":"user","content":"line %d"}}\n' % i for i in range(40))
        packed = subprocess.run(
            [sessionarchive._zstd_bin(), "-q", "-c"], input=plain, capture_output=True, check=True
        ).stdout
        rec = make_session_unit(
            self.dest, tool="codex", key="sessions/s.jsonl.zst", body=packed
        )
        self.assertGreater(len(packed.splitlines()), 1)
        # ``pack`` sniffs rows from the bytes, which are packed here, so say
        # what the real ingest knows: this member is jsonl.
        for mrec in rec["members"]:
            mrec["rows"] = "jsonl"
        write_session_manifest(self.dest, [rec])
        new = FakeClient()
        code = storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=FakeStore(),
            client_factory=lambda url: new,
            old_client=new,
        )
        self.assertEqual(code, 0, self.out.getvalue())
        rows = [r for r in new.rows if r.get("path") == rec["unit"]]
        self.assertEqual(len(rows), 40, "the 40 real lines, not the packed frame's few newlines")
        self.assertIn("line 39", " ".join(r["_msg"] for r in rows))

    def test_dry_run_starts_nothing(self):
        make_session_unit(self.dest)
        write_session_manifest(self.dest, [make_session_unit(self.dest, key="proj/sess-2.jsonl")])
        store = FakeStore()
        code = storebuild.rebuild(
            data_dir=str(self.data),
            port=19429,
            dest=str(self.dest),
            state=self.state,
            out=self.out,
            store=store,
        )
        self.assertEqual(code, 0)
        self.assertEqual(store.started, 0)
        self.assertFalse(self.data.exists())
        self.assertIn("dry run", self.out.getvalue())

    def test_session_jobs_skip_a_missing_archive(self):
        rec = make_session_unit(self.dest)
        os.unlink(rec["archive"])
        write_session_manifest(self.dest, [rec])
        self.assertEqual(storebuild.session_jobs(self.dest, self.state), [])

    def test_live_flags_drop_listen_and_data_path(self):
        plist = self.root / "vlogs.plist"
        plist.write_text(
            "<plist><dict><key>ProgramArguments</key><array>"
            "<string>/bin/victoria-logs-prod</string>"
            "<string>-httpListenAddr=127.0.0.1:19428</string>"
            "<string>-storageDataPath=/tmp/data</string>"
            "<string>-retentionPeriod=100y</string>"
            "<string>-storage.minFreeDiskSpaceBytes=20GiB</string>"
            "</array></dict></plist>",
            encoding="utf-8",
        )
        flags = storebuild.live_flags(plist)
        self.assertIn("-retentionPeriod=100y", flags)
        self.assertIn("-storage.minFreeDiskSpaceBytes=20GiB", flags)
        self.assertFalse(any(f.startswith("-httpListenAddr") for f in flags))
        self.assertFalse(any(f.startswith("-storageDataPath") for f in flags))
        self.assertEqual(storebuild.live_port(plist), 19428)
        self.assertEqual(storebuild.live_data_dir(plist), "/tmp/data")


class CutoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "sessions"
        self.state = self.dest / "state"
        self.live = self.root / "live"
        self.rebuilt = self.root / "rebuilt"
        self.live.mkdir()
        (self.live / "marker").write_text("old")
        self.rebuilt.mkdir()
        (self.rebuilt / "marker").write_text("new")
        self.out = io.StringIO()
        self.service = FakeService()
        self.archive_service = FakeService()

    def tearDown(self):
        self.tmp.cleanup()

    def _meta(self, snapshot_ts=None, expected_paths=("u",)):
        storebuild.write_meta(
            self.rebuilt,
            {
                "snapshot_ts": time.time() if snapshot_ts is None else snapshot_ts,
                "port": 19429,
                "expected_paths": list(expected_paths),
            },
        )

    def _cutover(self, **kwargs):
        kwargs.setdefault("from_dir", str(self.rebuilt))
        kwargs.setdefault("dest", str(self.dest))
        kwargs.setdefault("state", self.state)
        kwargs.setdefault("out", self.out)
        kwargs.setdefault("live_data", str(self.live))
        kwargs.setdefault("port", 19428)
        kwargs.setdefault("service", self.service)
        kwargs.setdefault("archive_service", self.archive_service)
        kwargs.setdefault("store", FakeStore())
        kwargs.setdefault("healthy", lambda url: True)
        return storebuild.cutover(**kwargs)

    def test_dry_run_changes_nothing(self):
        self._meta()
        code = self._cutover()
        self.assertEqual(code, 0)
        self.assertEqual((self.live / "marker").read_text(), "old")
        self.assertEqual(self.service.calls, [])
        self.assertIn("dry run", self.out.getvalue())

    def test_cutover_swaps_and_keeps_the_old_dir(self):
        self._meta(expected_paths=())
        good = FakeClient()
        code = self._cutover(
            apply=True,
            client_factory=lambda url: good,
            old_client=good,
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual((self.live / "marker").read_text(), "new")
        olds = list(self.root.glob("live.old-*"))
        self.assertEqual(len(olds), 1)
        self.assertEqual((olds[0] / "marker").read_text(), "old")
        self.assertEqual(self.service.calls, ["stop", "start"])
        self.assertEqual(self.archive_service.calls, ["stop", "start"])
        self.assertIn("rm -rf", self.out.getvalue())

    def test_cutover_swaps_back_on_failed_verification(self):
        self._meta()
        good = FakeClient()
        good.rows.append({"ws": "native/claude", "path": "u", "_msg": "{}"})
        bad = FakeClient()
        code = self._cutover(
            apply=True,
            client_factory=lambda url: bad,
            old_client=good,
        )
        self.assertEqual(code, 1)
        self.assertEqual((self.live / "marker").read_text(), "old")
        self.assertEqual((self.rebuilt / "marker").read_text(), "new")
        self.assertEqual(self.service.calls, ["stop", "start", "stop", "start"])
        self.assertEqual(self.archive_service.calls, ["stop", "start"])

    def test_cutover_reingests_units_archived_after_the_snapshot(self):
        snapshot = time.time() - 100
        self._meta(snapshot_ts=snapshot, expected_paths=())
        rec = make_session_unit(self.dest, ts=snapshot + 10)
        write_session_manifest(self.dest, [rec])
        new = FakeClient()
        code = self._cutover(
            apply=True,
            client_factory=lambda url: new,
            old_client=new,
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(new.count(rec["unit"]), rec["lines"])

    def test_cutover_refuses_without_rebuild_metadata(self):
        code = self._cutover(apply=True, client_factory=lambda url: FakeClient())
        self.assertEqual(code, 2)
        self.assertEqual(self.service.calls, [])

    def test_cutover_copies_live_only_paths_before_the_swap(self):
        self._meta(expected_paths=())
        live = FakeClient()
        live.insert(
            [json.dumps({"_time": "t", "_msg": "{}", "ws": "w", "path": "live-only"})]
        )
        new = FakeClient()
        code = self._cutover(
            apply=True,
            client_factory=lambda url: new,
            old_client=live,
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(new.count("live-only"), 1)

    def test_cutover_refuses_to_swap_when_the_copy_is_short(self):
        self._meta(expected_paths=())
        live = FakeClient()
        live.insert(
            [json.dumps({"_time": "t", "_msg": "{}", "ws": "w", "path": "live-only"})]
        )

        class LossyClient(FakeClient):
            def insert(self, lines):
                return 0

        code = self._cutover(
            apply=True,
            client_factory=lambda url: LossyClient(),
            old_client=live,
        )
        self.assertEqual(code, 1)
        self.assertEqual((self.live / "marker").read_text(), "old")
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.archive_service.calls, ["stop", "start"])


class VerifyTest(unittest.TestCase):
    """``verify`` compares per ws and tells a short rebuild from live-only rows."""

    def _row(self, ws, path, usage=None, sub="s1"):
        msg = {"message": {"role": "assistant" if usage else "user", "content": sub}}
        if usage:
            msg["message"]["usage"] = usage
        return {"ws": ws, "label": "lbl", "path": path, "type": "assistant", "_msg": json.dumps(msg)}

    def test_identical_stores_verify(self):
        old, new = FakeClient(), FakeClient()
        for client in (old, new):
            client.insert([json.dumps(self._row("ws1", "u1", {"input": 3, "output": 5, "cacheRead": 7}))])
        self.assertEqual(storebuild.verify(old, new, out=io.StringIO()), (True, unittest.mock.ANY))

    def test_rows_only_the_live_store_has_are_reported_not_failed(self):
        old = FakeClient()
        old.insert(
            [
                json.dumps(self._row("ws1", "u1")),
                json.dumps(self._row("ws1", "un-archived")),
                json.dumps(self._row("ws2", "u2")),
            ]
        )
        new = FakeClient()
        new.insert([json.dumps(self._row("ws1", "u1")), json.dumps(self._row("ws2", "u2"))])
        out = io.StringIO()
        ok, table = storebuild.verify(old, new, out=out, expected_paths={"u1", "u2"})
        self.assertTrue(ok, out.getvalue())
        rows = {row["ws"]: row for row in table}
        self.assertEqual(rows["ws1"]["live_only_rows"], 1)
        self.assertEqual(rows["ws1"]["expected_rows"], 1)
        self.assertEqual(rows["ws1"]["new_rows"], 1)
        self.assertEqual(rows["ws2"]["live_only_rows"], 0)
        self.assertIn("live-only: 1 rows", out.getvalue())

    def test_a_short_rebuild_fails(self):
        old = FakeClient()
        old.insert([json.dumps(self._row("ws1", "u1")), json.dumps(self._row("ws1", "u2"))])
        new = FakeClient()
        new.insert([json.dumps(self._row("ws1", "u1"))])
        out = io.StringIO()
        ok, table = storebuild.verify(old, new, out=out, expected_paths={"u1", "u2"})
        self.assertFalse(ok, "a unit the archive has but the store lacks is a failure")
        self.assertIn("MISSING", out.getvalue())
        self.assertFalse(table[0]["rows_ok"])
        self.assertEqual(table[0]["live_only_rows"], 0)
        self.assertTrue(table[0]["tokens_ok"], "no live-only rows, so tokens must be compared")

    def test_token_mismatch_fails(self):
        old = FakeClient()
        old.insert([json.dumps(self._row("ws1", "u1", {"input": 3, "output": 5, "cacheRead": 7}))])
        new = FakeClient()
        new.insert([json.dumps(self._row("ws1", "u1", {"input": 1, "output": 5, "cacheRead": 7}))])
        ok, table = storebuild.verify(old, new, out=io.StringIO())
        self.assertFalse(ok)
        self.assertTrue(table[0]["rows_ok"])
        self.assertFalse(table[0]["tokens_ok"])

    def test_an_archived_path_neither_store_has_is_reported_not_failed(self):
        old = FakeClient()
        old.insert([json.dumps(self._row("ws1", "u1"))])
        new = FakeClient()
        new.insert([json.dumps(self._row("ws1", "u1"))])
        out = io.StringIO()
        ok, _table = storebuild.verify(old, new, out=out, expected_paths={"u1", "future"})
        self.assertTrue(ok, out.getvalue())
        self.assertIn("archive-only: 1", out.getvalue())


class CopyTest(unittest.TestCase):
    """``copy_from_live`` copies the rows the archive cannot reproduce."""

    def _row(self, path, ws="native/claude", kind="native", msg="hi", **extra):
        row = {
            "_time": "2026-11-12T22:15:41.63Z",
            "_msg": msg,
            "ws": ws,
            "label": "lbl",
            "path": path,
            "type": "user",
            "kind": kind,
        }
        row.update(extra)
        return row

    def test_copy_copies_a_live_only_path_and_drops_duplicate_fields(self):
        live = FakeClient()
        live.insert([json.dumps(self._row("live-only", unit="u", session="s"))])
        new = FakeClient()
        result = storebuild.copy_from_live(live, new, ["live-only"], out=io.StringIO())
        self.assertEqual(result["live-only"]["copied"], 1)
        self.assertTrue(result["live-only"]["ok"])
        self.assertEqual(new.count("live-only"), 1)
        row = new.rows[0]
        self.assertNotIn("unit", row)
        self.assertNotIn("session", row)
        self.assertEqual(row["_time"], "2026-11-12T22:15:41.63Z")
        self.assertEqual(row["ws"], "native/claude")

    def test_copy_keeps_session_on_an_agentq_row(self):
        live = FakeClient()
        live.insert(
            [
                json.dumps(
                    self._row(
                        "agentq/jobs/j1/runs/r1/pi/x.ndjson",
                        ws="agentq",
                        kind="log",
                        session="sess-1",
                    )
                )
            ]
        )
        new = FakeClient()
        storebuild.copy_from_live(
            live, new, ["agentq/jobs/j1/runs/r1/pi/x.ndjson"], out=io.StringIO()
        )
        self.assertEqual(new.rows[0]["session"], "sess-1")

    def test_copy_is_idempotent(self):
        live = FakeClient()
        live.insert([json.dumps(self._row("p", n=i)) for i in range(3)])
        new = FakeClient()
        storebuild.copy_from_live(live, new, ["p"], out=io.StringIO())
        storebuild.copy_from_live(live, new, ["p"], out=io.StringIO())
        self.assertEqual(new.count("p"), 3)

    def test_copy_adds_only_the_delta_on_a_second_pass(self):
        live = FakeClient()
        live.insert([json.dumps(self._row("p", n=0))])
        new = FakeClient()
        storebuild.copy_from_live(live, new, ["p"], out=io.StringIO())
        live.insert([json.dumps(self._row("p", n=1))])
        result = storebuild.copy_from_live(live, new, ["p"], out=io.StringIO())
        self.assertEqual(result["p"]["copied"], 1)
        self.assertEqual(new.count("p"), 2)

    def test_copy_reports_a_short_copy(self):
        live = FakeClient()
        live.insert([json.dumps(self._row("p", n=i)) for i in range(3)])

        class LossyClient(FakeClient):
            def insert(self, lines):
                lines = list(lines)
                return super().insert(lines[1:])

        new = LossyClient()
        result = storebuild.copy_from_live(live, new, ["p"], out=io.StringIO())
        self.assertFalse(result["p"]["ok"])
        self.assertEqual(result["p"]["rebuilt"], 2)

    def test_copy_skips_a_path_the_live_store_lacks(self):
        live = FakeClient()
        new = FakeClient()
        result = storebuild.copy_from_live(live, new, ["archive-only"], out=io.StringIO())
        self.assertTrue(result["archive-only"]["ok"])
        self.assertEqual(result["archive-only"]["copied"], 0)

    def test_copy_plan_splits_live_only_from_missing(self):
        live = FakeClient()
        live.insert([json.dumps(self._row("live-only"))])
        live_only, missing = storebuild.copy_plan(live, {"archive-only"})
        self.assertEqual(live_only, ["live-only"])
        self.assertEqual(missing, ["archive-only"])


class CopyOnlyTest(unittest.TestCase):
    """``copy_only`` is the standalone form of the cutover's copy step."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "rebuilt"
        self.data.mkdir()
        self.out = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _meta(self):
        storebuild.write_meta(
            self.data, {"snapshot_ts": 1.0, "port": 19429, "expected_paths": ["u"]}
        )

    def _live(self):
        live = FakeClient()
        live.insert(
            [
                json.dumps(
                    {
                        "_time": "2026-11-12T22:15:41.63Z",
                        "_msg": "{}",
                        "ws": "w",
                        "path": "live-only",
                    }
                )
            ]
        )
        return live

    def test_dry_run_starts_nothing(self):
        self._meta()
        live = self._live()
        store = FakeStore()
        code = storebuild.copy_only(
            data_dir=str(self.data),
            port=19429,
            apply=False,
            out=self.out,
            store=store,
            client_factory=lambda url: live,
            old_client=live,
        )
        self.assertEqual(code, 0)
        self.assertEqual(store.started, 0)
        self.assertIn("dry run", self.out.getvalue())

    def test_apply_copies_and_records_the_paths(self):
        self._meta()
        live = self._live()
        new = FakeClient()
        store = FakeStore()
        code = storebuild.copy_only(
            data_dir=str(self.data),
            port=19429,
            apply=True,
            out=self.out,
            store=store,
            client_factory=lambda url: new,
            old_client=live,
        )
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(new.count("live-only"), 1)
        meta = storebuild.read_meta(self.data)
        self.assertIn("live-only", meta["expected_paths"])
        self.assertEqual(store.started, 1)
        self.assertEqual(store.stopped, 1)


if __name__ == "__main__":
    unittest.main()


class LaunchdServiceRaceTest(unittest.TestCase):
    """bootstrap right after bootout fails with I/O error 5 until launchd lets go."""

    def make(self, bootstrap_rcs, running_seq):
        svc = storebuild.LaunchdService("com.example.test", plist="/tmp/x.plist", uid=501)
        calls = []
        rcs = iter(bootstrap_rcs)
        runs = iter(running_seq)

        def fake_run(*args):
            calls.append(args[0])
            if args[0] == "bootstrap":
                return next(rcs)
            if args[0] == "print":
                return 0 if next(runs, False) else 113
            return 0

        svc._run = fake_run
        return svc, calls

    def test_start_retries_a_failed_bootstrap(self):
        svc, calls = self.make([5, 5, 0], [False, False])
        with unittest.mock.patch.object(storebuild.time, "sleep"):
            self.assertEqual(svc.start(), 0)
        self.assertEqual(calls.count("bootstrap"), 3)

    def test_start_gives_up_after_its_attempts(self):
        svc, calls = self.make([5] * 5, [False] * 5)
        with unittest.mock.patch.object(storebuild.time, "sleep"):
            self.assertEqual(svc.start(), 5)
        self.assertEqual(calls.count("bootstrap"), 5)

    def test_stop_waits_until_the_job_is_unloaded(self):
        svc, calls = self.make([], [True, True, False])
        with unittest.mock.patch.object(storebuild.time, "sleep"):
            svc.stop()
        self.assertEqual(calls.count("print"), 3)
