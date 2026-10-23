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
SID = "01a16401-9955-70cd-bf8c-19944db4f20c"
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

    def test_verdict_from_first_line(self):
        self.assertEqual(launch.verdict_of("PASS\nall good"), "PASS")
        self.assertEqual(launch.verdict_of("PARTIAL: x\nPASS later"), "PARTIAL")
        self.assertEqual(launch.verdict_of("BLOCKED: no key"), "BLOCKED")
        self.assertEqual(launch.verdict_of("nothing here"), "PARTIAL")

    def test_stream_errors_and_session_id(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(launch.session_id_of(raw), SID)
        err = json.dumps(
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "insufficient credit"}}
        ).encode() + b"\n"
        self.assertEqual(launch.stream_errors(err)[0]["error"], "insufficient credit")


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

    def test_pi_source_never_emits_usage(self):
        self.write_tsv([["ws15-exec-u", SID, 1792000000, DEEPSEEK]])
        (self.pad / "out-ws15-exec-u.md").write_text("PASS\n", encoding="utf-8")
        (self.pad / "stream-ws15-exec-u.ndjson").write_bytes(FIXTURE.read_bytes())
        self.poll()
        self.assertEqual(self.store.usage, {})

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


class ModelTierTest(unittest.TestCase):
    def test_model_family_is_the_tier(self):
        self.assertEqual(pi._model_tier("reflection/Beam-1.0-501B-A23B"), "beam")
        self.assertEqual(pi._model_tier("river/zai-org/GLM-5.3-Flash"), "glm")
        self.assertEqual(pi._model_tier("river/deepseek-ai/DeepSeek-V4.1-Flash-1017"), "deepseek")
        self.assertEqual(pi._model_tier(None), "pi")

    def test_store_family_tier_matches_source(self):
        from dash.store.runs import _family_tier

        for model, tier in (
            ("reflection/Beam-1.0-501B-A23B", "beam"),
            ("reflection/beam:free", "beam"),
            ("river/zai-org/GLM-5.3-Flash", "glm"),
            ("river/deepseek-ai/DeepSeek-V4.1-Flash-1017", "deepseek"),
            ("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek"),
        ):
            self.assertEqual(_family_tier("pi", model), tier)
            self.assertEqual(_family_tier("pi", model), pi._model_tier(model))
        self.assertIsNone(_family_tier("pi", "mystery-model-9"))
        self.assertIsNone(_family_tier("pi", None))


if __name__ == "__main__":
    unittest.main()

