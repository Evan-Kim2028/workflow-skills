"""Tests for the pi harness: dash/sources/pi.py, scripts/launch_pi.py, the
process liveness, the price rows, and the tile bucket.

Synthetic fixtures only: a pi-sessions.tsv manifest, a stream ndjson (the
shipped ``tests/fixtures/pi-stream.ndjson`` sample), out/err files, a
running/<label> pid file, and generic.jsonl report lines, all written under
a temp directory. No live state, no model calls, no API key.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import sys

sys.path.insert(0, str(ROOT))

from dash import pricing  # noqa: E402
from dash.models import model_key, run_model_key  # noqa: E402

SPEC = importlib.util.spec_from_file_location("dash_sources_pi", ROOT / "dash" / "sources" / "pi.py")
pi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pi)

SPEC2 = importlib.util.spec_from_file_location("launch_pi", ROOT / "scripts" / "launch_pi.py")
launch = importlib.util.module_from_spec(SPEC2)
SPEC2.loader.exec_module(launch)

SPEC3 = importlib.util.spec_from_file_location("dash_sources_process", ROOT / "dash" / "sources" / "process.py")
process = importlib.util.module_from_spec(SPEC3)
SPEC3.loader.exec_module(process)

FIXTURE = HERE / "fixtures" / "pi-stream.ndjson"
MORPH_FIXTURE = HERE / "fixtures" / "pi-stream-morph.ndjson"
SID = "01a16401-9955-70cd-bf8c-19944db4f20c"
MORPH = "morph/morph-dsv41flash-1017"
MORPH_SID = "01a1c3b9-4c18-7517-af98-8caad17525f4"
DEEPSEEK = "river/deepseek-ai/DeepSeek-V4.1-Flash"
GLM = "river/zai-org/GLM-5.3-Flash"
DEEPSEEK_ID = "deepseek-ai/DeepSeek-V4.1-Flash"
GLM_ID = "zai-org/GLM-5.3-Flash"
HEADER = "label\tsession_id\tstarted_epoch\tmodel\n"


class Store:
    """The store surface the pi source uses."""

    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = {}
        self.sessions = {}

    def get_offset(self, key):
        return self.offsets.get(key)

    def set_offset(self, key, inode, offset, size, mtime):
        self.offsets[key] = (inode, offset, size, mtime)

    def upsert_run(self, row):
        cur = dict(self.runs.get(row["run_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.runs[row["run_id"]] = cur

    def upsert_session(self, row):
        self.sessions[row["session_id"]] = dict(row)

    def add_usage(self, row):
        self.usage[(row.get("run_id"), row["message_id"])] = dict(row)

    def last_activity(self, run_id, started_at):
        return started_at

    def get_run(self, run_id):
        return self.runs.get(run_id)


class ParserTest(unittest.TestCase):
    """parse_stream / final_text on the shipped fixture."""

    def setUp(self):
        self.raw = FIXTURE.read_bytes()

    def test_assistant_events_and_cache_split(self):
        events = pi.parse_stream(self.raw, SID)
        self.assertEqual(len(events), 2)
        first, second = events
        # input is the cache miss, cacheRead the hit
        self.assertEqual((first["input"], first["cache_read"]), (1900, 0))
        self.assertEqual(first["output"], 38)
        self.assertEqual((second["input"], second["cache_read"]), (159, 1792))
        self.assertEqual(second["output"], 3)

    def test_message_id_is_session_plus_index(self):
        events = pi.parse_stream(self.raw, SID)
        self.assertEqual([e["message_id"] for e in events], [f"{SID}-1", f"{SID}-2"])

    def test_model_is_the_reported_id_without_provider_prefix(self):
        events = pi.parse_stream(self.raw, SID)
        self.assertEqual(events[0]["model"], DEEPSEEK_ID)

    def test_timestamp_is_an_epoch(self):
        events = pi.parse_stream(self.raw, SID)
        self.assertGreater(events[0]["ts"], 1_700_000_000)

    def test_final_text_is_the_last_assistant_text(self):
        self.assertEqual(pi.final_text(self.raw), "DONE")

    def test_no_usage_without_a_usage_block(self):
        raw = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": []}}).encode() + b"\n"
        self.assertEqual(pi.parse_stream(raw, SID), [])


class LauncherHelperTest(unittest.TestCase):
    def test_tier_buckets(self):
        self.assertEqual(launch.tier_of(DEEPSEEK), "deepseek")
        self.assertEqual(launch.tier_of(GLM), "glm")
        self.assertEqual(launch.tier_of(DEEPSEEK_ID), "deepseek")
        self.assertEqual(launch.tier_of(GLM_ID), "glm")

    def test_label_prefix_only_when_missing(self):
        self.assertEqual(launch.ensure_label_prefix("hi", "ws1-exec-x"), "[label: ws1-exec-x]\nhi")
        already = "[label: ws1-exec-x]\nhi"
        self.assertEqual(launch.ensure_label_prefix(already, "ws1-exec-x"), already)

    def test_bad_label_refused_with_a_fix(self):
        fix = launch.validate_label("hub-visuals")
        self.assertIsNotNone(fix)
        self.assertIn("hub", fix)
        self.assertIsNone(launch.validate_label("ws1-plan-visuals"))

    def test_quota_marks(self):
        self.assertTrue(launch.quota_hit([{"error": "insufficient credits", "stop": "error"}]))
        self.assertTrue(launch.quota_hit([{"error": "HTTP 402 payment required", "stop": "error"}]))
        self.assertTrue(launch.quota_hit([{"error": "429 rate limit", "stop": "error"}]))
        self.assertFalse(launch.quota_hit([{"error": "connection reset", "stop": "error"}]))

    def test_recovered_429_is_not_quota_and_counts_as_a_retry(self):
        # River's per-account 429 mid-run: pi emits the error message_end,
        # agent_end willRetry, auto_retry_start, then succeeds. The run
        # did the task, so this is a retry, not quota exhaustion.
        message = (
            '429: {"message":"Account request rate limit exceeded for this model",'
            '"type":"rate_limit_error","code":"account_rate"}'
        )
        events = [
            {"error": message, "stop": "error"},
            {"error": None, "stop": "toolUse"},
            {"error": None, "stop": "stop"},
        ]
        self.assertFalse(launch.quota_hit(events))
        self.assertEqual(launch.rate_limit_retries(events), 1)

    def test_unrecovered_429_is_quota(self):
        # Last assistant message_end: nothing follows, so quota.
        self.assertTrue(
            launch.quota_hit([{"error": "429: Account request rate limit exceeded for this model", "stop": "error"}])
        )
        # A 429 with no successful assistant message after it is unrecovered.
        tail = [
            {"error": None, "stop": "toolUse"},
            {"error": "429: rate limit exceeded", "stop": "error"},
        ]
        self.assertTrue(launch.quota_hit(tail))
        self.assertEqual(launch.rate_limit_retries(tail), 0)

    def test_credit_exhaustion_is_quota_even_when_recovered(self):
        recovered = [
            {"error": "HTTP 402 payment required", "stop": "error"},
            {"error": None, "stop": "stop"},
        ]
        self.assertTrue(launch.quota_hit(recovered))
        self.assertTrue(
            launch.quota_hit([{"error": None, "stop": "toolUse"}, {"error": "insufficient credits", "stop": "error"}])
        )

    def test_clean_run_has_no_quota_and_no_retries(self):
        events = [{"error": None, "stop": "toolUse"}, {"error": None, "stop": "stop"}]
        self.assertFalse(launch.quota_hit(events))
        self.assertEqual(launch.rate_limit_retries(events), 0)

    def test_verdict_from_first_line(self):
        self.assertEqual(launch.verdict_of("PASS\nall good"), "PASS")
        self.assertEqual(launch.verdict_of("PARTIAL: x\nPASS later"), "PARTIAL")
        self.assertEqual(launch.verdict_of("BLOCKED: no key"), "BLOCKED")
        self.assertEqual(launch.verdict_of("nothing here"), "PARTIAL")

    def test_parent_session_of_prefers_pad_then_env(self):
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        pad = os.path.join(os.sep, "tmp", "claude-1000", "-x", sid, "scratchpad", "pi")
        self.assertEqual(launch.parent_session_of(pad), sid)
        old = dict(os.environ)
        try:
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertIsNone(launch.parent_session_of("/tmp/flat"))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            self.assertEqual(
                launch.parent_session_of("/tmp/flat"),
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            )
            os.environ["CLAUDE_CODE_SESSION_ID"] = "not-a-session"
            self.assertIsNone(launch.parent_session_of("/tmp/flat"))
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_assistant_events_and_session_id(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(launch.session_id_of(raw), SID)
        # The fixture's two assistant message_end events are both successes.
        self.assertEqual([e["error"] for e in launch.assistant_events(raw)], [None, None])
        err = json.dumps(
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "insufficient credit"}}
        ).encode() + b"\n"
        self.assertEqual(launch.assistant_events(err)[0]["error"], "insufficient credit")

    def test_recovered_429_stream_is_not_quota_and_keeps_its_report(self):
        # The real run shape from the 2026-10-30 streams: a message_end 429
        # (rate_limit_error / account_rate), pi's auto-retry, then the run
        # finishes with text. Status must read done, one retry recorded.
        raw = b"".join(
            json.dumps(obj).encode("utf-8") + b"\n"
            for obj in (
                {"type": "session", "id": SID},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": (
                            '429: {"message":"Account request rate limit exceeded for this model",'
                            '"type":"rate_limit_error","code":"account_rate"}'
                        ),
                    },
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "PASS\nreport body"}],
                        "stopReason": "stop",
                    },
                },
            )
        )
        events = launch.assistant_events(raw)
        self.assertFalse(launch.quota_hit(events))
        self.assertEqual(launch.rate_limit_retries(events), 1)
        self.assertEqual(pi.final_text(raw), "PASS\nreport body")


class SourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.scratch = self.tmp / "scratch"
        self.pad = self.scratch / "enc" / SID / "scratchpad" / "pi"
        self.pad.mkdir(parents=True)
        self.store = Store()
        pi._tsv_cache.clear()
        pi._verdict_cache.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def cfg(self):
        return {
            "roots": {"scratchpads": [str(self.scratch)]},
            "read_budgets": {"per_file": 4 * 1024 * 1024, "per_tick": 8 * 1024 * 1024},
            # Per-poll discovery: these tests pin a row seen on the next poll.
            "dir_scan_s": 0,
        }

    def poll(self, now=1_800_000_000.0):
        pi.poll(self.store, self.cfg(), now)
        self.assertIsNone(pi.last_error)

    def write_tsv(self, rows):
        text = HEADER + "".join("\t".join(str(x) for x in row) + "\n" for row in rows)
        (self.pad / "pi-sessions.tsv").write_text(text, encoding="utf-8")

    def run_id(self, label, sid=SID):
        return f"pi:{sid}:{label}"

    def test_done_run_with_verdict_from_out_file(self):
        self.write_tsv([["ws15-exec-wire", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-wire.md").write_text("PASS\nDid the thing.\n", encoding="utf-8")
        self.poll()
        run = self.store.runs[self.run_id("ws15-exec-wire")]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["verdict"], "PASS")
        self.assertEqual(run["source"], "pi")
        self.assertEqual(run["tier"], "deepseek")
        self.assertEqual(run["model"], DEEPSEEK)
        self.assertAlmostEqual(run["started_at"], 1792000000.0)
        self.assertIn("out-ws15-exec-wire.md", run["out_path"])

    def test_run_links_to_lead_session_from_pad(self):
        # The pi outdir under a session scratchpad names the lead
        # session that launched the worker.
        self.write_tsv([["ws15-exec-wire", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-wire.md").write_text("PASS\nDid the thing.\n", encoding="utf-8")
        self.poll()
        run = self.store.runs[self.run_id("ws15-exec-wire")]
        self.assertEqual(run["parent_session_id"], SID)

    def test_live_running_row_reads_session_from_stream(self):
        self.write_tsv([["ws15-plan-wire", SID, 1792000000, GLM]])
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws15-plan-wire").write_text("999999\t%s" % GLM, encoding="utf-8")
        (self.pad / "stream-ws15-plan-wire.ndjson").write_bytes(FIXTURE.read_bytes())
        self.poll()
        run = self.store.runs[self.run_id("ws15-plan-wire")]
        # pid 999999 is dead, so the stream's own session read plus the absent
        # out file leaves it unknown rather than a false running.
        self.assertIn(run["status"], ("running", "unknown", "died_early"))

    def test_commandcode_outdir_with_running_folder_is_ignored(self):
        # A live Command Code launch (its own running/ folder, attempt
        # streams, even a manifest) must not read as a second pi: run for
        # the same label: no pi: run id may mention the cmd label.
        cmd = self.scratch / "enc" / SID / "scratchpad" / "cmd"
        cmd.mkdir(parents=True)
        (cmd / "running").mkdir()
        (cmd / "running" / "ws15-exec-cmd").write_text(
            "999999\treflection/beam:free", encoding="utf-8"
        )
        (cmd / "stream-ws15-exec-cmd-1.ndjson").write_text(
            '{"type": "event", "event": {"type": "run_start", "sessionId": "cmd-sid-1"}}\n',
            encoding="utf-8",
        )
        self.poll()
        self.assertEqual(self.store.runs, {})
        (cmd / "cmd-sessions.tsv").write_text(
            "label\tmodel\tsession_id\tstarted_epoch\tended_epoch\tinput_tokens\toutput_tokens\tstatus\n"
            "ws15-exec-cmd\treflection/beam:free\tcmd-sid-1\t1792000000\t1792000060\t10\t2\tok\n",
            encoding="utf-8",
        )
        self.poll()
        self.assertEqual(self.store.runs, {})

    def test_running_only_pi_launch_is_kept(self):
        # The guard must not hide a genuine pi launch that has no TSV row
        # yet: running/ plus a prompt file and a pi-shaped stream is a pi
        # outdir from the first second.
        (self.pad / "running").mkdir()
        (self.pad / "running" / "ws15-exec-early").write_text(
            "999999\t%s" % DEEPSEEK, encoding="utf-8"
        )
        (self.pad / "prompt-ws15-exec-early.md").write_text(
            "[label: ws15-exec-early]\ndo it\n", encoding="utf-8"
        )
        (self.pad / "stream-ws15-exec-early.ndjson").write_bytes(FIXTURE.read_bytes())
        self.poll()
        self.assertIn(self.run_id("ws15-exec-early"), self.store.runs)

    def test_quota_file_finishes_died_early_blocked(self):
        self.write_tsv([["ws15-exec-quota", SID, 1792000000, DEEPSEEK]])
        (self.pad / "QUOTA").write_text("model\t%s\nlabel\tws15-exec-quota\n" % DEEPSEEK, encoding="utf-8")
        self.poll()
        run = self.store.runs[self.run_id("ws15-exec-quota")]
        self.assertEqual(run["status"], "died_early")
        self.assertEqual(run["verdict"], "BLOCKED")

    def test_quota_error_log_finishes_died_early_blocked(self):
        self.write_tsv([["ws15-exec-quota2", SID, 1792000000, DEEPSEEK]])
        (self.pad / "err-ws15-exec-quota2.log").write_text(
            "pi: insufficient credits (HTTP 429)\n", encoding="utf-8"
        )
        self.poll()
        run = self.store.runs[self.run_id("ws15-exec-quota2")]
        self.assertEqual(run["status"], "died_early")
        self.assertEqual(run["verdict"], "BLOCKED")

    def test_empty_out_with_error_reads_died_early(self):
        self.write_tsv([["ws15-exec-fail", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-fail.md").write_text("", encoding="utf-8")
        (self.pad / "err-ws15-exec-fail.log").write_text("boom rc=1\n", encoding="utf-8")
        self.poll()
        run = self.store.runs[self.run_id("ws15-exec-fail")]
        self.assertEqual(run["status"], "died_early")

    def test_source_emits_usage_from_the_stream(self):
        self.write_tsv([["ws15-exec-u", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-u.md").write_text("PASS\n", encoding="utf-8")
        (self.pad / "stream-ws15-exec-u.ndjson").write_bytes(FIXTURE.read_bytes())
        self.poll()
        rows = list(self.store.usage.values())
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["input"], rows[0]["cache_read"], rows[0]["output"]), (1900, 0, 38))
        self.assertEqual(rows[0]["run_id"], self.run_id("ws15-exec-u"))
        self.assertEqual(rows[0]["tool"], "pi")
        self.assertEqual(rows[0]["message_id"], f"{SID}-1")

    def test_stream_is_read_once_per_byte_cursor(self):
        self.write_tsv([["ws15-exec-once", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-once.md").write_text("PASS\n", encoding="utf-8")
        (self.pad / "stream-ws15-exec-once.ndjson").write_bytes(FIXTURE.read_bytes())
        self.poll()
        first = dict(self.store.usage)
        self.poll(now=1_800_000_002.0)
        self.assertEqual(self.store.usage, first)

    def test_chunked_reads_keep_the_full_stream_message_ids(self):
        # A stream read in two chunks must number its messages as the
        # launcher's full-stream read does, or the second chunk overwrites
        # the first chunk's rows.
        self.write_tsv([["ws15-exec-chunk", SID, 1792000000, MORPH]])
        (self.pad / "out-ws15-exec-chunk.md").write_text("PASS\n", encoding="utf-8")
        (self.pad / "stream-ws15-exec-chunk.ndjson").write_bytes(MORPH_FIXTURE.read_bytes())
        cfg = self.cfg()
        cfg["read_budgets"] = {"per_file": 500, "per_tick": 500}
        pi.poll(self.store, cfg, 1_800_000_000.0)
        pi.poll(self.store, cfg, 1_800_000_002.0)
        ids = sorted(row["message_id"] for row in self.store.usage.values())
        self.assertEqual(ids, [f"{MORPH_SID}-1", f"{MORPH_SID}-2"])

    def test_split_layout_reads_the_stream_in_pi(self):
        # run_worker.py writes the manifest in the run folder while
        # launch_pi.py writes the stream in <run_dir>/pi/.
        run_dir = self.tmp / "runs" / "ws15-exec-split"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi-sessions.tsv").write_text(
            HEADER + f"ws15-exec-split\t{SID}\t1792000000\t{MORPH}\n", encoding="utf-8"
        )
        (run_dir / "pi" / "stream-ws15-exec-split.ndjson").write_bytes(MORPH_FIXTURE.read_bytes())
        (run_dir / "pi" / "out-ws15-exec-split.md").write_text("PASS\n", encoding="utf-8")
        pi.poll(
            self.store,
            {"roots": {"scratchpads": [str(self.tmp / "runs")]}, "read_budgets": {}},
            1_800_000_000.0,
        )
        self.assertIsNone(pi.last_error)
        # The stream's session id is the run id, not the run-folder's
        # pre-generated one: the run-folder row and the pi/ row are one run.
        self.assertEqual(list(self.store.runs), [self.run_id("ws15-exec-split", MORPH_SID)])
        run = self.store.runs[self.run_id("ws15-exec-split", MORPH_SID)]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["verdict"], "PASS")
        rows = list(self.store.usage.values())
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["input"], rows[0]["cache_read"], rows[0]["output"]), (645, 59392, 209))
        self.assertEqual(rows[0]["run_id"], self.run_id("ws15-exec-split", MORPH_SID))
        self.assertEqual(rows[0]["session_id"], MORPH_SID)
        # pi reports the served id without the provider prefix.
        self.assertEqual(rows[0]["model"], "morph-dsv41flash-1017")

    def test_parse_rows_skips_header_and_short_rows(self):
        self.write_tsv([["ws15-exec-a", SID, 1792000000, DEEPSEEK]])
        rows = pi.parse_rows(HEADER + "short\trow\n")
        self.assertEqual(rows, [])

    def test_flat_run_dir_tsv_is_found(self):
        flat = self.tmp / "flat"
        flat.mkdir()
        (flat / "pi-sessions.tsv").write_text(
            HEADER + f"ws15-exec-flat\t{SID}\t1792000000\t{DEEPSEEK}\n", encoding="utf-8"
        )
        (flat / "out-ws15-exec-flat.md").write_text("PARTIAL\n", encoding="utf-8")
        pi.poll(self.store, {"roots": {"scratchpads": [str(flat)]}, "read_budgets": {}}, 1_800_000_000.0)
        self.assertIn(self.run_id("ws15-exec-flat"), self.store.runs)

    def test_live_pi_process_marks_the_run_running(self):
        label = "ws15-exec-live"
        self.write_tsv([[label, SID, 1792000000, DEEPSEEK]])
        (self.pad / "running").mkdir()
        (self.pad / "running" / label).write_text("424242\t%s" % DEEPSEEK, encoding="utf-8")
        original = pi._agent_processes
        pi._agent_processes = lambda: [
            {"pid": 424242, "comm": "node", "started_at": 1791999000.0, "label": label, "model": DEEPSEEK, "session_id": None}
        ]
        try:
            self.poll()
        finally:
            pi._agent_processes = original
        run = self.store.runs[self.run_id(label)]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], 424242)

    def test_stored_died_early_with_live_launcher_returns_to_running(self):
        label = "ws15-exec-revive"
        self.write_tsv([[label, SID, 1792000000, DEEPSEEK]])
        (self.pad / "running").mkdir()
        (self.pad / "running" / label).write_text("%d\t%s" % (os.getpid(), DEEPSEEK), encoding="utf-8")
        (self.pad / ("stream-%s.ndjson" % label)).write_bytes(FIXTURE.read_bytes())
        self.store.runs[self.run_id(label)] = {
            "run_id": self.run_id(label),
            "source": "pi",
            "label": label,
            "session_id": SID,
            "status": "died_early",
            "pid": None,
            "started_at": 1792000000.0,
            "ended_at": 1792000100.0,
        }
        original = pi._agent_processes
        pi._agent_processes = lambda: [
            {
                "pid": os.getpid(),
                "comm": "python3",
                "started_at": 1791999900.0,
                "label": label,
                "launcher": "pi",
                "model": DEEPSEEK,
                "session_id": None,
            }
        ]
        try:
            self.poll()
        finally:
            pi._agent_processes = original
        run = self.store.runs[self.run_id(label)]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pid"], os.getpid())

    def test_dead_launcher_closes_a_running_row_as_died_early(self):
        label = "ws15-exec-dead"
        self.write_tsv([[label, SID, 1792000000, DEEPSEEK]])
        (self.pad / "running").mkdir()
        (self.pad / "running" / label).write_text("999999999\t%s" % DEEPSEEK, encoding="utf-8")
        (self.pad / ("err-%s.log" % label)).write_text("boom rc=1\n", encoding="utf-8")
        self.store.runs[self.run_id(label)] = {
            "run_id": self.run_id(label),
            "source": "pi",
            "label": label,
            "session_id": SID,
            "status": "running",
            "pid": 999999999,
            "started_at": 1792000000.0,
        }
        original = pi._agent_processes
        pi._agent_processes = lambda: []
        try:
            self.poll()
        finally:
            pi._agent_processes = original
        run = self.store.runs[self.run_id(label)]
        self.assertEqual(run["status"], "died_early")

    def test_cmd_outdir_running_files_are_not_pi_runs(self):
        cmd = self.scratch / "enc2" / SID / "scratchpad" / "cmd"
        cmd.mkdir(parents=True)
        (cmd / "cmd-sessions.tsv").write_text(
            "label\tmodel\tsession_id\tstarted_epoch\tended_epoch\tinput_tokens\toutput_tokens\tstatus\n",
            encoding="utf-8",
        )
        (cmd / "running").mkdir()
        (cmd / "running" / "ws16-exec-f-wire").write_text("424242\tbeam", encoding="utf-8")
        self.poll()
        self.assertEqual(self.store.runs, {})

    def test_live_row_without_a_session_waits_for_the_next_poll(self):
        label = "ws15-exec-early"
        (self.pad / "running").mkdir()
        (self.pad / "running" / label).write_text("424242\t%s" % DEEPSEEK, encoding="utf-8")
        self.poll()
        self.assertEqual(self.store.runs, {})
        (self.pad / ("stream-%s.ndjson" % label)).write_bytes(FIXTURE.read_bytes())
        self.poll()
        run = self.store.runs[self.run_id(label)]
        self.assertEqual(run["session_id"], SID)
        self.assertNotIn("pi:%s:%s" % (label, label), self.store.runs)

    def test_run_id_stays_stable_from_stream_to_tsv(self):
        label = "ws15-exec-stable"
        (self.pad / "running").mkdir()
        (self.pad / "running" / label).write_text("424242\t%s" % DEEPSEEK, encoding="utf-8")
        (self.pad / ("stream-%s.ndjson" % label)).write_bytes(FIXTURE.read_bytes())
        self.poll()
        self.assertIn(self.run_id(label), self.store.runs)
        self.assertNotIn("pi:%s:%s" % (label, label), self.store.runs)
        (self.pad / "running" / label).unlink()
        self.write_tsv([[label, SID, 1792000000, DEEPSEEK]])
        (self.pad / ("out-%s.md" % label)).write_text("PASS\nDid it.\n", encoding="utf-8")
        self.poll()
        run = self.store.runs[self.run_id(label)]
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["verdict"], "PASS")
        self.assertNotIn("pi:%s:%s" % (label, label), self.store.runs)


class ProcessLivenessTest(unittest.TestCase):
    def test_pi_label_from_prompt_path(self):
        argv = (
            "424242 node /opt/x/node_modules/@mariozechner/pi-coding-agent/dist/cli.js "
            "-p --mode json --session-dir /tmp/s --model river/deepseek-ai/DeepSeek-V4.1-Flash "
            "@/tmp/pi/ws15-exec-wire.md Do the task"
        )
        self.assertEqual(process.pi_worker_label_from_args(argv), "ws15-exec-wire")

    def test_non_pi_node_has_no_label(self):
        self.assertIsNone(process.pi_worker_label_from_args("424242 node server.js @/x/ws15-exec-wire.md"))

    def test_pi_run_matches_by_label(self):
        procs = [{"pid": 7, "comm": "node", "started_at": 90, "label": "ws15-exec-wire"}]
        got = process.match_agent("pi", None, "ws15-exec-wire", procs)
        self.assertIsNotNone(got)
        self.assertEqual(got["pid"], 7)


class PriceTest(unittest.TestCase):
    def test_river_rows_resolve(self):
        table = pricing.catalog()
        for model, inrate, cr, out in (
            (DEEPSEEK, 0.12, 0.0024, 0.48),
            (GLM, 0.12, 0.024, 0.40),
        ):
            row = pricing._lookup(model, table)
            self.assertIsNotNone(row, model)
            self.assertEqual(row["provider"], "river")
            self.assertEqual(float(row["input"]), inrate)
            self.assertEqual(float(row["cache_read"]), cr)
            self.assertEqual(float(row["output"]), out)
            self.assertEqual(row["confidence"], "published")

    def test_reported_id_without_provider_prefix_resolves_too(self):
        table = pricing.catalog()
        for model in (DEEPSEEK_ID, GLM_ID):
            row = pricing._lookup(model, table)
            self.assertIsNotNone(row, model)
            self.assertEqual(row["provider"], "river")

    def test_no_time_bands(self):
        table = pricing.catalog()
        for model in (DEEPSEEK, GLM):
            row = pricing._lookup(model, table)
            self.assertIsNone(row.get("bands"))
            self.assertIsNone(row.get("long_context"))


class TileBucketTest(unittest.TestCase):
    def test_river_deepseek_buckets_as_deepseek(self):
        self.assertEqual(model_key(DEEPSEEK_ID, "pi"), ("deepseek", "deepseek-flash"))
        self.assertEqual(run_model_key({"model": DEEPSEEK_ID, "tier": "pi", "source": "pi"}), "deepseek-flash")

    def test_river_glm_buckets_as_glm(self):
        self.assertEqual(model_key(GLM_ID, "pi")[0], "glm")
        self.assertEqual(run_model_key({"model": GLM_ID, "tier": "pi", "source": "pi"}), "glm-5.3-flash")


class PhasesTest(unittest.TestCase):
    """A pi run with a ws mark lands on the exec view / planning card."""

    @classmethod
    def setUpClass(cls):
        from dash import phases

        phases.clear_caches()
        cls.phases = phases
        cls.rules, _digest, _version = phases.load_rules()

    def classify(self, label):
        run = {"run_id": f"pi:{SID}:{label}", "label": label, "source": "pi", "tier": "pi"}
        phase, _rule = self.phases.classify_run(run, {"task_id": None, "in_window": False}, rules=self.rules)
        return phase

    def test_exec_label_is_execution(self):
        self.assertEqual(self.classify("ws15-exec-pi-wire"), "execution")

    def test_plan_label_is_planning(self):
        self.assertEqual(self.classify("ws15-plan-pi-wire"), "planning")


class FixturePhasesTest(unittest.TestCase):
    """A pi run seeded into a real store splits on the planning card.

    The fixture-backed half of the requirement: a pi run with an ``exec``
    mark and a pi run with a ``plan`` mark, both with usage, seed the same
    store the Project home card reads (``dash/projectphases.py`` backs
    ``GET /api/project/phases``) and land in the right buckets.
    """

    def setUp(self):
        import time

        from dash import phases, projectphases
        from dash.store import Store

        phases.clear_caches()
        projectphases.clear_cache()
        self.projectphases = projectphases
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.store = Store(Path(self._tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(projectphases.clear_cache)
        self.addCleanup(phases.clear_caches)

    def add_pi_run(self, sid, label, model, start_off, inp, out, cache_read):
        run_id = f"pi:{sid}:{label}"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "pi",
                "tier": "pi",
                "model": model,
                "label": label,
                "session_id": sid,
                "status": "done",
                "verdict": "PASS",
                "started_at": self.now - start_off,
                "ended_at": self.now - start_off + 60,
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - start_off + 30,
                "session_id": sid,
                "run_id": run_id,
                "model": model,
                "tool": "pi",
                "message_id": f"{sid}-1",
                "input": inp,
                "output": out,
                "cache_read": cache_read,
                "cache_write": 0,
            }
        )

    def test_exec_and_plan_pi_runs_split_on_the_card(self):
        exec_sid = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        plan_sid = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        self.add_pi_run(exec_sid, "ws15-exec-pi-wire", GLM_ID, 300, 1000, 200, 400)
        self.add_pi_run(plan_sid, "ws15-plan-pi-wire", DEEPSEEK_ID, 400, 500, 100, 0)
        got = self.store.by_phase_all_time()
        ws = got.get("WS15")
        self.assertIsNotNone(ws, got)
        self.assertEqual(ws["execution"]["tokens"], 1000 + 200 + 400)
        self.assertEqual(ws["planning"]["tokens"], 500 + 100)
        self.assertGreater(ws["execution"]["cost_usd"], 0)
        payload = self.projectphases.build_phases(self.store, "all", self.now)
        row = next((item for item in payload["workstreams"] if item["ws"] == "WS15"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["by_phase"]["execution"]["runs"], 1)
        self.assertEqual(row["by_phase"]["planning"]["runs"], 1)

    def test_pi_runs_show_on_the_workstream_exec_view(self):
        exec_sid = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        plan_sid = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        self.add_pi_run(exec_sid, "ws15-exec-pi-wire", GLM_ID, 300, 1000, 200, 400)
        self.add_pi_run(plan_sid, "ws15-plan-pi-wire", DEEPSEEK_ID, 400, 500, 100, 0)
        runs = {r["run_id"]: r for r in self.store.query_runs(self.now, "WS15", None, None, 100)}
        exec_run = runs.get(f"pi:{exec_sid}:ws15-exec-pi-wire")
        plan_run = runs.get(f"pi:{plan_sid}:ws15-plan-pi-wire")
        self.assertIsNotNone(exec_run)
        self.assertIsNotNone(plan_run)
        self.assertEqual(exec_run["workstream"], "WS15")
        self.assertEqual(exec_run["source"], "pi")
        self.assertEqual(exec_run["status"], "done")
        self.assertEqual(exec_run["verdict"], "PASS")


class PiTierColumnTest(SourceTest):
    """The recorded tier beats the model family, so no pi run lands in ``pi``."""

    def write_tiered(self, rows):
        text = "label\tsession_id\tstarted_epoch\tmodel\ttier\n" + "".join(
            "\t".join(str(x) for x in row) + "\n" for row in rows
        )
        (self.pad / "pi-sessions.tsv").write_text(text, encoding="utf-8")

    def test_recorded_tier_is_the_run_tier(self):
        self.write_tiered(
            [
                ["ws16-exec-ds", SID, 1792000000, "river/deepseek-ai/DeepSeek-V4.1-Flash-1017", "pi-deepseek"],
                ["ws16-exec-beam", "beam-sid", 1792000001, "reflection/Beam-1.0-501B-A23B", "pi-beam"],
            ]
        )
        (self.pad / "out-ws16-exec-ds.md").write_text("PASS\nok\n", encoding="utf-8")
        (self.pad / "out-ws16-exec-beam.md").write_text("PASS\nok\n", encoding="utf-8")
        self.poll()
        ds = self.store.runs[self.run_id("ws16-exec-ds")]
        beam = self.store.runs["pi:beam-sid:ws16-exec-beam"]
        self.assertEqual(ds["tier"], "pi-deepseek")
        self.assertEqual(beam["tier"], "pi-beam")
        self.assertEqual(ds["model"], "river/deepseek-ai/DeepSeek-V4.1-Flash-1017")
        self.assertEqual(beam["model"], "reflection/Beam-1.0-501B-A23B")

    def test_meta_file_supplies_the_tier_when_the_row_has_none(self):
        self.write_tsv([["ws16-exec-meta", SID, 1792000000, DEEPSEEK_ID]])
        (self.pad / "ws16-exec-meta.meta.json").write_text(
            '{"tier": "pi-deepseek", "harness": "pi", "model": "%s"}' % DEEPSEEK_ID,
            encoding="utf-8",
        )
        self.poll()
        self.assertEqual(self.store.runs[self.run_id("ws16-exec-meta")]["tier"], "pi-deepseek")

    def test_old_rows_still_read_the_model_family(self):
        self.write_tsv([["ws16-exec-old", SID, 1792000000, DEEPSEEK_ID]])
        self.poll()
        self.assertEqual(self.store.runs[self.run_id("ws16-exec-old")]["tier"], "deepseek")


class ModelTierTest(unittest.TestCase):
    def test_model_family_is_the_tier(self):
        self.assertEqual(pi._model_tier("reflection/Beam-1.0-501B-A23B"), "beam")
        self.assertEqual(pi._model_tier("river/zai-org/GLM-5.3-Flash"), "glm")
        self.assertEqual(pi._model_tier("river/deepseek-ai/DeepSeek-V4.1-Flash-1017"), "deepseek")
        self.assertEqual(pi._model_tier(MORPH), "deepseek")
        self.assertEqual(pi._model_tier("morph/morph-dsv41flash"), "deepseek")
        self.assertEqual(pi._model_tier("morph/morph-kimik31"), "kimi")
        self.assertEqual(pi._model_tier("morph/morph-kimik31:low"), "kimi")
        self.assertEqual(pi._model_tier("kimi-k3.1"), "kimi")
        self.assertEqual(pi._model_tier(None), "pi")

    def test_store_family_tier_matches_source(self):
        from dash.store.runs import _family_tier

        # A healed pi row takes the registry tier its launch would record
        # (``pi-beam``), so healing and a fresh launch agree.
        for model, tier in (
            ("reflection/Beam-1.0-501B-A23B", "pi-beam"),
            ("reflection/beam:free", "pi-beam"),
            ("river/zai-org/GLM-5.3-Flash", "pi-glm"),
            ("river/deepseek-ai/DeepSeek-V4.1-Flash-1017", "pi-deepseek"),
            ("deepseek-ai/DeepSeek-V4.1-Flash", "pi-deepseek"),
            (MORPH, "pi-deepseek"),
            ("morph-dsv41flash-1017", "pi-deepseek"),
        ):
            self.assertEqual(_family_tier("pi", model), tier)
            self.assertEqual(_family_tier("pi", model)[3:], pi._model_tier(model))
        self.assertEqual(_family_tier("generic", "river/zai-org/GLM-5.3-Flash"), "glm")
        self.assertIsNone(_family_tier("pi", "mystery-model-9"))
        self.assertIsNone(_family_tier("pi", None))

    def test_tier_of_prefers_the_recorded_tier(self):
        self.assertEqual(pi._tier_of({"tier": "pi-kimi", "model": "morph/morph-kimik31"}), "pi-kimi")
        self.assertEqual(pi._tier_of({"tier": "pi-beam", "model": DEEPSEEK}), "pi-beam")
        self.assertEqual(pi._tier_of({"meta": {"tier": "pi-deepseek"}, "model": GLM}), "pi-deepseek")
        self.assertEqual(pi._tier_of({"model": DEEPSEEK}), "deepseek")


class CacheWriteSplitTest(unittest.TestCase):
    """pi's cacheWrite1h becomes the 1h part; the 5m part is the remainder."""

    def test_present_field_splits_and_remainder_is_5m(self):
        self.assertEqual(pi.write_split({"cacheWrite": 1000, "cacheWrite1h": 300}, "claude-haiku-5-5"), (700, 300))
        self.assertEqual(pi.write_split({"cacheWrite": 1000, "cacheWrite1h": 0}, DEEPSEEK_ID), (1000, 0))

    def test_one_hour_part_is_clamped_to_the_total(self):
        self.assertEqual(pi.write_split({"cacheWrite": 40, "cacheWrite1h": 90}, "claude-haiku-5-5"), (0, 40))
        self.assertEqual(pi.write_split({"cacheWrite": 40, "cacheWrite1h": -5}, "claude-haiku-5-5"), (40, 0))

    def test_absent_field_on_anthropic_is_all_5m(self):
        self.assertEqual(pi.write_split({"cacheWrite": 5000}, "claude-haiku-5-5"), (5000, 0))
        self.assertEqual(pi.write_split({"cacheWrite": 0}, "anthropic/claude-haiku-5-5"), (0, 0))

    def test_absent_field_off_anthropic_keeps_no_split(self):
        for model in (DEEPSEEK_ID, GLM_ID, "reflection/Beam-1.0-501B-A23B", None):
            self.assertEqual(pi.write_split({"cacheWrite": 5000}, model), (None, None), model)

    def test_non_numeric_field_counts_as_absent(self):
        self.assertEqual(pi.write_split({"cacheWrite": 7, "cacheWrite1h": "3"}, "claude-haiku-5-5"), (7, 0))
        self.assertEqual(pi.write_split({"cacheWrite": 7, "cacheWrite1h": True}, DEEPSEEK_ID), (None, None))

    def test_parse_stream_events_carry_the_split(self):
        lines = [
            {"type": "message_end", "timestamp": 1_792_000_000_000, "message": {
                "role": "assistant", "model": "claude-haiku-5-5",
                "usage": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 100, "cacheWrite1h": 40}}},
            {"type": "message_end", "timestamp": 1_792_000_001_000, "message": {
                "role": "assistant", "model": "claude-haiku-5-5",
                "usage": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 100}}},
        ]
        raw = b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"
        first, second = pi.parse_stream(raw, SID, default_model="anthropic/claude-haiku-5-5")
        self.assertEqual((first["cache_write"], first["cache_write_5m"], first["cache_write_1h"]), (100, 60, 40))
        self.assertEqual((second["cache_write_5m"], second["cache_write_1h"]), (100, 0))

    def test_default_model_decides_when_the_message_names_none(self):
        raw = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "usage": {"input": 1, "cacheWrite": 9}}}).encode() + b"\n"
        self.assertEqual(pi.parse_stream(raw, SID)[0]["cache_write_5m"], None)
        self.assertEqual(pi.parse_stream(raw, SID, default_model="anthropic/claude-haiku-5-5")[0]["cache_write_5m"], 9)


