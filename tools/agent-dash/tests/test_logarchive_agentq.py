"""``logs archive --agentq`` and ``agentq gc``: archive a job folder before reclaiming it.

Runs the real pipeline on a synthetic agentq jobs tree (``<home>/jobs/<id>/runs/<run>/pi``)
under a temp state dir. The store is a fake client object (rows kept in memory,
the count and token queries answered for the shapes the archive sends), so the
tests need no VictoriaLogs. Covered: discovery of a finished job's stream and
session files (including the janitor's ``.gz`` copies), the job fields on every
row, the gzip keep rule, the verified pipeline, idempotent reruns, and the two
guarantees ``agentq gc`` now rests on: a file is never reclaimed without a
verified archive, and an unreachable store deletes nothing.
"""

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentq import gc as G  # noqa: E402
from dash import logarchive as la  # noqa: E402
from fake_vlogs import B64, FakeClient, session_bytes, stream_bytes, usage_line  # noqa: E402,F401

NOW = 1_800_000_000.0
WEEK = 7 * 86400.0
JOB = "0a1b2c3d4e5f"
LABEL = "hub-exec-agentq-0a1b2c3d"
# A workspace path whose basename names the stream field; no real home path.
WORKSPACE = "/srv/work/workflow-skills"


def job_row(job_id=JOB, **over):
    """A ``jobs`` row as ``agentq.db.select_jobs`` returns it."""
    row = {
        "id": job_id,
        "state": "done",
        "label": LABEL,
        "workspace": WORKSPACE,
        "kind": "write",
        "tier": "pi-deepseek-flash",
        "verdict": "PASS",
        "session": "6eaeced1-b770-4937-b3eb-0edb03574081",
        "attempt": 0,
        "finished_at": NOW - 2 * WEEK,
    }
    row.update(over)
    return row


class Tree:
    """A synthetic ``$AGENTQ_HOME`` with one job folder."""

    def __init__(self, base: Path):
        self.home = base / "agentq"
        self.state = base / "state"
        self.pi = self.home / "jobs" / JOB / "runs" / "run1" / "pi"
        self.pi.mkdir(parents=True)
        self.state.mkdir(parents=True)

    def stream(self, name="stream-%s.ndjson" % LABEL, data=None, mtime=NOW - 2 * WEEK, gz=False):
        path = self.pi / name
        body = stream_bytes() if data is None else data
        if gz:
            with gzip.open(path, "wb") as fh:
                fh.write(body)
        else:
            path.write_bytes(body)
        os.utime(path, (mtime, mtime))
        return path

    def session(self, name="2026-11-14T13-26-07-894Z_01a1da3c.jsonl", data=None, mtime=NOW - 2 * WEEK, gz=False):
        folder = self.pi / "sessions"
        folder.mkdir(exist_ok=True)
        path = folder / name
        body = session_bytes() if data is None else data
        if gz:
            with gzip.open(path, "wb") as fh:
                fh.write(body)
        else:
            path.write_bytes(body)
        os.utime(path, (mtime, mtime))
        return path

    def db(self, rows):
        conn = sqlite3.connect(str(self.home / "agentq.db"))
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, label TEXT, workspace TEXT, kind TEXT,"
            " tier TEXT, verdict TEXT, session TEXT, attempt INTEGER, finished_at REAL)"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["state"], row["label"], row["workspace"], row["kind"],
                    row["tier"], row["verdict"], row["session"], row["attempt"], row["finished_at"],
                ),
            )
        conn.commit()
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.tree = Tree(self.base)
        self.client = FakeClient()

    def discover(self, jobs=None, **kw):
        return la.discover_agentq(self.tree.home, [job_row()] if jobs is None else jobs, now=NOW, **kw)

    def archive(self, entries, **kw):
        kw.setdefault("apply", True)
        kw.setdefault("client", self.client)
        return la.archive_entries(entries, state=self.tree.state, now=NOW, **kw)


