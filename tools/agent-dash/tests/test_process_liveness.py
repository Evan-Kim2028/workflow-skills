"""Tests for muse liveness in dash/sources/process.py. Fake process rows only."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("dash_sources_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


process = load("process")


def load_liveness():
    path = os.path.join(ROOT, "dash", "liveness.py")
    spec = importlib.util.spec_from_file_location("dash_liveness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


liveness = load_liveness()

SID = "ce22eaff-a7d1-4b65-8e9d-0ff213fcc1f5"
SID_UPPER = SID.upper()
MUSE_ARGV = (
    "28553 /home/user/.local/bin/muse-bin-x exec "
    "--trust-workspace --prompt-file /tmp/pads/scratchpad/muse/ws2-x.md "
    "--session-id %s" % SID_UPPER
)


class Store:
    def __init__(self, runs=None):
        self.saved = dict(runs or {})

    def get_run(self, run_id):
        return self.saved.get(run_id)

    def running_tool_runs(self):
        return [row for row in self.saved.values() if row.get("status") == "running"]

    def unknown_tool_runs(self):
        return [
            row
            for row in self.saved.values()
            if row.get("status") == "unknown" and row.get("source") == "muse"
        ]

    def session_runs(self, session_id):
        return [row for row in self.saved.values() if row.get("session_id") == session_id]

    def upsert_run(self, row):
        cur = dict(self.saved.get(row["run_id"], {}))
        cur.update(row)
        self.saved[row["run_id"]] = cur

    def last_activity(self, run_id, started_at):
        return started_at


def muse_row(pid=28553, comm="muse-bin-x", sid=SID_UPPER):
    return {
        "pid": pid,
        "comm": comm,
        "started_at": 1792164000.0,
        "label": None,
        "session_id": sid,
    }


class SessionIdTests(unittest.TestCase):
    def test_space_form(self):
        self.assertEqual(process.session_id_from_args(MUSE_ARGV), SID_UPPER)

    def test_equals_form(self):
        self.assertEqual(
            process.session_id_from_args("muse exec --session-id=%s" % SID),
            SID,
        )

    def test_missing_flag_is_none(self):
        self.assertIsNone(process.session_id_from_args("muse-bin exec --prompt-file x.md"))

    def test_non_uuid_is_none(self):
        self.assertIsNone(process.session_id_from_args("muse exec --session-id abc"))

    def test_returns_only_the_uuid(self):
        got = process.session_id_from_args(MUSE_ARGV)
        self.assertEqual(got, SID_UPPER)
        self.assertNotIn("exec", got)
        self.assertNotIn("prompt-file", got)


class CommTests(unittest.TestCase):
    def test_versioned_muse_bin_canonicalises(self):
        self.assertEqual(process._canon_comm("muse-bin-x"), "muse-bin")

    def test_plain_names_unchanged(self):
        for comm in ("muse-bin", "muse", "claude", "cursor-agent", "grok"):
            self.assertEqual(process._canon_comm(comm), comm)

    def test_parse_ps_normalises_pathed_versioned_comm(self):
        rows = process.parse_ps(
            "28553     1 28553  50000 10:00 /home/user/.local/bin/muse-bin-x\n"
        )
        self.assertEqual(rows[0]["comm"], "muse-bin")
        self.assertEqual(rows[0]["pid"], 28553)

    def test_versioned_comm_is_a_wanted_source(self):
        wanted = set().union(*process.SOURCE_COMMS.values())
        self.assertIn(process._canon_comm("muse-bin-x"), wanted)


class MatchAgentTests(unittest.TestCase):
    def test_session_id_match_with_versioned_comm(self):
        run_procs = [muse_row()]
        got = process.match_agent("muse", None, "ws2-WS2.7b-ask-ratio", run_procs, SID)
        self.assertIsNotNone(got)
        self.assertEqual(got["pid"], 28553)

    def test_session_match_is_case_insensitive(self):
        run_procs = [muse_row(sid=SID_UPPER)]
        got = process.match_agent("muse", None, None, run_procs, SID)
        self.assertIsNotNone(got)

    def test_session_match_does_not_cross_sources(self):
        run_procs = [muse_row()]
        self.assertIsNone(process.match_agent("grok", None, None, run_procs, SID))

    def test_pid_match_still_works(self):
        run_procs = [muse_row()]
        got = process.match_agent("muse", 28553, None, run_procs)
        self.assertEqual(got["pid"], 28553)

    def test_label_match_still_works(self):
        run_procs = [{"pid": 9, "comm": "cursor-agent", "started_at": 90, "label": "dash-a"}]
        got = process.match_agent("grok", None, "dash-a", run_procs)
        self.assertEqual(got["pid"], 9)

    def test_no_match_returns_none(self):
        run_procs = [muse_row()]
        self.assertIsNone(
            process.match_agent(
                "muse", 1, "other-label", run_procs, "00000000-0000-4000-8000-000000000000"
            )
        )

    def test_unknown_source_returns_none(self):
        self.assertIsNone(process.match_agent("nope", 28553, None, [muse_row()], SID))

    def test_non_uuid_session_is_ignored(self):
        run_procs = [muse_row()]
        self.assertIsNone(process.match_agent("muse", None, None, run_procs, "chat"))

    def test_session_match_beats_label_match(self):
        by_label = {"pid": 11, "comm": "muse", "started_at": 1, "label": "ws2-x"}
        by_session = dict(muse_row(), label="other-label")
        got = process.match_agent("muse", None, "ws2-x", [by_label, by_session], SID_UPPER)
        self.assertEqual(got["pid"], 28553)

    def test_pid_match_beats_session_match(self):
        by_pid = dict(muse_row(), session_id="00000000-0000-4000-8000-000000000000")
        by_session = {"pid": 44, "comm": "muse", "started_at": 1, "session_id": SID_UPPER}
        got = process.match_agent("muse", 28553, None, [by_pid, by_session], SID_UPPER)
        self.assertEqual(got["pid"], 28553)


class ModelFromArgsTests(unittest.TestCase):
    def test_space_and_equals_forms(self):
        self.assertEqual(
            process.model_from_args("cursor-agent -p --model grok-4.7-high --force"),
            "grok-4.7-high",
        )
        self.assertEqual(
            process.model_from_args("muse exec --model=muse-spark-1.3-contributor"),
            "muse-spark-1.3-contributor",
        )
        self.assertEqual(
            process.model_from_args(
                "cursor-agent -p --model grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]"
            ),
            "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]",
        )

    def test_missing_flag_is_none(self):
        self.assertIsNone(process.model_from_args("cursor-agent -p --force"))
        self.assertIsNone(process.model_from_args(""))

    def test_returns_only_the_flag_value(self):
        text = (
            "999 cursor-agent -p --model grok-4.7-high --force --trust "
            "--workspace /work/tree --output-format stream-json "
            "[label: ws7-x]\nthe prompt body stays out"
        )
        got = process.model_from_args(text)
        self.assertEqual(got, "grok-4.7-high")
        self.assertNotIn("workspace", got)
        self.assertNotIn("label", got)
        self.assertNotIn("prompt", got)

    def test_short_flag_is_not_read(self):
        self.assertIsNone(process.model_from_args("grok -p text -m glm-5.3"))

    def test_live_row_carries_only_the_model(self):
        keep_ps, keep_tokens = process._ps, process._tokens_by_pid
        process._ps = lambda: [
            {"pid": 7, "ppid": 1, "pgid": 7, "rss_kb": 10, "elapsed_s": 5.0, "comm": "cursor-agent"}
        ]
        process._tokens_by_pid = lambda: {
            7: {"label": "ws7-x", "session_id": None, "prompt_label": None, "model": "grok-4.7-high"}
        }
        try:
            rows = process.live_agent_processes(now=100.0)
        finally:
            process._ps, process._tokens_by_pid = keep_ps, keep_tokens
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "grok-4.7-high")
        import json

        self.assertNotIn("/work/tree", json.dumps(rows[0]))
        self.assertNotIn("[label:", json.dumps(rows[0]))


class VetoTests(unittest.TestCase):
    LABEL = "ws3-WS3.15-smoke-rev1"
    OLD_SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    NEW_SID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def vstore(self, runs):
        base = Store({key: dict(value) for key, value in runs.items()})

        def newer_open_label_row(label, source, started_at, exclude_run_id):
            best = None
            base_ts = started_at or 0
            for row in base.saved.values():
                if row.get("run_id") == exclude_run_id:
                    continue
                if row.get("label") != label or row.get("source") != source:
                    continue
                if row.get("status") != "running":
                    continue
                if (row.get("started_at") or 0) > base_ts and (
                    best is None or row["started_at"] > best["started_at"]
                ):
                    best = row
            return dict(best) if best else None

        base.newer_open_label_row = newer_open_label_row
        return base

    def old_row(self, **over):
        row = {
            "run_id": "muse:%s:%s" % (self.OLD_SID, self.LABEL),
            "source": "muse",
            "session_id": self.OLD_SID,
            "label": self.LABEL,
            "status": "running",
            "pid": 100,
            "started_at": 1000,
        }
        row.update(over)
        return row

    def new_row(self, **over):
        row = {
            "run_id": "muse:%s:%s" % (self.NEW_SID, self.LABEL),
            "source": "muse",
            "session_id": self.NEW_SID,
            "label": self.LABEL,
            "status": "running",
            "pid": 200,
            "started_at": 2000,
        }
        row.update(over)
        return row

    def live_proc(self):
        return {
            "pid": 200,
            "comm": "muse",
            "started_at": 1990,
            "label": None,
            "prompt_label": self.LABEL,
            "session_id": self.NEW_SID,
        }

    def test_label_match_loses_to_newer_row_and_old_row_closes(self):
        old = self.old_row()
        new = self.new_row()
        store = self.vstore({old["run_id"]: old, new["run_id"]: new})
        run = {"run_id": old["run_id"], "label": self.LABEL, "session_id": self.OLD_SID}
        process.apply_liveness(store, run, "muse", [self.live_proc()])
        self.assertNotEqual(run.get("status"), "running")
        self.assertIsNone(run.get("pid"))
        self.assertEqual(run.get("ended_at"), 1000)

    def test_session_match_is_never_vetoed(self):
        old = self.old_row()
        new = self.new_row()
        store = self.vstore({old["run_id"]: old, new["run_id"]: new})
        proc = dict(self.live_proc(), session_id=self.OLD_SID, started_at=990)
        run = {"run_id": old["run_id"], "label": self.LABEL, "session_id": self.OLD_SID}
        process.apply_liveness(store, run, "muse", [proc])
        self.assertEqual(run.get("status"), "running")
        self.assertEqual(run.get("pid"), 200)

    def test_pid_match_with_own_live_pid_is_kept(self):
        old = self.old_row(pid=100)
        new = self.new_row(pid=300)
        store = self.vstore({old["run_id"]: old, new["run_id"]: new})
        proc = {"pid": 100, "comm": "muse", "started_at": 990, "label": self.LABEL}
        run = {"run_id": old["run_id"], "label": self.LABEL, "session_id": self.OLD_SID}
        process.apply_liveness(store, run, "muse", [proc])
        self.assertEqual(run.get("status"), "running")
        self.assertEqual(run.get("pid"), 100)

    def test_pid_match_vetoed_when_twin_owns_that_pid(self):
        old = self.old_row(pid=200)
        new = self.new_row(pid=200)
        store = self.vstore({old["run_id"]: old, new["run_id"]: new})
        run = {"run_id": old["run_id"], "label": self.LABEL, "session_id": self.OLD_SID}
        process.apply_liveness(store, run, "muse", [self.live_proc()])
        self.assertNotEqual(run.get("status"), "running")
        self.assertIsNone(run.get("pid"))

    def test_reconcile_closes_vetoed_row_and_keeps_newer(self):
        old = self.old_row()
        new = self.new_row()
        store = self.vstore({old["run_id"]: old, new["run_id"]: new})
        ended = process.reconcile_running(store, 3000, [self.live_proc()])
        self.assertEqual(store.get_run(new["run_id"])["status"], "running")
        self.assertEqual(store.get_run(new["run_id"])["pid"], 200)
        self.assertEqual(store.get_run(old["run_id"])["status"], "died_early")
        self.assertIsNone(store.get_run(old["run_id"])["pid"])
        self.assertEqual(ended, 1)


class RunSessionTests(unittest.TestCase):
    def test_uuid_from_muse_run_id(self):
        self.assertEqual(
            process._uuid_from_run_id("muse:%s:ws2-x" % SID), SID
        )

    def test_uuid_from_run_id_rejects_other_sources(self):
        self.assertIsNone(process._uuid_from_run_id("grok:%s:ws2-x" % SID))
        self.assertIsNone(process._uuid_from_run_id("muse:s:ws7-plan"))
        self.assertIsNone(process._uuid_from_run_id(None))

    def test_run_session_prefers_session_id(self):
        run = {"run_id": "muse:%s:ws2-x" % SID, "session_id": SID_UPPER}
        self.assertEqual(process._run_session(run, None), SID_UPPER)

    def test_run_session_falls_back_to_run_id(self):
        run = {"run_id": "muse:%s:ws2-x" % SID}
        self.assertEqual(process._run_session(run, None), SID)


class ApplyLivenessTests(unittest.TestCase):
    def test_muse_run_goes_running_by_session(self):
        store = Store()
        run = {
            "run_id": "muse:%s:ws2-WS2.7b-ask-ratio" % SID,
            "source": "muse",
            "session_id": SID,
            "label": "ws2-WS2.7b-ask-ratio",
            "pid": None,
        }
        process.apply_liveness(store, run, "muse", [muse_row()])
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], 28553)
        self.assertIsNone(run["ended_at"])

    def test_muse_run_goes_running_by_run_id_uuid(self):
        store = Store()
        run = {
            "run_id": "muse:%s:ws2-x" % SID,
            "source": "muse",
            "label": "ws2-x",
            "pid": None,
        }
        process.apply_liveness(store, run, "muse", [muse_row()])
        self.assertEqual(run["status"], "running")

    def test_gone_process_ends_labeled_run_as_died_early(self):
        store = Store(
            {
                "muse:s:ws7-plan": {
                    "run_id": "muse:s:ws7-plan",
                    "status": "running",
                    "started_at": 50,
                }
            }
        )
        run = {"run_id": "muse:s:ws7-plan", "label": "ws7-plan", "pid": 3}
        process.apply_liveness(store, run, "muse", [])
        self.assertEqual(run["status"], "died_early")

    def test_gone_process_ends_unlabeled_run_as_unknown(self):
        store = Store(
            {
                "muse:s:s": {
                    "run_id": "muse:s:s",
                    "status": "running",
                    "started_at": 50,
                }
            }
        )
        run = {"run_id": "muse:s:s", "pid": 3}
        process.apply_liveness(store, run, "muse", [])
        self.assertEqual(run["status"], "unknown")

    def test_close_never_downgrades_a_reported_finish(self):
        store = Store(
            {
                "muse:s:ws7-plan": {
                    "run_id": "muse:s:ws7-plan",
                    "status": "running",
                    "started_at": 50,
                }
            }
        )
        run = {
            "run_id": "muse:s:ws7-plan",
            "label": "ws7-plan",
            "pid": 3,
            "status": "done",
            "verdict": "PASS",
            "ended_at": 60,
        }
        process.apply_liveness(store, run, "muse", [])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["verdict"], "PASS")

    def test_reused_pid_never_vouches_the_old_row(self):
        proc = dict(muse_row(), started_at=1792164000.0 + 3600.0)
        self.assertIsNone(process.match_agent("muse", 28553, None, [proc], SID, 1792164000.0))
        store = Store(
            {
                "muse:%s:ws2-old" % SID: {
                    "run_id": "muse:%s:ws2-old" % SID,
                    "source": "muse",
                    "session_id": SID,
                    "label": "ws2-old",
                    "status": "running",
                    "pid": 28553,
                    "started_at": 1792164000.0,
                }
            }
        )
        run = {"run_id": "muse:%s:ws2-old" % SID, "label": "ws2-old", "pid": 28553,
               "started_at": 1792164000.0}
        process.apply_liveness(store, run, "muse", [proc])
        self.assertEqual(run["status"], "died_early")

    def test_label_scan_skips_a_reused_process(self):
        old = dict(muse_row(), pid=111, started_at=1792164000.0 + 3600.0, label="ws2-x")
        new = dict(muse_row(), pid=222, started_at=1792163990.0, label="ws2-x")
        got = process.match_agent("muse", None, "ws2-x", [old, new], None, 1792164000.0)
        self.assertIsNotNone(got)
        self.assertEqual(got["pid"], 222)

    def test_reconcile_closes_a_vanished_labeled_row_as_died_early(self):
        store = Store(
            {
                "muse:s:ws7-plan": {
                    "run_id": "muse:s:ws7-plan",
                    "source": "muse",
                    "status": "running",
                    "label": "ws7-plan",
                    "pid": 3,
                    "started_at": 50,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 1)
        self.assertEqual(store.get_run("muse:s:ws7-plan")["status"], "died_early")

    def test_reconcile_keeps_session_matched_muse_running(self):
        store = Store(
            {
                "muse:%s:ws2-x" % SID: {
                    "run_id": "muse:%s:ws2-x" % SID,
                    "source": "muse",
                    "session_id": SID,
                    "label": "ws2-x",
                    "status": "running",
                    "pid": None,
                    "started_at": 1792163990.0,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [muse_row()])
        self.assertEqual(ended, 0)
        row = store.get_run("muse:%s:ws2-x" % SID)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 28553)


class ReconcilePiTests(unittest.TestCase):
    def test_reconcile_closes_a_vanished_pi_row_as_died_early(self):
        store = Store(
            {
                "pi:ws15-exec-gone:ws15-exec-gone": {
                    "run_id": "pi:ws15-exec-gone:ws15-exec-gone",
                    "source": "pi",
                    "label": "ws15-exec-gone",
                    "status": "running",
                    "pid": 424242,
                    "started_at": 50,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 1)
        row = store.get_run("pi:ws15-exec-gone:ws15-exec-gone")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "DEAD")
        self.assertIsNone(row.get("pid"))

    def test_reconcile_close_marks_a_rate_limit_as_quota(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out-pi-quota.md"
            out.write_text("partial\n", encoding="utf-8")
            err = Path(tmp) / "err-pi-quota.log"
            err.write_text("Error: 429 Rate limit reached for requests\n", encoding="utf-8")
            store = Store(
                {
                    "pi:s:pi-quota": {
                        "run_id": "pi:s:pi-quota",
                        "source": "pi",
                        "label": "pi-quota",
                        "status": "running",
                        "pid": 424242,
                        "started_at": 50,
                        "out_path": str(out),
                    }
                }
            )
            ended = process.reconcile_running(store, 200, [])
            self.assertEqual(ended, 1)
            row = store.get_run("pi:s:pi-quota")
            self.assertEqual(row["status"], "died_early")
            self.assertEqual(row["verdict"], "QUOTA")

    def test_reconcile_close_keeps_an_existing_verdict(self):
        store = Store(
            {
                "pi:s:pi-partial": {
                    "run_id": "pi:s:pi-partial",
                    "source": "pi",
                    "label": "pi-partial",
                    "status": "running",
                    "pid": 424242,
                    "started_at": 50,
                    "verdict": "PARTIAL",
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 1)
        self.assertEqual(store.get_run("pi:s:pi-partial")["verdict"], "PARTIAL")

    def test_reconcile_keeps_a_label_matched_pi_run_running(self):
        store = Store(
            {
                "pi:s1:ws15-exec-live": {
                    "run_id": "pi:s1:ws15-exec-live",
                    "source": "pi",
                    "label": "ws15-exec-live",
                    "status": "running",
                    "pid": 7,
                    "started_at": 90,
                }
            }
        )
        procs = [
            {
                "pid": 7,
                "comm": "node",
                "started_at": 95.0,
                "label": "ws15-exec-live",
                "model": None,
                "reasoning_effort": None,
                "prompt_label": None,
                "session_id": None,
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 0)
        row = store.get_run("pi:s1:ws15-exec-live")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 7)

    def test_reconcile_keeps_a_launcher_matched_pi_run_running(self):
        store = Store(
            {
                "pi:s1:ws15-exec-live": {
                    "run_id": "pi:s1:ws15-exec-live",
                    "source": "pi",
                    "label": "ws15-exec-live",
                    "status": "running",
                    "pid": 424242,
                    "started_at": 90,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 95.0,
                "label": "ws15-exec-live",
                "launcher": "pi",
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 0)
        row = store.get_run("pi:s1:ws15-exec-live")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 424242)

    def test_reconcile_keeps_a_label_matched_pi_launcher_running(self):
        store = Store(
            {
                "pi:s1:ws15-exec-live": {
                    "run_id": "pi:s1:ws15-exec-live",
                    "source": "pi",
                    "label": "ws15-exec-live",
                    "status": "running",
                    "pid": None,
                    "started_at": 90,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 95.0,
                "label": "ws15-exec-live",
                "launcher": "pi",
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 0)
        row = store.get_run("pi:s1:ws15-exec-live")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 424242)

    def test_commandcode_launcher_does_not_vouch_a_pi_run(self):
        store = Store(
            {
                "pi:s1:ws15-exec-live": {
                    "run_id": "pi:s1:ws15-exec-live",
                    "source": "pi",
                    "label": "ws15-exec-live",
                    "status": "running",
                    "pid": None,
                    "started_at": 90,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 95.0,
                "label": "ws15-exec-live",
                "launcher": "commandcode",
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 1)
        row = store.get_run("pi:s1:ws15-exec-live")
        self.assertEqual(row["status"], "died_early")

    def test_reconcile_keeps_a_launcher_matched_commandcode_run_running(self):
        store = Store(
            {
                "commandcode:s:ws6-plan-live": {
                    "run_id": "commandcode:s:ws6-plan-live",
                    "source": "commandcode",
                    "label": "ws6-plan-live",
                    "status": "running",
                    "pid": 424242,
                    "started_at": 50,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 55.0,
                "label": "ws6-plan-live",
                "launcher": "commandcode",
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 0)
        row = store.get_run("commandcode:s:ws6-plan-live")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 424242)

    def test_reconcile_closes_a_vanished_commandcode_row_as_died_early(self):
        store = Store(
            {
                "commandcode:s:ws6-plan-gone": {
                    "run_id": "commandcode:s:ws6-plan-gone",
                    "source": "commandcode",
                    "label": "ws6-plan-gone",
                    "status": "running",
                    "pid": 424242,
                    "started_at": 50,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 1)
        row = store.get_run("commandcode:s:ws6-plan-gone")
        self.assertEqual(row["status"], "died_early")
        self.assertIsNone(row.get("pid"))

    def test_pi_launcher_does_not_vouch_a_commandcode_run(self):
        store = Store(
            {
                "commandcode:s:ws6-plan-live": {
                    "run_id": "commandcode:s:ws6-plan-live",
                    "source": "commandcode",
                    "label": "ws6-plan-live",
                    "status": "running",
                    "pid": None,
                    "started_at": 50,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 55.0,
                "label": "ws6-plan-live",
                "launcher": "pi",
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        self.assertEqual(ended, 1)
        self.assertEqual(store.get_run("commandcode:s:ws6-plan-live")["status"], "died_early")


class StaleStore(Store):
    def stale_running(self, now):
        return [
            (dict(row), "no source evidence for longer than 6h")
            for row in self.saved.values()
            if row.get("status") == "running"
        ]


class StaleGenericTests(unittest.TestCase):
    def test_quiet_generic_run_closes_as_died_early(self):
        store = StaleStore(
            {
                "generic:s:ws15-plan-pcdedup": {
                    "run_id": "generic:s:ws15-plan-pcdedup",
                    "source": "generic",
                    "label": "ws15-plan-pcdedup",
                    "status": "running",
                    "pid": None,
                    "started_at": 50,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 1)
        row = store.get_run("generic:s:ws15-plan-pcdedup")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "IDLE")

    def test_generic_row_with_fresh_stale_evidence_is_left_alone(self):
        store = StaleStore()
        ended = process.reconcile_running(store, 200, [])
        self.assertEqual(ended, 0)


class PromptFileTests(unittest.TestCase):
    ARGV = (
        "muse-bin-1.4.9 exec --prompt-file "
        "/tmp/pads/scratchpad/muse/ws2-WS2.14-cards-list.md "
        "--session-id " + SID
    )

    def test_slash_form_returns_only_the_label(self):
        got = process.prompt_label_from_args(self.ARGV)
        self.assertEqual(got, "ws2-WS2.14-cards-list")
        self.assertNotIn("/", got)
        self.assertNotIn("prompt-file", got)

    def test_equals_form(self):
        text = "muse exec --prompt-file=/tmp/muse/ws2-x.md"
        self.assertEqual(process.prompt_label_from_args(text), "ws2-x")

    def test_bare_filename_is_none(self):
        self.assertIsNone(process.prompt_label_from_args("muse exec --prompt-file ws2-x.md"))

    def test_non_md_is_none(self):
        self.assertIsNone(
            process.prompt_label_from_args("muse exec --prompt-file /tmp/muse/ws2-x.txt")
        )

    def test_match_by_prompt_label_without_session(self):
        procs = [
            {
                "pid": 28553,
                "comm": "muse-bin-1.4.9-R6297.1",
                "started_at": 1,
                "label": None,
                "prompt_label": "ws2-WS2.14-cards-list",
                "session_id": None,
            }
        ]
        got = process.match_agent("muse", None, "ws2-WS2.14-cards-list", procs, None)
        self.assertEqual(got["pid"], 28553)

    def test_prompt_label_does_not_match_other_sources(self):
        procs = [{"pid": 1, "comm": "cursor-agent", "label": None, "prompt_label": "ws2-x"}]
        self.assertIsNone(process.match_agent("grok", None, "ws2-x", procs))

    def test_reconcile_adopts_unknown_labeled_row(self):
        label = "ws2-WS2.14-cards-list"
        run_id = "muse:%s:%s" % (SID, label)
        store = Store(
            {
                run_id: {
                    "run_id": run_id,
                    "source": "muse",
                    "session_id": SID,
                    "label": label,
                    "status": "unknown",
                    "pid": None,
                    "started_at": 100,
                }
            }
        )
        procs = [
            {
                "pid": 28553,
                "comm": "muse-bin",
                "started_at": 90,
                "label": label,
                "prompt_label": label,
                "session_id": None,
            }
        ]
        ended = process.reconcile_running(store, 200, procs)
        row = store.get_run(run_id)
        self.assertEqual(ended, 0)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 28553)
        self.assertIsNone(row["ended_at"])

    def test_reconcile_closes_unknown_when_the_process_is_gone(self):
        run_id = "muse:%s:ws2-x" % SID
        store = Store(
            {
                run_id: {
                    "run_id": run_id,
                    "source": "muse",
                    "session_id": SID,
                    "label": "ws2-x",
                    "status": "unknown",
                    "pid": 999,
                    "started_at": 100,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [])
        row = store.get_run(run_id)
        self.assertEqual(ended, 1)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["ended_at"], 100)
        self.assertIsNone(row["pid"])

    def test_apply_liveness_closes_unknown_and_revives_a_live_one(self):
        run_id = "muse:%s:ws2-x" % SID
        store = Store(
            {
                run_id: {
                    "run_id": run_id,
                    "status": "unknown",
                    "started_at": 50,
                    "pid": 3,
                    "session_id": SID,
                }
            }
        )
        dead = {"run_id": run_id, "label": "ws2-x", "pid": 3, "session_id": SID}
        process.apply_liveness(store, dead, "muse", [])
        self.assertEqual(dead["status"], "done")
        self.assertEqual(dead["ended_at"], 50)
        self.assertIsNone(dead["pid"])
        live = {"run_id": run_id, "label": "ws2-x", "pid": None, "session_id": SID,
                "started_at": 1792163990.0}
        process.apply_liveness(store, live, "muse", [muse_row()])
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["pid"], 28553)

    def test_reconcile_retires_live_shadow_onto_labeled_run(self):
        label = "ws2-x"
        labeled = "muse:%s:%s" % (SID, label)
        shadow = "muse:%s:%s" % (SID, SID)
        store = Store(
            {
                labeled: {
                    "run_id": labeled,
                    "source": "muse",
                    "session_id": SID,
                    "label": label,
                    "status": "unknown",
                    "pid": None,
                    "started_at": 100,
                },
                shadow: {
                    "run_id": shadow,
                    "source": "muse",
                    "session_id": SID,
                    "status": "running",
                    "pid": 28553,
                    "started_at": 90,
                },
            }
        )
        ended = process.reconcile_running(store, 200, [dict(muse_row(), started_at=90)])
        self.assertEqual(ended, 0)
        self.assertEqual(store.get_run(shadow)["status"], "unknown")
        self.assertEqual(store.get_run(shadow)["parent_session_id"], labeled)
        self.assertIsNone(store.get_run(shadow)["pid"])
        self.assertEqual(store.get_run(labeled)["status"], "running")
        self.assertEqual(store.get_run(labeled)["pid"], 28553)


class RunWorkerLabelTests(unittest.TestCase):
    def test_argv_label_is_extracted(self):
        self.assertEqual(
            process.run_worker_label_from_args(
                "4321 python3 /opt/tools/agent-dash/scripts/run_worker.py default ws8-WS8.1-budget /ws /tmp/x.md /runs 540"
            ),
            "ws8-WS8.1-budget",
        )

    def test_first_token_is_the_tier_not_the_label(self):
        self.assertEqual(
            process.run_worker_label_from_args("4321 run_worker.py auto my-label"),
            "my-label",
        )

    def test_no_run_worker_in_argv_is_none(self):
        self.assertIsNone(process.run_worker_label_from_args("4321 grok -p hello -m glm-5.3"))
        self.assertIsNone(process.run_worker_label_from_args("4321 vim run_worker.py"))
        self.assertIsNone(process.run_worker_label_from_args(""))

    def test_bad_labels_are_rejected(self):
        self.assertIsNone(process.run_worker_label_from_args("1 run_worker.py default ../evil"))
        self.assertIsNone(process.run_worker_label_from_args("1 run_worker.py default"))
        self.assertIsNone(
            process.run_worker_label_from_args("1 /x/run_worker.py.bak default lbl")
        )

    def test_worker_listing_covers_bracket_labels_on_any_comm(self):
        saved = (
            process.live_agent_processes,
            process._ps,
            process._tokens_by_pid,
            process._run_worker_labels,
        )
        process.live_agent_processes = lambda now=None: [
            {"pid": 11, "comm": "grok", "started_at": 1.0, "label": "ws9-a",
             "prompt_label": None, "session_id": None}
        ]
        process._ps = lambda: [
            {"pid": 11, "comm": "grok", "elapsed_s": 5.0},
            {"pid": 22, "comm": "sleep", "elapsed_s": 5.0},
            {"pid": 33, "comm": "python3", "elapsed_s": 5.0},
        ]
        process._tokens_by_pid = lambda: {
            22: {"label": "dash-proof-worker", "session_id": None, "prompt_label": None},
        }
        process._run_worker_labels = lambda: {33: "ws9-runner"}
        try:
            rows = process.live_worker_processes(1000.0)
        finally:
            (
                process.live_agent_processes,
                process._ps,
                process._tokens_by_pid,
                process._run_worker_labels,
            ) = saved
        by_pid = {row["pid"]: row for row in rows}
        self.assertEqual(by_pid[11]["label"], "ws9-a")
        self.assertEqual(by_pid[22]["label"], "dash-proof-worker")
        self.assertEqual(by_pid[22]["comm"], "sleep")
        self.assertEqual(by_pid[33]["label"], "ws9-runner")
        for row in rows:
            self.assertEqual(
                set(row),
                {"pid", "comm", "started_at", "label", "prompt_label", "session_id"},
            )


class QuotaAndPidFileTests(unittest.TestCase):
    def test_text_patterns(self):
        self.assertTrue(liveness.text_shows_quota("Usage limit reached for 5 hours"))
        self.assertTrue(liveness.text_shows_quota("RATE LIMIT exceeded, retry later"))
        self.assertTrue(liveness.text_shows_quota("request failed with error 429"))
        self.assertTrue(liveness.text_shows_quota("429 Too Many Requests"))
        self.assertTrue(liveness.text_shows_quota("over quota, cooling down"))
        self.assertFalse(liveness.text_shows_quota("PASS\nall done\n"))
        self.assertFalse(liveness.text_shows_quota(""))
        self.assertFalse(liveness.text_shows_quota(None))
        self.assertFalse(liveness.text_shows_quota("all good", patterns=["(["]))

    def test_file_tail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            hit = os.path.join(tmp, "err.log")
            with open(hit, "wb") as fh:
                fh.write(b"x" * 9000 + b"\nUsage limit reached for 5 hour\n")
            self.assertTrue(liveness.file_shows_quota(hit))
            miss = os.path.join(tmp, "clean.log")
            with open(miss, "wb") as fh:
                fh.write(b"working\nPASS\n")
            self.assertFalse(liveness.file_shows_quota(miss))
            empty = os.path.join(tmp, "empty.log")
            open(empty, "wb").close()
            self.assertFalse(liveness.file_shows_quota(empty))
            self.assertFalse(liveness.file_shows_quota(os.path.join(tmp, "missing.log")))
            self.assertFalse(liveness.file_shows_quota(None))

    def test_read_pid_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pid_path = os.path.join(tmp, "ws9-x.pid")
            with open(pid_path, "w") as fh:
                fh.write("25345\n")
            self.assertEqual(liveness.read_pid_file(pid_path), 25345)
            blank = os.path.join(tmp, "blank.pid")
            with open(blank, "w") as fh:
                fh.write("   \n")
            self.assertIsNone(liveness.read_pid_file(blank))
            junk = os.path.join(tmp, "junk.pid")
            with open(junk, "w") as fh:
                fh.write("not-a-pid\n")
            self.assertIsNone(liveness.read_pid_file(junk))
            self.assertIsNone(liveness.read_pid_file(os.path.join(tmp, "missing.pid")))
            self.assertIsNone(liveness.read_pid_file(None))


class LauncherParsingTests(unittest.TestCase):
    PI_ARGV = (
        "424242 python3 /opt/tools/agent-dash/scripts/launch_pi.py "
        "ws15-exec-wire /ws /tmp/prompt-ws15-exec-wire.md /out/pi "
        "--model river/deepseek-ai/DeepSeek-V4.1-Flash"
    )
    CMD_ARGV = (
        "424243 python3 /opt/tools/agent-dash/scripts/launch_cmd.py "
        "ws6-plan-wire /ws /tmp/prompt-ws6-plan-wire.md /out/cmd"
    )
    CODEX_ARGV = (
        "424244 python3 /opt/tools/agent-dash/scripts/launch_codex.py "
        "ws11-plan-wire /ws /tmp/prompt-ws11-plan-wire.md /out "
        "--model gpt-6-luna --tier codex-luna --reasoning-effort xhigh"
    )

    def test_pi_launcher_source_and_label(self):
        self.assertEqual(process.launcher_source_from_args(self.PI_ARGV), "pi")
        self.assertEqual(process.launcher_label_from_args(self.PI_ARGV), "ws15-exec-wire")

    def test_cmd_launcher_source_and_label(self):
        self.assertEqual(process.launcher_source_from_args(self.CMD_ARGV), "commandcode")
        self.assertEqual(process.launcher_label_from_args(self.CMD_ARGV), "ws6-plan-wire")

    def test_codex_launcher_source_and_label(self):
        self.assertEqual(process.launcher_source_from_args(self.CODEX_ARGV), "codex")
        self.assertEqual(process.launcher_label_from_args(self.CODEX_ARGV), "ws11-plan-wire")

    def test_codex_effort_from_dash_c_flag(self):
        self.assertEqual(
            process.reasoning_effort_from_args('codex exec -c model_reasoning_effort="xhigh" -m gpt-6-luna'),
            "xhigh",
        )
        self.assertIsNone(process.reasoning_effort_from_args("codex exec -m gpt-6-luna"))

    def test_plain_python3_is_not_a_launcher(self):
        argv = "999 python3 /opt/tools/run_worker.py default ws8-x /ws /tmp/x.md /runs 540"
        self.assertIsNone(process.launcher_source_from_args(argv))
        self.assertIsNone(process.launcher_label_from_args(argv))
        self.assertIsNone(process.launcher_source_from_args("999 python3 -c 'print(1)'"))
        self.assertIsNone(process.launcher_label_from_args(""))
        self.assertIsNone(process.launcher_label_from_args(None))

    def test_launcher_without_a_label_is_none(self):
        self.assertIsNone(
            process.launcher_label_from_args("999 python3 /opt/tools/scripts/launch_pi.py")
        )

    def test_bad_launcher_labels_are_rejected(self):
        self.assertIsNone(
            process.launcher_label_from_args("1 python3 /x/launch_pi.py ../evil /w /p /o")
        )
        self.assertIsNone(
            process.launcher_label_from_args("1 python3 /x/launch_cmd.py ../evil /w /p /o")
        )
        self.assertIsNone(
            process.launcher_source_from_args("1 python3 /x/launch_pi_py_bak ws1-x /w /p /o")
        )

    def test_returns_only_the_label(self):
        label = process.launcher_label_from_args(self.PI_ARGV)
        self.assertNotIn("launch_pi", label)
        self.assertNotIn("/ws", label)
        self.assertNotIn("DeepSeek", label)


class LauncherRowTests(unittest.TestCase):
    def live_rows(self, ps_rows, tokens_by_pid, now=1000.0):
        keep_ps, keep_tokens = process._ps, process._tokens_by_pid
        process._ps = lambda: ps_rows
        process._tokens_by_pid = lambda: tokens_by_pid
        try:
            return process.live_agent_processes(now=now)
        finally:
            process._ps, process._tokens_by_pid = keep_ps, keep_tokens

    def launcher_token(self, label, launcher):
        return {
            "label": label,
            "session_id": None,
            "prompt_label": None,
            "model": None,
            "reasoning_effort": None,
            "pi": False,
            "launcher": launcher,
        }

    def test_pi_launcher_row_is_emitted_with_its_label(self):
        rows = self.live_rows(
            [{"pid": 7, "ppid": 1, "pgid": 7, "rss_kb": 10, "elapsed_s": 5.0, "comm": "python3"}],
            {7: self.launcher_token("ws15-exec-wire", "pi")},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 7)
        self.assertEqual(rows[0]["comm"], "python3")
        self.assertEqual(rows[0]["label"], "ws15-exec-wire")
        self.assertEqual(rows[0]["launcher"], "pi")
        import json

        self.assertNotIn("scratchpad", json.dumps(rows[0]))

    def test_cmd_launcher_row_is_emitted_with_its_label(self):
        rows = self.live_rows(
            [{"pid": 9, "ppid": 1, "pgid": 9, "rss_kb": 10, "elapsed_s": 5.0, "comm": "python3"}],
            {9: self.launcher_token("ws6-plan-wire", "commandcode")},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["launcher"], "commandcode")
        self.assertEqual(rows[0]["label"], "ws6-plan-wire")

    def test_plain_python3_without_a_launcher_token_is_not_emitted(self):
        rows = self.live_rows(
            [{"pid": 9, "ppid": 1, "pgid": 9, "rss_kb": 10, "elapsed_s": 5.0, "comm": "python3"}],
            {},
        )
        self.assertEqual(rows, [])

    def test_match_agent_accepts_the_launcher_row_for_its_source(self):
        procs = [
            {
                "pid": 7,
                "comm": "python3",
                "started_at": 90,
                "label": "ws15-exec-wire",
                "launcher": "pi",
            }
        ]
        got = process.match_agent("pi", 7, None, procs)
        self.assertIsNotNone(got)
        self.assertEqual(got["pid"], 7)
        got = process.match_agent("pi", None, "ws15-exec-wire", procs)
        self.assertIsNotNone(got)
        self.assertIsNone(process.match_agent("commandcode", 7, None, procs))
        self.assertIsNone(process.match_agent("commandcode", None, "ws15-exec-wire", procs))

    def test_match_agent_accepts_a_commandcode_launcher(self):
        procs = [
            {
                "pid": 9,
                "comm": "python3",
                "started_at": 90,
                "label": "ws6-plan-wire",
                "launcher": "commandcode",
            }
        ]
        self.assertIsNotNone(process.match_agent("commandcode", 9, None, procs))
        self.assertIsNotNone(process.match_agent("commandcode", None, "ws6-plan-wire", procs))
        self.assertIsNone(process.match_agent("pi", 9, None, procs))
        self.assertIsNone(process.match_agent("muse", 9, "ws6-plan-wire", procs))

    def test_live_launcher_revives_a_stored_died_early_pi_row(self):
        run_id = "pi:s1:ws15-exec-wire"
        store = Store(
            {
                run_id: {
                    "run_id": run_id,
                    "source": "pi",
                    "session_id": "s1",
                    "label": "ws15-exec-wire",
                    "status": "died_early",
                    "pid": None,
                    "started_at": 90,
                    "ended_at": 120,
                }
            }
        )
        procs = [
            {
                "pid": 424242,
                "comm": "python3",
                "started_at": 95.0,
                "label": "ws15-exec-wire",
                "launcher": "pi",
            }
        ]
        run = {"run_id": run_id, "label": "ws15-exec-wire", "pid": 424242, "started_at": 90}
        process.apply_liveness(store, run, "pi", procs)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], 424242)
        self.assertIsNone(run["ended_at"])

    def test_dead_launcher_keeps_a_stored_died_early_pi_row_closed(self):
        run_id = "pi:s1:ws15-exec-gone"
        store = Store(
            {
                run_id: {
                    "run_id": run_id,
                    "source": "pi",
                    "session_id": "s1",
                    "label": "ws15-exec-gone",
                    "status": "died_early",
                    "pid": None,
                    "started_at": 90,
                    "ended_at": 120,
                }
            }
        )
        run = {"run_id": run_id, "label": "ws15-exec-gone", "pid": None, "started_at": 90}
        process.apply_liveness(store, run, "pi", [])
        self.assertNotEqual(run.get("status"), "running")


if __name__ == "__main__":
    unittest.main()
