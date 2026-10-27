"""Tests for dash.sources.manifests. Synthetic fixtures plus a bounded real-file smoke."""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash.sources import manifests  # noqa: E402
from dash.store import Store  # noqa: E402

PARENT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
MUSE = "11111111-1111-1111-1111-111111111111"
GLM = "22222222-2222-2222-2222-222222222222"
NOW = 1_792_000_000
SECRET = "SECRET_PROMPT"
ERR_TEXT = "UNIQUE_ERR_TEXT"


class MemStore:
    def __init__(self):
        self.offsets = {}
        self.runs = {}

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset)

    def upsert_run(self, row):
        self.runs[row["run_id"]] = row


def _write(path, data, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))


def _dump(store):
    return json.dumps(list(store.runs.values()), default=str)


class ManifestUnitTests(unittest.TestCase):
    def test_parent_session_from_pad(self):
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        base = os.path.join(os.sep, "tmp", "claude-1000", "-proj", sid, "scratchpad")
        self.assertEqual(
            manifests.parent_session_from_pad(os.path.join(base, "pi")), sid
        )
        self.assertEqual(
            manifests.parent_session_from_pad(os.path.join(base, "cmd")), sid
        )
        self.assertEqual(manifests.parent_session_from_pad(base), sid)
        self.assertIsNone(manifests.parent_session_from_pad(os.path.join(os.sep, "tmp", "flat")))
        self.assertIsNone(manifests.parent_session_from_pad(os.path.join(base, "..", "other")))
        self.assertIsNone(manifests.parent_session_from_pad(None))
        self.assertIsNone(manifests.parent_session_from_pad(""))

    def test_parse_tsv_header_cursor_and_glm(self):
        text = "\n".join(
            [
                "label\tsession_id\tstarted",
                "note-row\t-\t1700000003",
                "rev-ws\t22222222-2222-2222-2222-222222222222\t1700000002\tglm-5.3",
                "skip-me",
                "# comment",
            ]
        )
        rows = manifests.parse_manifest_text(text)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["session_uuid"])
        self.assertEqual(rows[0]["label"], "note-row")
        self.assertEqual(rows[0]["started_at"], 1700000003)
        self.assertEqual(rows[1]["model"], "glm-5.3")
        self.assertEqual(rows[1]["session_uuid"], "22222222-2222-2222-2222-222222222222")

    def test_cursor_model_column_parses(self):
        text = "\n".join(
            [
                "ws8-WS8.1-budget\t-\t1792243358\tgrok-4.7-high",
                "ws8-WS8.21-reserve\t-\t1792243361",
            ]
        )
        rows = manifests.parse_manifest_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "grok-4.7-high")
        self.assertIsNone(rows[0]["session_uuid"])
        self.assertIsNone(rows[1]["model"])

    def test_verdict_regex(self):
        self.assertEqual(manifests.verdict_of("PASS\n"), "PASS")
        self.assertEqual(manifests.verdict_of("PASS. more text\n"), "PASS")
        self.assertEqual(manifests.verdict_of("PARTIAL. gap\n"), "PARTIAL")
        self.assertEqual(manifests.verdict_of("BLOCKED\n"), "BLOCKED")
        self.assertEqual(manifests.verdict_of("VERDICT: APPROVE\n"), "APPROVE")
        self.assertEqual(
            manifests.verdict_of("VERDICT: APPROVE_WITH_CHANGES\n"),
            "APPROVE_WITH_CHANGES",
        )
        self.assertEqual(
            manifests.verdict_of("VERDICT: REQUEST_CHANGES\n"), "REQUEST_CHANGES"
        )
        self.assertEqual(manifests.verdict_of("narration\nPASS\n"), "PASS")
        self.assertIsNone(manifests.verdict_of("The model said hello\n"))
        self.assertIsNone(manifests.verdict_of("VERDICT:APPROVE\n"))

    def test_classify_matrix(self):
        base = dict(err_has_sigterm=False, err_nonempty=False, err_known=True)
        self.assertEqual(
            manifests.classify_status(
                out_bytes=10, prev_out_bytes=0, label_alive=False, **base
            ),
            "running",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=True,
                err_has_sigterm=True,
                err_nonempty=True,
            ),
            "running",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=None,
                err_has_sigterm=True,
                err_nonempty=True,
            ),
            "killed",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=False,
                err_has_sigterm=False,
                err_nonempty=True,
            ),
            "died_early",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=None,
                err_has_sigterm=False,
                err_nonempty=True,
            ),
            "unknown",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=None,
                err_has_sigterm=False,
                err_nonempty=False,
            ),
            "unknown",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=20,
                prev_out_bytes=None,
                label_alive=None,
                err_has_sigterm=False,
                err_nonempty=False,
            ),
            "done",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=20,
                prev_out_bytes=20,
                label_alive=False,
                err_has_sigterm=False,
                err_nonempty=False,
            ),
            "done",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=0,
                prev_out_bytes=None,
                label_alive=False,
                err_has_sigterm=False,
                err_nonempty=False,
            ),
            "done",
        )
        self.assertEqual(
            manifests.classify_status(
                out_bytes=8,
                prev_out_bytes=8,
                label_alive=None,
                err_has_sigterm=False,
                err_nonempty=False,
                err_known=False,
            ),
            "unknown",
        )