class DiscoverTest(Base):
    def test_a_finished_job_yields_its_stream_and_session_files(self):
        stream = self.tree.stream()
        session = self.tree.session()
        entries, skipped = self.discover()
        # Sorted by path: ``sessions/`` sorts before ``stream-``.
        self.assertEqual([e.path for e in entries], [str(session), str(stream)])
        self.assertEqual(skipped, {})

    def test_the_janitor_gzip_copies_are_found_and_marked_compressed(self):
        stream = self.tree.stream(name="stream-%s.ndjson.gz" % LABEL, gz=True)
        session = self.tree.session(name="2026-11-14T13-26-07-894Z_01a1da3c.jsonl.gz", gz=True)
        entries, _ = self.discover()
        self.assertEqual([e.path for e in entries], [str(session), str(stream)])
        self.assertTrue(all(e.compressed for e in entries))
        self.assertEqual([e.kind for e in entries], ["session", "stream"])

    def test_a_running_job_is_not_discovered(self):
        self.tree.stream()
        entries, skipped = self.discover([job_row(state="running")])
        self.assertEqual(entries, [])
        self.assertEqual(skipped, {"not terminal": 1})

    def test_a_recently_finished_job_is_not_discovered(self):
        self.tree.stream()
        entries, skipped = self.discover([job_row(finished_at=NOW - 60)])
        self.assertEqual(entries, [])
        self.assertEqual(skipped, {"finished recently": 1})

    def test_a_job_id_with_path_parts_is_refused(self):
        self.tree.stream()
        entries, skipped = self.discover([job_row(id="../../victim")])
        self.assertEqual(entries, [])
        self.assertEqual(skipped, {"job id is not plain hex": 1})

    def test_a_symlinked_runs_folder_is_refused(self):
        outside = self.base / "outside" / "run1" / "pi"
        outside.mkdir(parents=True)
        (outside / "stream-x.ndjson").write_bytes(stream_bytes())
        runs = self.tree.home / "jobs" / JOB / "runs"
        for child in sorted(runs.iterdir()):
            for path in sorted(child.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            child.rmdir()
        runs.rmdir()
        runs.symlink_to(outside.parent.parent, target_is_directory=True)
        entries, skipped = self.discover()
        self.assertEqual(entries, [])
        self.assertEqual(skipped, {"runs folder is a symlink": 1})

    def test_other_files_are_ignored(self):
        (self.tree.pi / ("out-%s.md" % LABEL)).write_text("PASS\n")
        (self.tree.pi / ("err-%s.log" % LABEL)).write_text("noise\n")
        entries, _ = self.discover()
        self.assertEqual(entries, [])

    def test_the_entry_carries_the_job_identity(self):
        self.tree.stream()
        entry = self.discover()[0][0]
        self.assertEqual(entry.ws, "agentq/workflow-skills")
        self.assertEqual(entry.label, LABEL)
        self.assertEqual(entry.source, "agentq")
        self.assertEqual(entry.rel, "agentq/jobs/%s/runs/run1/pi/stream-%s.ndjson" % (JOB, LABEL))
        self.assertEqual(
            entry.extra,
            {
                "job_id": JOB,
                "job_kind": "write",
                "tier": "pi-deepseek-flash",
                "verdict": "PASS",
                "session": "6eaeced1-b770-4937-b3eb-0edb03574081",
                "attempt": "0",
            },
        )

    def test_a_gzip_file_is_never_kept_stripped(self):
        self.tree.stream(name="stream-%s.ndjson.gz" % LABEL, gz=True)
        entry = self.discover()[0][0]
        self.assertEqual(la.keep_for(entry), "remove")
        self.assertEqual(la.keep_for(entry, "stripped"), "remove")
        self.assertEqual(la.keep_for(entry, "stub"), "stub")

    def test_a_plain_pi_stream_keeps_the_stripped_copy(self):
        self.tree.stream()
        entry = self.discover()[0][0]
        self.assertEqual(la.keep_for(entry), "stripped")


class MeasureGzipTest(Base):
    def test_a_gzip_file_measures_the_same_as_its_plain_form(self):
        plain = self.tree.stream()
        gz = self.tree.stream(name="stream-%s.ndjson.gz" % LABEL, gz=True)
        a = la.measure(str(plain), None, "message_end")
        b = la.measure(str(gz), None, "message_end", compressed=True)
        self.assertEqual((a.raw_bytes, a.stripped_bytes, a.raw_lines), (b.raw_bytes, b.stripped_bytes, b.raw_lines))
        self.assertEqual(a.raw_tokens, b.raw_tokens)
        self.assertEqual(a.stripped_sha256, b.stripped_sha256)


class PipelineTest(Base):
    def test_apply_archives_ingests_verifies_and_reclaims(self):
        stream = self.tree.stream()
        session = self.tree.session()
        entries, _ = self.discover()
        rc, totals, results, _plans = self.archive(entries)
        self.assertEqual(rc, 0)
        self.assertEqual(results["archived"], 2)
        self.assertEqual(results["reclaimed"], 2)
        self.assertEqual(results["errors"], 0)
        # pi stream: stripped at the path, base64 gone, every line kept.
        self.assertTrue(stream.is_file())
        self.assertNotIn(b"QUJDREVGR0hJ", stream.read_bytes())
        self.assertEqual(len(stream.read_bytes().splitlines()), 4)
        # pi session: removed, its rows are in the store.
        self.assertFalse(session.exists())
        self.assertEqual(len(self.client.rows["agentq/jobs/%s/runs/run1/pi/sessions/"
                                            "2026-11-14T13-26-07-894Z_01a1da3c.jsonl" % JOB]), 3)
        # The zstd copy is under the log-archive tree, mirrored by the job path.
        zst = self.tree.state / "log-archive" / "agentq" / "jobs" / JOB / "runs" / "run1" / "pi"
        self.assertTrue((zst / ("stream-%s.ndjson.zst" % LABEL)).is_file())
        self.assertEqual(totals.raw, len(stream_bytes()) + len(session_bytes()))

    def test_every_row_carries_the_job_fields(self):
        self.tree.stream()
        entries, _ = self.discover()
        self.archive(entries)
        rel = entries[0].rel
        rows = self.client.rows[rel]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["ws"], "agentq/workflow-skills")
            self.assertEqual(row["label"], LABEL)
            self.assertEqual(row["kind"], "stream")
            self.assertEqual(row["job_id"], JOB)
            self.assertEqual(row["job_kind"], "write")
            self.assertEqual(row["tier"], "pi-deepseek-flash")
            self.assertEqual(row["verdict"], "PASS")
            self.assertEqual(row["session"], "6eaeced1-b770-4937-b3eb-0edb03574081")
            self.assertEqual(row["attempt"], "0")

    def test_a_gzip_stream_is_removed_after_its_verified_archive(self):
        gz = self.tree.stream(name="stream-%s.ndjson.gz" % LABEL, gz=True)
        entries, _ = self.discover()
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual((rc, results["reclaimed"]), (0, 1))
        self.assertFalse(gz.exists())
        self.assertEqual(len(self.client.rows[entries[0].rel]), 4)

    def test_a_rerun_neither_reposts_nor_rereads_a_done_file(self):
        self.tree.stream()
        entries, _ = self.discover()
        self.archive(entries)
        posts = self.client.posts
        again, _ = self.discover()
        rc, _totals, results, _plans = self.archive(again)
        self.assertEqual(rc, 0)
        self.assertEqual(self.client.posts, posts)
        self.assertEqual(results["archived"], 0)
        self.assertEqual(results["done"], 1)

    def test_a_dry_run_writes_nothing(self):
        stream = self.tree.stream()
        before = stream.read_bytes()
        entries, _ = self.discover()
        rc, totals, results, _plans = self.archive(entries, apply=False, client=None)
        self.assertEqual(rc, 0)
        self.assertEqual(results["planned"], 1)
        self.assertGreater(totals.reclaimable, 0)
        self.assertEqual(stream.read_bytes(), before)
        self.assertFalse((self.tree.state / "log-archive").exists())
        self.assertEqual(self.client.posts, 0)

    def test_an_unreachable_store_keeps_every_file(self):
        stream = self.tree.stream()
        entries, _ = self.discover()
        rc, _totals, results, _plans = la.archive_entries(
            entries, apply=True, state=self.tree.state, now=NOW, url="http://127.0.0.1:1"
        )
        self.assertEqual(rc, 5)
        self.assertEqual(results["store"], "unreachable")
        self.assertTrue(stream.is_file())