class CacheWriteChainTest(unittest.TestCase):
    """parse_stream -> usage line -> generic reader -> store -> cost, on a real store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "dash.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def test_split_reaches_the_stored_cost(self):
        from dash.sources import generic
        from dash.store import Store

        def msg(index, usage):
            return json.dumps({"type": "message_end", "timestamp": 1_792_000_000_000 + index, "message": {
                "role": "assistant", "model": "claude-haiku-5-5", "usage": usage}}).encode()

        raw = b"\n".join([
            msg(1, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 50_000, "cacheWrite1h": 20_000}),
            msg(2, {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 50_000}),
        ]) + b"\n"
        events = pi.parse_stream(raw, SID, default_model="anthropic/claude-haiku-5-5")
        store = Store(self.db)
        try:
            for index, ev in enumerate(events):
                line = launch.usage_line(ev, SID, "ws1-exec-x", "anthropic/claude-haiku-5-5", 1_792_000_100.0)
                row = generic._usage_row(json.loads(json.dumps(line)), index, 1_792_000_100.0)
                store.add_usage(row)
            costs = dict(store._conn.execute("SELECT message_id, est_cost_usd FROM usage_events").fetchall())
        finally:
            store.close()
        # 30k at the 5m rate (0.125) + 20k at the 1h rate (0.2), then 50k all at 5m.
        self.assertAlmostEqual(costs[f"{SID}-1"], 0.00775, places=9)
        self.assertAlmostEqual(costs[f"{SID}-2"], 0.00625, places=9)


class StreamCostTest(unittest.TestCase):
    """The split layout's stream prices a Morph DeepSeek run end to end."""

    def test_split_layout_run_has_tokens_and_cost(self):
        from dash.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name) / "runs" / "ws15-exec-morph"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi-sessions.tsv").write_text(
            HEADER + f"ws15-exec-morph\t{SID}\t1792000000\t{MORPH}\n", encoding="utf-8"
        )
        (run_dir / "pi" / "stream-ws15-exec-morph.ndjson").write_bytes(MORPH_FIXTURE.read_bytes())
        (run_dir / "pi" / "out-ws15-exec-morph.md").write_text("PASS\n", encoding="utf-8")
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        pi._tsv_cache.clear()
        pi.poll(
            store,
            {"roots": {"scratchpads": [str(Path(tmp.name) / "runs")]}, "read_budgets": {}},
            1_800_000_000.0,
        )
        runs = {r["run_id"]: r for r in store.query_runs(1_800_000_000.0, "WS15", None, None, 100)}
        run = runs[f"pi:{MORPH_SID}:ws15-exec-morph"]
        self.assertGreater(run["tokens"]["output"], 0)
        self.assertGreater(run["tokens"]["cache_read"], 0)
        self.assertGreater(run["cost_usd"], 0)
        self.assertEqual(run["model"], MORPH)

    def test_pre_generated_twin_folds_into_the_stream_run(self):
        # run_worker.py records a session id before pi starts; pi picks its
        # own and the launcher records it in <run_dir>/pi/pi-sessions.tsv.
        # The store must list one run, carrying the stream's tokens and cost,
        # and a re-read from offset 0 must not double count.
        from dash.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        label = "ws15-exec-twin"
        run_dir = Path(tmp.name) / "runs" / label
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi-sessions.tsv").write_text(
            HEADER + f"{label}\t{SID}\t1792000000\t{MORPH}\n", encoding="utf-8"
        )
        (run_dir / "pi" / "pi-sessions.tsv").write_text(
            HEADER + f"{label}\t{MORPH_SID}\t1792000000\t{MORPH}\n", encoding="utf-8"
        )
        stream = run_dir / "pi" / f"stream-{label}.ndjson"
        stream.write_bytes(MORPH_FIXTURE.read_bytes())
        (run_dir / "pi" / f"out-{label}.md").write_text("PASS\n", encoding="utf-8")
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        # The pre-generated row the old poll left behind: listed, tokenless.
        store.upsert_run(
            {
                "run_id": f"pi:{SID}:{label}",
                "source": "pi",
                "label": label,
                "session_id": SID,
                "status": "done",
                "started_at": 1792000000.0,
            }
        )
        pi._tsv_cache.clear()
        cfg = {"roots": {"scratchpads": [str(Path(tmp.name) / "runs")]}, "read_budgets": {}}
        pi.poll(store, cfg, 1_800_000_000.0)
        runs = {r["run_id"]: r for r in store.query_runs(1_800_000_000.0, "WS15", None, None, 100)}
        self.assertEqual(list(runs), [f"pi:{MORPH_SID}:{label}"])
        run = runs[f"pi:{MORPH_SID}:{label}"]
        self.assertGreater(run["tokens"]["output"], 0)
        self.assertGreater(run["cost_usd"], 0)
        self.assertIsNone(store.get_run(f"pi:{SID}:{label}"))
        first = dict(run["tokens"])
        first_cost = run["cost_usd"]
        # Re-read the stream from offset 0: the same message ids upsert, so
        # the run's tokens and cost do not grow.
        st = stream.stat()
        store.set_offset("pi:stream:" + str(stream), st.st_ino, 0, 0, 0)
        pi.poll(store, cfg, 1_800_000_002.0)
        runs = {r["run_id"]: r for r in store.query_runs(1_800_000_002.0, "WS15", None, None, 100)}
        self.assertEqual(list(runs), [f"pi:{MORPH_SID}:{label}"])
        self.assertEqual(runs[f"pi:{MORPH_SID}:{label}"]["tokens"], first)
        self.assertEqual(runs[f"pi:{MORPH_SID}:{label}"]["cost_usd"], first_cost)