class ManifestPollTests(unittest.TestCase):
    def setUp(self):
        manifests.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()
        self.root = self.tmp.name
        self.scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")

    def tearDown(self):
        self.tmp.cleanup()
        manifests.reset_state()

    def _cfg(self, **extra):
        cfg = {
            "scratch_root": self.root,
            # Isolate from the machine's registered run folders: without this,
            # scan_roots picks up live run-dirs.txt entries and leaks foreign
            # rows (and read budget) into these hermetic tests.
            "state_dir": os.path.join(self.root, "state"),
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }
        cfg.update(extra)
        return cfg

    def test_poll_maps_rows_and_ignores_nested_and_prompts(self):
        _write(
            os.path.join(self.scratch, "muse-sessions.tsv"),
            f"alpha\t{MUSE}\t1700000000\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "glm-sessions.tsv"),
            f"rev\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "cursor-sessions.tsv"),
            "dash-preview\t-\t1700000001\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "grok-sessions.tsv"),
            "label\tsession_id\tstarted\nnote-row\t-\t1700000003\tgrok-4.7-high\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "out-alpha.md"),
            f"PASS\n{SECRET}\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "err-alpha.log"),
            f"{ERR_TEXT}\nreceived SIGTERM\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "alpha.md"),
            f"{SECRET}\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "out-DECOY.md"),
            "PASS\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "nested", "out-NESTED.md"),
            "PASS\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "head-check", "out-DECOY.md"),
            f"PASS\n{SECRET}\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "cursor", "out-dash-preview.md"),
            "notes only\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "exec", "out-job.md"),
            "BLOCKED\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "exec", "nested", "out-INNER.md"),
            "PASS\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "exec", "out-old.md"),
            "PASS\n",
            NOW - 40 * 3600,
        )
        ignored = os.path.join(self.root, "enc", "not-a-session", "scratchpad")
        _write(
            os.path.join(ignored, "muse-sessions.tsv"),
            "ignored-session\t-\t1700000099\n",
            NOW,
        )
        manifests.poll(self.store, self._cfg(), NOW)
        blob = _dump(self.store)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(ERR_TEXT, blob)
        labels = {row["label"] for row in self.store.runs.values()}
        self.assertNotIn("DECOY", labels)
        self.assertNotIn("NESTED", labels)
        self.assertNotIn("INNER", labels)
        self.assertNotIn("old", labels)
        self.assertNotIn("ignored-session", labels)

        muse = self.store.runs[f"muse:{MUSE}:alpha"]
        self.assertEqual(muse["source"], "muse")
        self.assertEqual(muse["tier"], "muse")
        self.assertEqual(muse["parent_session_id"], PARENT)
        self.assertEqual(muse["status"], "killed")
        self.assertEqual(muse["verdict"], "PASS")
        self.assertGreater(muse["out_bytes"], 0)
        self.assertGreater(muse["err_bytes"], 0)

        glm = self.store.runs[f"glm:{GLM}:rev"]
        self.assertEqual(glm["model"], "glm-5.3")
        self.assertEqual(glm["tier"], "glm")
        self.assertEqual(glm["parent_session_id"], PARENT)
        self.assertEqual(glm["status"], "unknown")

        cursor = self.store.runs[f"grok:{PARENT}:cursor/dash-preview@1700000001"]
        self.assertEqual(cursor["source"], "grok")
        self.assertEqual(cursor["tier"], "grok")
        self.assertIsNone(cursor.get("session_id"))
        self.assertIsNone(cursor.get("verdict"))
        self.assertEqual(cursor["status"], "done")

        grok = self.store.runs[f"grok:{PARENT}:note-row@1700000003"]
        self.assertEqual(grok["source"], "grok")
        self.assertNotIn("cursor/", grok["run_id"])

        shell = self.store.runs[f"shell:{PARENT}:job"]
        self.assertEqual(shell["source"], "shell")
        self.assertEqual(shell["verdict"], "BLOCKED")
        self.assertEqual(shell["clock_source"], "mtime")
        self.assertEqual(shell["status"], "done")

    def test_growth_live_label_and_died_early(self):
        _write(
            os.path.join(self.scratch, "muse-sessions.tsv"),
            f"alpha\t{MUSE}\t1700000000\nbeta\t{MUSE}\t1700000001\n",
            NOW,
        )
        out = os.path.join(self.scratch, "muse", "out-alpha.md")
        err = os.path.join(self.scratch, "muse", "err-beta.log")
        _write(out, b"", NOW)
        _write(err, b"boom\n", NOW)
        store = MemStore()
        manifests.poll(store, self._cfg(), NOW)
        # An empty out file with no live process is zero evidence: no
        # placeholder row opens for alpha until its file grows. Beta's
        # error content is evidence, so it reads unknown.
        self.assertNotIn(f"muse:{MUSE}:alpha", store.runs)
        self.assertEqual(store.runs[f"muse:{MUSE}:beta"]["status"], "unknown")

        with open(out, "ab") as fh:
            fh.write(b"still going\n")
        os.utime(out, (NOW + 2, NOW + 2))
        manifests.poll(store, self._cfg(), NOW + 2)
        self.assertEqual(store.runs[f"muse:{MUSE}:alpha"]["status"], "running")

        store.live_labels = lambda: []
        manifests.poll(store, self._cfg(), NOW + 3)
        self.assertEqual(store.runs[f"muse:{MUSE}:beta"]["status"], "died_early")

        store.live_labels = lambda: ["beta"]
        manifests.poll(store, self._cfg(), NOW + 4)
        self.assertEqual(store.runs[f"muse:{MUSE}:beta"]["status"], "running")

    def test_process_samples_ttl(self):
        _write(
            os.path.join(self.scratch, "muse-sessions.tsv"),
            f"alpha\t{MUSE}\t1700000000\n",
            NOW,
        )
        _write(os.path.join(self.scratch, "muse", "out-alpha.md"), b"x", NOW)

        class Sampled(MemStore):
            def process_samples(self):
                return [{"label": "alpha", "ts": NOW - 5, "pid": 9}]

        store = Sampled()
        manifests.poll(store, self._cfg(process_ttl_s=30), NOW)
        self.assertEqual(store.runs[f"muse:{MUSE}:alpha"]["status"], "running")

        class Stale(MemStore):
            def process_samples(self):
                return [{"label": "alpha", "ts": NOW - 500}]

        stale = Stale()
        manifests.reset_state()
        manifests.poll(stale, self._cfg(process_ttl_s=30), NOW)
        self.assertEqual(stale.runs[f"muse:{MUSE}:alpha"]["status"], "done")

    def test_report_fallback_and_out_wins(self):
        _write(
            os.path.join(self.scratch, "muse-sessions.tsv"),
            f"alpha\t{MUSE}\t1700000000\nbeta\t{MUSE}\t1700000004\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "out-alpha.md"),
            "PASS\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "report-alpha.md"),
            "BLOCKED\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "out-beta.md"),
            "The model said hello\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "muse", "report-beta.md"),
            "VERDICT: APPROVE_WITH_CHANGES\n",
            NOW,
        )
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self.store.runs[f"muse:{MUSE}:alpha"]["verdict"], "PASS")
        self.assertEqual(
            self.store.runs[f"muse:{MUSE}:beta"]["verdict"], "APPROVE_WITH_CHANGES"
        )

    def test_budget_retries_verdict(self):
        tsv = f"alpha\t{MUSE}\t1700000000\n".encode()
        _write(os.path.join(self.scratch, "muse-sessions.tsv"), tsv, NOW)
        _write(
            os.path.join(self.scratch, "muse", "out-alpha.md"),
            b"PASS\n" + (b"x" * 40),
            NOW,
        )
        cfg = self._cfg()
        cfg["read_budgets"] = {"per_file": 1024 * 1024, "per_tick": len(tsv) + 1}
        manifests.poll(self.store, cfg, NOW)
        run = self.store.runs[f"muse:{MUSE}:alpha"]
        self.assertIsNone(run.get("verdict"))
        manifests.poll(self.store, cfg, NOW + 1)
        self.assertEqual(self.store.runs[f"muse:{MUSE}:alpha"]["verdict"], "PASS")

    def test_encoded_root_attributes_glm_sessions(self):
        enc = os.path.join(self.root, "enc")
        _write(
            os.path.join(self.scratch, "glm-sessions.tsv"),
            f"rev\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        manifests.poll(self.store, self._cfg(scratch_root=enc), NOW)
        glm = self.store.runs[f"glm:{GLM}:rev"]
        self.assertEqual(glm["model"], "glm-5.3")
        self.assertEqual(glm["source"], "glm")
        self.assertEqual(glm["tier"], "glm")
        self.assertEqual(glm["parent_session_id"], PARENT)

    def test_backfill_includes_old_exec(self):
        _write(
            os.path.join(self.scratch, "exec", "out-old.md"),
            b"PASS\n",
            NOW - 40 * 3600,
        )
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self.store.runs, {})
        manifests.poll(
            self.store, self._cfg(mode="backfill", session_id=PARENT), NOW
        )
        self.assertEqual(self.store.runs[f"shell:{PARENT}:old"]["verdict"], "PASS")

    def test_other_session_not_mixed_into_backfill(self):
        other = os.path.join(self.root, "enc", OTHER, "scratchpad")
        _write(
            os.path.join(other, "muse-sessions.tsv"),
            f"alpha\t{MUSE}\t1700000000\n",
            NOW,
        )
        manifests.poll(self.store, self._cfg(backfill_session=PARENT), NOW)
        self.assertEqual(self.store.runs, {})

    def test_poll_reads_a_registered_flat_run_dir(self):
        rundir = os.path.join(self.root, "runs", "ws9")
        _write(
            os.path.join(rundir, "glm-sessions.tsv"),
            f"ws9-WS9.1-build\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        _write(
            os.path.join(rundir, "out-ws9-WS9.1-build.md"),
            f"PASS\n{SECRET}\n",
            NOW,
        )
        _write(
            os.path.join(rundir, "err-ws9-WS9.1-build.log"),
            "",
            NOW,
        )
        state = os.path.join(self.root, "state")
        os.makedirs(state)
        with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
            fh.write(rundir + "\n")
            fh.write(os.path.join(self.root, "gone") + "\n")
        manifests.poll(self.store, self._cfg(state_dir=state), NOW)
        glm = self.store.runs[f"glm:{GLM}:ws9-WS9.1-build"]
        self.assertEqual(glm["source"], "glm")
        self.assertEqual(glm["tier"], "glm")
        self.assertEqual(glm["label"], "ws9-WS9.1-build")
        self.assertEqual(glm["status"], "done")
        self.assertEqual(glm["verdict"], "PASS")
        self.assertEqual(glm["ended_at"], NOW)
        self.assertNotIn(SECRET, _dump(self.store))

    def test_poll_reads_grok_build_tsv_in_registered_flat_run_dir(self):
        # A grok-build-47 launch writes grok-build-sessions.tsv (registry
        # manifest override) instead of glm-sessions.tsv, with the pid
        # file, meta file, and logs beside it. The flat run-dir scan must
        # read that name, keep the recorded tier, and take the finished
        # verdict from the out file.
        label = "ws19-exec-grokbuild-visible-gfix"
        rundir = os.path.join(self.root, "runs", "ws19grokb")
        _write(
            os.path.join(rundir, "grok-build-sessions.tsv"),
            f"{label}\t{GLM}\t1700000002\tgrok-4.7\tgrok-build-47\tgrok-cli\n",
            NOW,
        )
        _write(
            os.path.join(rundir, f"out-{label}.md"),
            "PASS\nsynthetic report body\n",
            NOW,
        )
        _write(
            os.path.join(rundir, f"err-{label}.log"),
            "",
            NOW,
        )
        _write(os.path.join(rundir, f"{label}.pid"), "987654321\n", NOW)
        _write(
            os.path.join(rundir, f"{label}.meta.json"),
            json.dumps(
                {
                    "err_path": os.path.join(rundir, f"err-{label}.log"),
                    "harness": "grok-cli",
                    "label": label,
                    "model": "grok-4.7",
                    "out_path": os.path.join(rundir, f"out-{label}.md"),
                    "pid": 987654321,
                    "started_at": 1700000002,
                    "tier": "grok-build-47",
                }
            ),
            NOW,
        )
        state = os.path.join(self.root, "state")
        os.makedirs(state)
        with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
            fh.write(rundir + "\n")
        manifests.poll(self.store, self._cfg(state_dir=state), NOW)
        run = self.store.runs[f"glm:{GLM}:{label}"]
        self.assertEqual(run["source"], "glm")
        self.assertEqual(run["tier"], "grok-build-47")
        self.assertEqual(run["model"], "grok-4.7")
        self.assertEqual(run["label"], label)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["verdict"], "PASS")
        self.assertEqual(run["ended_at"], NOW)

    def test_poll_prefers_last_seen_alive_over_file_mtime(self):
        rundir = os.path.join(self.root, "runs", "ws9b")
        _write(
            os.path.join(rundir, "glm-sessions.tsv"),
            f"ws9b-build\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        _write(
            os.path.join(rundir, "out-ws9b-build.md"),
            "notes only\n",
            NOW - 900,
        )
        state = os.path.join(self.root, "state")
        os.makedirs(state)
        with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
            fh.write(rundir + "\n")

        class Seen(MemStore):
            def last_activity(self, run_id, started_at=None):
                return NOW - 100

        store = Seen()
        manifests.poll(store, self._cfg(state_dir=state), NOW)
        run = store.runs[f"glm:{GLM}:ws9b-build"]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["ended_at"], NOW - 100)

    def test_poll_stays_running_on_a_fresh_sighting(self):
        rundir = os.path.join(self.root, "runs", "ws9c")
        _write(
            os.path.join(rundir, "glm-sessions.tsv"),
            f"ws9c-build\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        _write(
            os.path.join(rundir, "out-ws9c-build.md"),
            "notes only\n",
            NOW - 900,
        )
        state = os.path.join(self.root, "state")
        os.makedirs(state)
        with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
            fh.write(rundir + "\n")

        class Seen(MemStore):
            def last_activity(self, run_id, started_at=None):
                return NOW

        store = Seen()
        manifests.poll(store, self._cfg(state_dir=state), NOW)
        run = store.runs[f"glm:{GLM}:ws9c-build"]
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run.get("ended_at"))