class BatchTest(Base):
    """Many files, one store flush: a flush per small file is what made the pass crawl.

    Each flush makes VictoriaLogs seal a part and then merge it, so the pass
    posts a batch of files and flushes once (2026-11-21: ~1 file per 15 s with
    ~27,000 queued). The safety rules are per file, inside the batch: the store
    is still asked per path what it already has, and a file whose verify fails
    is recorded as an error and left alone while its batch mates are reclaimed.
    """

    def three_files(self):
        """Two streams and a session: three files, two token types, one batch."""
        self.tree.stream(name="stream-%s-a.ndjson" % LABEL)
        self.tree.stream(name="stream-%s-b.ndjson" % LABEL)
        self.tree.session(name="s1.jsonl")
        entries, _skipped = self.discover()
        self.assertEqual(len(entries), 3)
        return entries

    def test_one_flush_covers_a_whole_batch(self):
        entries = self.three_files()
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual((rc, results["archived"], results["reclaimed"], results["errors"]), (0, 3, 3, 0))
        self.assertEqual(self.client.flushes, 1)

    def test_a_short_batch_flushes_per_batch_not_per_file(self):
        entries = self.three_files()
        rc, _totals, results, _plans = self.archive(entries, batch_files=2)
        self.assertEqual((rc, results["reclaimed"], results["errors"]), (0, 3, 0))
        self.assertEqual(self.client.flushes, 2)

    def test_the_batch_asks_the_store_once_for_the_whole_batch(self):
        """Before-count, row count and token sums: three grouped queries per batch, not per file."""
        entries = self.three_files()
        self.archive(entries)
        # one before-count + one row count + one token query per token type.
        self.assertEqual(self.client.queries, 4)

    def test_a_store_too_old_for_in_falls_back_to_one_query_per_path(self):
        class NoIn(FakeClient):
            def query(self, logsql):
                if "path:in(" in logsql:
                    raise la.vlogs.VLogsError("cannot parse `in(`")
                return super().query(logsql)

        self.client = NoIn()
        entries = self.three_files()
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual((rc, results["reclaimed"], results["errors"]), (0, 3, 0))
        for entry in entries:
            self.assertTrue(self.client.rows[entry.rel])

    def test_a_file_whose_verify_fails_is_kept_and_the_rest_are_reclaimed(self):
        entries = self.three_files()
        bad = entries[0].rel

        class LosesOne(FakeClient):
            def query(self, logsql):
                rows = super().query(logsql)
                out = []
                for row in rows:
                    if row.get("path") == bad:
                        continue  # the store never counts this path's rows
                    out.append(row)
                return out

        self.client = LosesOne()
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual(rc, 1)
        self.assertEqual((results["archived"], results["reclaimed"], results["errors"]), (2, 2, 1))
        latest = la.Manifest(la.archive_root(self.tree.state)).latest
        self.assertEqual(latest[bad]["status"], "error")
        self.assertIn("expected 3", latest[bad]["error"])
        others = [e for e in entries if e.rel != bad]
        self.assertEqual([latest[e.rel]["status"] for e in others], ["reclaimed"] * 2)
        # The rewritten files are still there: a failed batch mate never stops a reclaim.
        self.assertTrue(all(Path(e.path).is_file() for e in others))

    def test_a_partial_store_refuses_that_file_only(self):
        entries = self.three_files()
        bad = entries[0]
        self.client.rows[bad.rel] = [{"path": bad.rel, "type": "message_end", "_msg": "{}", "role": "user"}]
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual(rc, 1)
        self.assertEqual((results["reclaimed"], results["errors"]), (2, 1))
        self.assertTrue(Path(bad.path).is_file())
        self.assertEqual(len(self.client.rows[bad.rel]), 1)  # nothing was posted for it

    def test_a_batch_retries_cleanly_after_a_crash_between_post_and_reclaim(self):
        """Rows already in the store: no second POST, no second row, still verified and reclaimed."""
        entries = self.three_files()
        for entry in entries:
            for line, _acct in la.row_lines(entry, self._zst(entry)):
                self.client.insert(iter([line]))
        posts = self.client.posts
        rc, _totals, results, _plans = self.archive(entries)
        self.assertEqual((rc, results["reclaimed"], results["errors"]), (0, 3, 0))
        self.assertEqual(self.client.posts, posts)
        self.assertEqual(self.client.flushes, 0)

    def _zst(self, entry):
        """The stripped zstd copy of one entry, written straight into the archive tree."""
        root = la.archive_root(self.tree.state)
        zst = la.zst_path_for(root, entry)
        zst.parent.mkdir(parents=True, exist_ok=True)
        m = la.measure(entry.path, str(zst), la.token_type(entry.kind), compressed=entry.compressed)
        la._verify_measure(m)
        return str(zst)