class NonAnthropicPricingUnchangedTest(unittest.TestCase):
    """DeepSeek, GLM and Beam rows have no 5m/1h split to gain: their cost is unchanged."""

    MODELS = (
        DEEPSEEK,
        DEEPSEEK_ID,
        GLM,
        GLM_ID,
        "reflection/Beam-1.0-501B-A23B",
        "reflection/Beam-1.0-501B-A23B:xhigh",
        "reflection-beam",
    )

    def test_cost_with_the_split_equals_cost_without_it(self):
        table = pricing.catalog()
        for model in self.MODELS:
            for cache_write in (0, 1, 5000, 123_457):
                before = pricing.cost(model, 1000, 200, 300, cache_write, table)
                split = pi.write_split({"cacheWrite": cache_write, "cacheWrite1h": 0}, model)
                after = pricing.cost(model, 1000, 200, 300, cache_write, table, cache_write_5m=split[0], cache_write_1h=split[1])
                self.assertEqual(before, after, (model, cache_write))

    def test_absent_field_leaves_the_pi_event_unsplit(self):
        for model in self.MODELS:
            self.assertEqual(pi.write_split({"cacheWrite": 5000}, model), (None, None), model)

    def test_writes_on_a_rateless_write_class_stay_unpriced(self):
        # DeepSeek publishes no cache-write rate: a write is unpriced, with or without the split.
        self.assertIsNone(pricing.cost(DEEPSEEK_ID, 10, 10, 0, 5000))
        self.assertIsNone(pricing.cost(DEEPSEEK_ID, 10, 10, 0, 5000, cache_write_5m=5000, cache_write_1h=0))
        self.assertEqual(pricing.cost("reflection/Beam-1.0-501B-A23B", 10, 10, 0, 0), 0.0)


if __name__ == "__main__":
    unittest.main()