class ManifestAliasRestartTests(unittest.TestCase):
    """A merged cursor row stays merged when the TSV is re-read after a restart."""

    LABEL = "ws6-c11-restart"
    CHAT_UUID = "e33e33e3-3e33-4e33-8e33-e33e33e33e33"
    START = 1700000001
    MANIFEST = f"grok:{PARENT}:cursor/{LABEL}@{START}"
    CHAT = f"grok:{CHAT_UUID}:{LABEL}"

    def setUp(self):
        manifests.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")
        self.store = Store(os.path.join(self.root, "dash.sqlite"))
        _write(
            os.path.join(self.scratch, "cursor-sessions.tsv"),
            f"{self.LABEL}\t-\t{self.START}\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "cursor", f"out-{self.LABEL}.md"),
            "PASS\n",
            NOW,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        manifests.reset_state()

    def _cfg(self):
        return {
            "scratch_root": self.root,
            # Isolate from the machine's registered run folders (see above).
            "state_dir": os.path.join(self.root, "state"),
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }

    def _add_chat(self):
        self.store.upsert_run(
            {
                "run_id": self.CHAT,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.CHAT_UUID,
                "status": "done",
                "started_at": float(self.START + 2),
                "ended_at": float(self.START + 100),
                "clock_source": "transcript",
            }
        )

    def _manifest_rows(self):
        return [
            row[0]
            for row in self.store._conn.execute(
                "SELECT run_id FROM runs WHERE run_id LIKE 'grok:%:cursor/%'"
            ).fetchall()
        ]

    def _duplicates(self):
        report = self.store.reconcile_numbers(float(NOW + 10), None, None)
        return [item for item in report["mismatches"] if "duplicate run" in item]

    def test_merged_row_survives_a_restart_reread(self):
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self._manifest_rows(), [self.MANIFEST])
        self._add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertEqual(self._manifest_rows(), [])
        # Simulated restart: in-memory source state is gone, the TSV is not.
        manifests.reset_state()
        manifests.poll(self.store, self._cfg(), NOW + 1)
        self.assertEqual(self._manifest_rows(), [])
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(
            chat["out_path"],
            os.path.join(self.scratch, "cursor", f"out-{self.LABEL}.md"),
        )
        self.assertEqual(self.store.resolve_run_id(self.MANIFEST), self.CHAT)
        self.assertEqual(self._duplicates(), [])

    def test_reread_with_stored_twin_creates_no_row(self):
        self._add_chat()
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self._manifest_rows(), [])
        self.assertEqual(self.store.merge_grok_manifests(), 0)
        self.assertEqual(self.store.resolve_run_id(self.MANIFEST), self.CHAT)
        manifests.reset_state()
        manifests.poll(self.store, self._cfg(), NOW + 1)
        self.assertEqual(self._manifest_rows(), [])
        self.assertEqual(self._duplicates(), [])