class ArchiveStatusTest(Base):
    def test_status_is_none_before_and_reclaimed_after(self):
        self.tree.stream()
        entries, _ = self.discover()
        self.assertEqual(la.archive_status(entries, state=self.tree.state), {entries[0].rel: "none"})
        self.archive(entries)
        # A caller re-reads the tree: the stripped rewrite changed size and mtime.
        again, _ = self.discover()
        self.assertEqual(la.archive_status(again, state=self.tree.state), {again[0].rel: "reclaimed"})
        # A stale entry (measured before the rewrite) matches nothing: the safe answer.
        self.assertEqual(la.archive_status(entries, state=self.tree.state), {entries[0].rel: "none"})

    def test_a_changed_file_has_no_verified_archive(self):
        stream = self.tree.stream()
        entries, _ = self.discover()
        self.archive(entries)
        stream.write_bytes(stream_bytes() + b'{"type":"turn_end"}\n')
        os.utime(stream, (NOW, NOW))
        again, _ = self.discover()
        self.assertEqual(la.archive_status(again, state=self.tree.state), {again[0].rel: "none"})


class GcTest(Base):
    """``agentq gc`` archives first: it never unlinks a file without a verified archive."""

    def plan(self, **kw):
        return G.plan(self.tree.home, [job_row()], older_than_s=WEEK, now=NOW, **kw)

    def test_the_plan_carries_the_job_row(self):
        self.tree.stream()
        items, _skipped = self.plan()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["job"]["label"], LABEL)

    def test_apply_archives_then_reclaims(self):
        stream = self.tree.stream()
        items, _skipped = self.plan()
        report = G.archive_and_reclaim(
            self.tree.home, items, apply=True, state=self.tree.state, client=self.client, now=NOW
        )
        self.assertEqual((report["archived"], report["reclaimed"], report["errors"]), (1, 1, 0))
        self.assertTrue(stream.is_file())  # pi stream: stripped, not removed
        self.assertNotIn(b"QUJDREVGR0hJ", stream.read_bytes())
        self.assertEqual(len(self.client.rows[items[0]["path"] and la.agentq_entry(
            items[0]["path"], items[0]["job"], self.tree.home).rel]), 4)

    def test_an_unreachable_store_deletes_nothing(self):
        stream = self.tree.stream()
        items, _skipped = self.plan()
        report = G.archive_and_reclaim(
            self.tree.home, items, apply=True, state=self.tree.state, url="http://127.0.0.1:1", now=NOW
        )
        self.assertEqual(report["exit"], 5)
        self.assertEqual(report["reclaimed"], 0)
        self.assertTrue(stream.is_file())

    def test_a_path_outside_the_job_tree_is_refused(self):
        victim = self.base / "victim" / "runs" / "run1" / "pi" / "stream-victim.ndjson"
        victim.parent.mkdir(parents=True)
        victim.write_bytes(stream_bytes())
        items = [{"job_id": JOB, "job": job_row(), "root": str(self.tree.home / "jobs" / JOB / "runs"),
                  "path": str(victim), "size": victim.stat().st_size, "finished_at": NOW - 2 * WEEK}]
        report = G.archive_and_reclaim(
            self.tree.home, items, apply=True, state=self.tree.state, client=self.client, now=NOW
        )
        self.assertEqual(report["entries"], 0)
        self.assertEqual(report["refused"][0]["error"], "outside the job runs tree")
        self.assertTrue(victim.exists())
        self.assertEqual(self.client.posts, 0)

    def test_status_reports_what_still_needs_archiving(self):
        self.tree.stream()
        items, _skipped = self.plan()
        self.assertEqual(set(G.archive_status(self.tree.home, items, state=self.tree.state).values()), {"none"})
        G.archive_and_reclaim(
            self.tree.home, items, apply=True, state=self.tree.state, client=self.client, now=NOW
        )
        self.assertEqual(
            set(G.archive_status(self.tree.home, items, state=self.tree.state).values()), {"reclaimed"}
        )


