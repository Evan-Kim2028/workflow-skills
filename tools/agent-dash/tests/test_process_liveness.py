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

SID = "ce22eaff-a7d1-4b65-8e9d-0ff213fcc1f5"
SID_UPPER = SID.upper()
MUSE_ARGV = (
    "28553 /Users/evandekim/.local/bin/muse-bin-1.4.9-R6297.1 exec "
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


def muse_row(pid=28553, comm="muse-bin-1.4.9-R6297.1", sid=SID_UPPER):
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
        self.assertEqual(process._canon_comm("muse-bin-1.4.9-R6297.1"), "muse-bin")

    def test_plain_names_unchanged(self):
        for comm in ("muse-bin", "muse", "claude", "cursor-agent", "grok"):
            self.assertEqual(process._canon_comm(comm), comm)

    def test_parse_ps_normalises_pathed_versioned_comm(self):
        rows = process.parse_ps(
            "28553     1 28553  50000 10:00 /Users/evandekim/.local/bin/muse-bin-1.4.9-R6297.1\n"
        )
        self.assertEqual(rows[0]["comm"], "muse-bin")
        self.assertEqual(rows[0]["pid"], 28553)

    def test_versioned_comm_is_a_wanted_source(self):
        wanted = set().union(*process.SOURCE_COMMS.values())
        self.assertIn(process._canon_comm("muse-bin-1.4.9-R6297.1"), wanted)


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

    def test_gone_process_ends_labeled_run_as_done(self):
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
        self.assertEqual(run["status"], "done")

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
                    "started_at": 100,
                }
            }
        )
        ended = process.reconcile_running(store, 200, [muse_row()])
        self.assertEqual(ended, 0)
        row = store.get_run("muse:%s:ws2-x" % SID)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 28553)


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
        ended = process.reconcile_running(store, 200, [muse_row()])
        self.assertEqual(ended, 0)
        self.assertEqual(store.get_run(shadow)["status"], "unknown")
        self.assertEqual(store.get_run(shadow)["parent_session_id"], labeled)
        self.assertIsNone(store.get_run(shadow)["pid"])
        self.assertEqual(store.get_run(labeled)["status"], "running")
        self.assertEqual(store.get_run(labeled)["pid"], 28553)


if __name__ == "__main__":
    unittest.main()