class ManifestQuotaAndSupersedeTests(unittest.TestCase):
    def setUp(self):
        manifests.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()
        self.root = self.tmp.name
        self.scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")

    def tearDown(self):
        self.tmp.cleanup()
        manifests.reset_state()

    def _emit(self, label, session_id, started, files, live, **kwargs):
        base = os.path.join(self.scratch, "glm")
        params = {"out_path": None, "err_path": None, "report_path": None}
        for kind, name in (("out", f"out-{label}.md"), ("err", f"err-{label}.log")):
            if name in files:
                path = os.path.join(base, name)
                _write(path, files[name], NOW)
                params["out_path" if kind == "out" else "err_path"] = path
        run_id = f"glm:{session_id}:{label}"
        left = manifests._emit_files(
            self.store,
            run_id=run_id,
            source="glm",
            tier="glm",
            model="glm-5.3",
            label=label,
            session_id=session_id,
            parent=PARENT,
            started_at=started,
            clock="manifest",
            out_path=params["out_path"],
            err_path=params["err_path"],
            report_path=params["report_path"],
            left=1024 * 1024,
            per_file=1024 * 1024,
            live=live,
            now=NOW,
            ttl=30,
            **kwargs,
        )
        self.assertGreaterEqual(left, 0)
        return self.store.runs[run_id]

    def test_quota_err_closes_running_row_as_died(self):
        row = self._emit(
            "quota-lane",
            GLM,
            1700000002,
            {"out-quota-lane.md": "working\n", "err-quota-lane.log": "Usage limit reached for 5 hour\n"},
            live={"quota-lane"},
            pid_file=4242,
            quota_hit=True,
            superseded=False,
        )
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "QUOTA")
        self.assertEqual(row["pid"], 4242)
        self.assertIsNotNone(row.get("ended_at"))

    def test_quota_applies_to_newest_attempt_only(self):
        row = self._emit(
            "quota-lane",
            GLM,
            1700000002,
            {"out-quota-lane.md": "working\n", "err-quota-lane.log": "Usage limit reached for 5 hour\n"},
            live={"quota-lane"},
            pid_file=4242,
            quota_hit=True,
            superseded=True,
        )
        # The superseded row still closes (its growth belongs to the newer
        # attempt) but the quota verdict and pid stay with the newest row.
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row.get("verdict"))
        self.assertIsNone(row.get("pid"))

    def test_superseded_running_row_closes_without_quota(self):
        row = self._emit(
            "old-lane",
            GLM,
            1700000002,
            {"out-old-lane.md": "working\n"},
            live={"old-lane"},
            pid_file=4242,
            quota_hit=False,
            superseded=True,
        )
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row.get("pid"))
        self.assertIsNotNone(row.get("ended_at"))

    def test_pid_file_adopted_on_newest_attempt_only(self):
        fresh = self._emit(
            "pid-lane", GLM, 1700000002, {"out-pid-lane.md": "working\n"},
            live=set(), pid_file=4242, quota_hit=False, superseded=False,
        )
        self.assertEqual(fresh["pid"], 4242)
        stale = self._emit(
            "pid-lane", OTHER, 1700000001, {"out-pid-lane.md": "working\n"},
            live=set(), pid_file=4242, quota_hit=False, superseded=True,
        )
        self.assertIsNone(stale.get("pid"))

    def test_poll_quota_err_marks_glm_row_died(self):
        _write(
            os.path.join(self.scratch, "glm-sessions.tsv"),
            f"quota-poll\t{GLM}\t1700000002\tglm-5.3\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "glm", "out-quota-poll.md"),
            "working\n",
            NOW,
        )
        _write(
            os.path.join(self.scratch, "glm", "err-quota-poll.log"),
            "Usage limit reached for 5 hour\n",
            NOW,
        )
        cfg = {
            "scratch_root": self.root,
            # Isolate from the machine's registered run folders (see above).
            "state_dir": os.path.join(self.root, "state"),
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }
        manifests.poll(self.store, cfg, NOW)
        row = self.store.runs[f"glm:{GLM}:quota-poll"]
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "QUOTA")