class ScheduledTest(unittest.TestCase):
    def test_an_unknown_argument_is_refused(self):
        import agent_dash

        self.assertEqual(agent_dash.cmd_logs_scheduled(["--nope"]), 2)
        self.assertEqual(agent_dash.cmd_logs_scheduled(["--url"]), 2)

    def test_the_hourly_pass_runs_sessions_then_agentq(self):
        """One process, two passes: the plist's taskpolicy and memcap cover both."""
        import agent_dash
        from dash import sessionarchive

        calls = []

        def fake_sessions(opts):
            calls.append(("sessions", dict(opts)))
            return 0

        def fake_archive(opts):
            calls.append(("archive", dict(opts)))
            return 5

        with mock.patch.object(sessionarchive, "run", fake_sessions), mock.patch.object(
            la, "run", fake_archive
        ):
            rc = agent_dash.cmd_logs_scheduled(["--apply"])

        self.assertEqual([name for name, _ in calls], ["sessions", "archive"])
        self.assertEqual(calls[0][1], {"apply": True, "scheduled": True, "url": None})
        self.assertEqual(calls[1][1]["agentq"], True)
        self.assertEqual(calls[1][1]["apply"], True)
        self.assertEqual(calls[1][1]["scheduled"], True)
        # The worse of the two, so a store outage is not hidden by a clean session pass.
        self.assertEqual(rc, 5)


if __name__ == "__main__":
    unittest.main()