class ManifestCodexTests(unittest.TestCase):
    def setUp(self):
        manifests.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemStore()
        self.root = self.tmp.name
        self.scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")

    def tearDown(self):
        self.tmp.cleanup()
        manifests.reset_state()

    def _cfg(self, **extra):
        cfg = {
            "scratch_root": self.root,
            # Isolate from the machine's registered run folders (see above).
            "state_dir": os.path.join(self.root, "state"),
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }
        cfg.update(extra)
        return cfg

    def test_codex_manifest_is_scanned(self):
        self.assertIn(("codex-sessions.tsv", "codex", "codex", "codex"), manifests._MANIFESTS)

    def test_tier_of_maps_gpt_models_to_codex(self):
        self.assertEqual(manifests.tier_of("gpt-6-luna"), "codex")
        self.assertEqual(manifests.tier_of("gpt-6.1-sol"), "codex")
        self.assertEqual(manifests.tier_of("muse-spark-1.3-contributor"), "muse")
        self.assertIsNone(manifests.tier_of(None))

    def test_poll_codex_row_reads_pass_and_model(self):
        sid = "01a16401-9955-70cd-bf8c-19944db4f20c"
        _write(
            os.path.join(self.scratch, "codex-sessions.tsv"),
            f"pass-poll\t{sid}\t1700000002\tgpt-6-luna\tcodex-luna\n",
            NOW,
        )
        _write(os.path.join(self.scratch, "codex", "out-pass-poll.md"), "PASS\nbuilt it\n", NOW)
        _write(os.path.join(self.scratch, "codex", "err-pass-poll.log"), "", NOW)
        manifests.poll(self.store, self._cfg(), NOW)
        row = self.store.runs[f"codex:{sid}:pass-poll"]
        self.assertEqual(row["source"], "codex")
        # The recorded tier (fifth cell) is what run_worker launched.
        self.assertEqual(row["tier"], "codex-luna")
        self.assertEqual(row["model"], "gpt-6-luna")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["verdict"], "PASS")
        # Without a recorded tier the model family names the tier.
        manifests.reset_state()
        self.store = MemStore()
        _write(
            os.path.join(self.scratch, "codex-sessions.tsv"),
            f"pass-poll\t{sid}\t1700000002\tgpt-6-luna\tcodex-luna\n"
            f"bare-poll\t{sid}\t1700000003\tgpt-6-luna\n",
            NOW,
        )
        _write(os.path.join(self.scratch, "codex", "out-bare-poll.md"), "PASS\nbare\n", NOW)
        _write(os.path.join(self.scratch, "codex", "err-bare-poll.log"), "", NOW)
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self.store.runs[f"codex:{sid}:bare-poll"]["tier"], "codex")

    def test_poll_codex_quota_err_marks_row_died(self):
        sid = "01a16401-9955-70cd-bf8c-19944db4f20d"
        _write(
            os.path.join(self.scratch, "codex-sessions.tsv"),
            f"quota-poll\t{sid}\t1700000002\tgpt-6-luna\tcodex-luna\n",
            NOW,
        )
        _write(os.path.join(self.scratch, "codex", "out-quota-poll.md"), "working\n", NOW)
        _write(
            os.path.join(self.scratch, "codex", "err-quota-poll.log"),
            "error 429 rate limit exceeded\n",
            NOW,
        )
        manifests.poll(self.store, self._cfg(), NOW)
        row = self.store.runs[f"codex:{sid}:quota-poll"]
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "QUOTA")


class ManifestSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local scratchpad")
    def test_manifest_parses(self):
        root = os.environ.get("AGENT_DASH_SMOKE_SCRATCH")
        if not root or not os.path.isdir(root):
            self.skipTest("AGENT_DASH_SMOKE_SCRATCH is not set")
        found = list(Path(root).glob("*/scratchpad/muse-sessions.tsv"))
        if not found:
            self.skipTest("no muse-sessions.tsv under AGENT_DASH_SMOKE_SCRATCH")
        rows = manifests.parse_manifest_text(found[0].read_text(encoding="utf-8")[:65536])
        self.assertGreaterEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
