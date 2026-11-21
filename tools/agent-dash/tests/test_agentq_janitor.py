"""agentq janitor: scratch reaper, job-dir retention, disk-pressure admission.

Offline, and every path is a temp folder: the agentq home, the scratch root and the
job folders. No test reads the real disk, the real scratch root or the real slot
state, so a full laptop cannot change a result here. Timestamps are real wall-clock
values, because retention compares a file's mtime with the cutoff.
"""

import concurrent.futures
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import cli, db, janitor, slots  # noqa: E402
from agentq import headroom as H  # noqa: E402

LABEL = "hub-exec-janitor"
DAY = 86400.0


class FakeGauges:
    """Laptop headroom with fixed readings, so the laptop gate never holds a test job."""

    def __init__(self, free=90.0, cpu=0.2, swap=0.1, load=0.2):
        self.reading = H.Headroom(free, cpu, swap, load)

    def read(self):
        return self.reading


class InlinePool:
    """Runs each launch on the calling thread, so a tick's launches have landed when it returns."""

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # pragma: no cover - a launch never raises in these tests
            future.set_exception(exc)
        return future


class FakeLauncher:
    """Writes a pi-shaped report and starts ``sleep <live_s>`` as the worker."""

    def __init__(self, live_s=30.0):
        self.live_s = live_s
        self.procs = []

    def __call__(self, job, run_dir, env):
        pi = Path(run_dir) / "pi"
        pi.mkdir(parents=True, exist_ok=True)
        (pi / f"out-{job['label']}.md").write_text("PASS\nall green\n", encoding="utf-8")
        proc = subprocess.Popen(["sleep", str(self.live_s)], start_new_session=True)
        self.procs.append(proc)
        (Path(run_dir) / f"{job['label']}.pid").write_text(str(proc.pid), encoding="utf-8")
        return proc.pid

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def touch(path, ts):
    """Set a file's mtime, so retention's "modified after the cutoff" guard is exercised."""
    os.utime(str(path), (ts, ts))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.now = time.time()
        self.env = {
            "AGENTQ_HOME": str(self.home),
            "AGENTQ_SESSION": "sess-janitor",
            "AGENTQ_HARNESS": "pi",
            "AGENT_DASH_SCRATCH_ROOT": str(self.scratch),
            "PATH": os.environ["PATH"],
        }
        patcher = mock.patch.dict(os.environ, {"AGENT_DASH_SCRATCH_ROOT": str(self.scratch)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)
        self.launchers = []

    def tearDown(self):
        for launcher in self.launchers:
            launcher.close()

    # -- fixtures ---------------------------------------------------------

    def job(self, *, state="done", finished_at=None, label=LABEL, kind="read", res="light",
            prompt="Do the thing.\n", run_dir=None, verdict="PASS"):
        """One job row in ``state``, with its prompt on disk. Returns the job id."""
        job_id = db.new_job_id()
        prompt_path = db.job_dir(self.home, job_id) / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        job = {
            "id": job_id, "lane": "normal", "kind": kind, "res": res, "tier": "pi-deepseek-flash",
            "label": label, "workspace": str(self.tmp), "session": "sess-janitor", "harness": "pi",
            "prompt_path": str(prompt_path),
        }  # fmt: skip
        with db.tx(self.conn):
            db.insert_job(self.conn, job, self.now - 2 * DAY)
        if state != "queued":
            db.transition(self.conn, job_id, ("queued",), self.now - 2 * DAY, None, "",
                          state=state, finished_at=finished_at, run_dir=run_dir, verdict=verdict)
        return job_id

    def row(self, job_id):
        return db.get_job(self.conn, job_id)

    def scratch_folder(self, label=LABEL, *, group="hub", run_dir="", marker=True, size=4096):
        """A scratch folder laid out the way ``run_worker.py`` creates one."""
        folder = self.scratch / group / label
        folder.mkdir(parents=True, exist_ok=True)
        if marker:
            (folder / janitor.OWNER_FILE).write_text(
                json.dumps({"label": label, "workstream": group, "run_dir": run_dir, "created_at": 1}),
                encoding="utf-8",
            )
        (folder / "cache.bin").write_bytes(b"x" * size)
        return folder

    def run_dir(self, job_id, *, live=False):
        """A run folder for ``job_id``, optionally with a live pid in ``running/``."""
        path = db.job_dir(self.home, job_id) / "runs" / "abcd1234"
        path.mkdir(parents=True, exist_ok=True)
        if live:
            running = path / "running"
            running.mkdir(parents=True, exist_ok=True)
            (running / LABEL).write_text("%d\tpi\n" % os.getpid(), encoding="utf-8")
        return path

    def submit(self, *args, label=None):
        out = io.StringIO()
        (self.tmp / "prompt.md").write_text("Do the thing.\n", encoding="utf-8")
        argv = ["submit", str(self.tmp / "prompt.md"), "--label", label or LABEL, *args]
        code = cli.main(argv, env=self.env, out=out, now=lambda: self.now)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue().splitlines()[0]

    def broker(self, free):
        launcher = FakeLauncher()
        self.launchers.append(launcher)
        return B.Broker(self.home, launcher=launcher, now=lambda: self.now, proxy=lambda: None,
                        gauges=FakeGauges(), launch_pool=InlinePool(), disk_free=lambda: free["gb"])


class ScratchReaperTests(Base):
    def test_a_finished_job_scratch_older_than_the_grace_is_removed(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual([item["path"] for item in report["removed"]], [str(folder)])
        self.assertFalse(folder.exists())
        self.assertGreater(report["bytes"], 0)

    def test_a_finished_job_inside_the_grace_is_kept(self):
        job_id = self.job(finished_at=self.now - 1800.0)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_a_running_job_scratch_is_kept(self):
        job_id = self.job(state="running", finished_at=None)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_a_queued_job_scratch_is_kept(self):
        job_id = self.job(state="queued", finished_at=None)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_an_unmarked_directory_is_never_touched(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder(marker=False)
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_a_directory_whose_marker_names_another_label_is_never_touched(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        (folder / janitor.OWNER_FILE).write_text(
            json.dumps({"label": "hub-exec-someone-else", "run_dir": ""}), encoding="utf-8")
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_a_folder_owned_by_another_job_with_the_same_label_is_kept(self):
        other = self.job(finished_at=self.now - 7200.0)
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder(run_dir=str(self.run_dir(other)))
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_a_live_pid_in_the_run_folder_keeps_the_scratch(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        row = dict(self.row(job_id), run_dir=str(self.run_dir(job_id, live=True)))
        report = janitor.reap_scratch(self.home, [row], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_keep_scratch_in_the_front_matter_opts_out(self):
        job_id = self.job(finished_at=self.now - 7200.0,
                          prompt="---\nkeep_scratch: true\n---\nDo the thing.\n")
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())

    def test_keep_scratch_false_does_not_opt_out(self):
        job_id = self.job(finished_at=self.now - 7200.0,
                          prompt="---\nkeep_scratch: false\n---\nDo the thing.\n")
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual([item["path"] for item in report["removed"]], [str(folder)])
        self.assertFalse(folder.exists())

    def test_a_symlinked_folder_is_never_followed(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.bin").write_bytes(b"y" * 128)
        group = self.scratch / "hub"
        group.mkdir(parents=True, exist_ok=True)
        (group / LABEL).symlink_to(outside)
        report = janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue((outside / "keep.bin").is_file())

    def test_a_marker_without_a_run_folder_is_kept_while_the_label_is_live(self):
        done = self.job(finished_at=self.now - 7200.0)
        self.job(state="running", finished_at=None)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, db.select_jobs(self.conn), now=self.now,
                                      grace_s=3600.0)
        self.assertEqual(report["removed"], [])
        self.assertTrue(folder.exists())
        self.assertEqual(self.row(done)["state"], "done")

    def test_a_marker_without_a_run_folder_goes_once_the_label_is_idle(self):
        self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        report = janitor.reap_scratch(self.home, db.select_jobs(self.conn), now=self.now,
                                      grace_s=3600.0)
        self.assertEqual([item["path"] for item in report["removed"]], [str(folder)])
        self.assertFalse(folder.exists())

    def test_the_index_reads_the_root_once_for_every_job(self):
        first = self.job(finished_at=self.now - 7200.0, label="hub-exec-one")
        second = self.job(finished_at=self.now - 7200.0, label="hub-exec-two")
        one = self.scratch_folder("hub-exec-one")
        two = self.scratch_folder("hub-exec-two", group="WS4")
        index = janitor.scratch_index(self.scratch)
        self.assertEqual(sorted(index), ["hub-exec-one", "hub-exec-two"])
        report = janitor.reap_scratch(self.home, [self.row(first), self.row(second)], now=self.now,
                                      grace_s=3600.0, index=index)
        self.assertEqual(sorted(item["path"] for item in report["removed"]), sorted([str(one), str(two)]))
        self.assertFalse(one.exists())
        self.assertFalse(two.exists())


class RetentionTests(Base):
    def big_job(self, *, finished_at=None, size=10_000):
        finished_at = self.now - 8 * DAY if finished_at is None else finished_at
        job_id = self.job(finished_at=finished_at)
        job_root = db.job_dir(self.home, job_id)
        (job_root / "report.md").write_text("PASS\nall green\n", encoding="utf-8")
        run = job_root / "runs" / "abcd1234"
        (run / "pi" / "sessions").mkdir(parents=True, exist_ok=True)
        stream = run / "pi" / f"stream-{LABEL}.ndjson"
        stream.write_text("s" * size, encoding="utf-8")
        err = run / f"err-{LABEL}.log"
        err.write_text("e" * size, encoding="utf-8")
        session = run / "pi" / "sessions" / "2026-11-19T00-00-00-000Z_abc.jsonl"
        session.write_text("j" * size, encoding="utf-8")
        out = run / f"out-{LABEL}.md"
        out.write_text("PASS\n", encoding="utf-8")
        for path in (stream, err, session, out):
            touch(path, finished_at)
        return job_id, job_root, stream, err, session, out

    def test_retention_keeps_the_prompt_the_report_and_the_verdict(self):
        job_id, job_root, stream, err, session, out = self.big_job()
        report = janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        self.assertTrue((job_root / "prompt.md").is_file())
        self.assertTrue((job_root / "report.md").is_file())
        self.assertTrue(out.is_file())
        self.assertEqual(self.row(job_id)["verdict"], "PASS")
        self.assertFalse(stream.exists())
        self.assertTrue(stream.with_name(stream.name + ".gz").is_file())
        self.assertFalse(err.exists())
        self.assertFalse(session.exists())
        self.assertGreater(report["bytes"], 0)
        self.assertEqual(report["files"], 3)

    def test_retention_leaves_a_file_over_the_gzip_cap_for_the_archive(self):
        # Never deleted: a log leaves its path only after the log archive has a
        # verified zstd copy and its rows are in the store (docs/log-archive.md).
        job_id, _job_root, stream, err, session, _out = self.big_job(size=4096)
        report = janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0, gzip_max=1024)
        self.assertTrue(stream.is_file())
        self.assertTrue(err.is_file())
        self.assertTrue(session.is_file())
        self.assertFalse(stream.with_name(stream.name + ".gz").exists())
        self.assertEqual(report["needs_archive"], 3)
        self.assertEqual(report["files"], 0)
        self.assertEqual(report["bytes"], 0)

    def test_retention_leaves_a_job_inside_the_window_alone(self):
        job_id, _job_root, stream, _err, _session, _out = self.big_job(finished_at=self.now - 6 * DAY)
        report = janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        self.assertEqual(report["jobs"], [])
        self.assertTrue(stream.is_file())

    def test_retention_leaves_a_running_job_alone(self):
        job_id, _job_root, stream, _err, _session, _out = self.big_job()
        row = dict(self.row(job_id), state="running")
        report = janitor.retention(self.home, [row], now=self.now, keep_days=7.0)
        self.assertEqual(report["jobs"], [])
        self.assertTrue(stream.is_file())

    def test_retention_skips_a_job_whose_run_folder_is_still_live(self):
        job_id, _job_root, stream, _err, _session, _out = self.big_job()
        row = dict(self.row(job_id), run_dir=str(self.run_dir(job_id, live=True)))
        report = janitor.retention(self.home, [row], now=self.now, keep_days=7.0)
        self.assertEqual(report["jobs"], [])
        self.assertTrue(stream.is_file())

    def test_retention_skips_a_file_modified_after_the_cutoff(self):
        job_id, _job_root, stream, _err, _session, _out = self.big_job()
        touch(stream, self.now)
        janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        self.assertTrue(stream.is_file())

    def test_retention_touches_at_most_the_pass_limit(self):
        ids = [self.big_job()[0] for _ in range(3)]
        report = janitor.retention(self.home, [self.row(job_id) for job_id in ids],
                                   now=self.now, keep_days=7.0, jobs_per_pass=2)
        self.assertEqual(len(report["jobs"]), 2)

    def test_a_second_pass_finds_nothing_left_to_do(self):
        job_id, _job_root, _stream, _err, _session, _out = self.big_job()
        janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        again = janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        self.assertEqual(again["jobs"], [])


class SettingsTests(Base):
    def test_defaults(self):
        cfg = janitor.settings()
        self.assertEqual(cfg["scratch_grace_s"], 3600.0)
        self.assertEqual(cfg["jobs_keep_days"], 7.0)
        self.assertEqual(cfg["disk_floor_gb"], 25.0)
        self.assertEqual(cfg["resume_gb"], 30.0)

    def test_the_knobs_come_from_the_janitor_section_and_the_slots_floor(self):
        cfg = janitor.settings({"scratch_grace_s": 60, "jobs_keep_days": 2}, {"disk_floor_gb": 12.5})
        self.assertEqual(cfg["scratch_grace_s"], 60.0)
        self.assertEqual(cfg["jobs_keep_days"], 2.0)
        self.assertEqual(cfg["disk_floor_gb"], 12.5)
        self.assertEqual(cfg["resume_gb"], 17.5)

    def test_a_bad_value_falls_back_to_the_default(self):
        cfg = janitor.settings({"scratch_grace_s": "soon"}, {"disk_floor_gb": "lots"})
        self.assertEqual(cfg["scratch_grace_s"], 3600.0)
        self.assertEqual(cfg["disk_floor_gb"], 25.0)

    def test_load_config_reads_the_janitor_section_and_keeps_the_floor_out_of_the_caps(self):
        (self.home / "broker.json").write_text(
            json.dumps({"slots": {"tests": 3, "disk_floor_gb": 12.5}, "janitor": {"jobs_keep_days": 2}}),
            encoding="utf-8",
        )
        cfg = B.load_config(self.home)
        self.assertEqual(cfg["janitor"]["disk_floor_gb"], 12.5)
        self.assertEqual(cfg["janitor"]["jobs_keep_days"], 2.0)
        self.assertEqual(cfg["slots"]["tests"], 3)
        self.assertNotIn("disk_floor_gb", cfg["slots"])


class DiskStateTests(Base):
    def test_below_the_floor_the_gate_is_active(self):
        state = janitor.disk_state(self.conn, self.home, floor_gb=25.0, free=10.0)
        self.assertTrue(state["active"])
        self.assertEqual(state["resume_gb"], 30.0)
        self.assertEqual(state["held"], 0)

    def test_above_the_floor_the_gate_is_open(self):
        state = janitor.disk_state(self.conn, self.home, floor_gb=25.0, free=40.0)
        self.assertFalse(state["active"])

    def test_an_unreadable_volume_never_holds_work(self):
        with mock.patch.object(janitor, "free_gb", return_value=None):
            state = janitor.disk_state(self.conn, self.home, floor_gb=25.0)
        self.assertIsNone(state["free_gb"])
        self.assertFalse(state["active"])

    def test_the_line_names_free_space_the_floor_and_the_held_count(self):
        line = janitor.format_disk(janitor.disk_state(self.conn, self.home, floor_gb=25.0, free=10.0))
        self.assertIn("10.0 GB free", line)
        self.assertIn("floor 25 GB", line)
        self.assertIn("0 job(s) held", line)
        self.assertIn("gate held", line)

    def test_free_gb_of_a_missing_root_uses_the_nearest_parent(self):
        self.assertIsNotNone(janitor.free_gb(self.tmp / "nope" / "deeper"))

    def test_held_count_reads_the_detail_the_broker_writes(self):
        job_id = self.job(state="queued", finished_at=None)
        db.transition(self.conn, job_id, ("queued",), self.now, None, "",
                      detail="hold disk: disk pressure: 10.0 GB free, floor 25 GB")
        self.assertEqual(janitor.held_count(self.conn), 1)


class DiskAdmissionTests(Base):
    def test_admission_holds_below_the_floor_and_resumes_above_floor_plus_five(self):
        write = self.submit("--kind", "write", "--in-place", label="hub-exec-write")
        tests = self.submit("--res", "tests", label="hub-exec-tests")
        read = self.submit(label="hub-exec-read")
        free = {"gb": 10.0}
        broker = self.broker(free)
        self.assertEqual(broker.tick(), [read])
        self.assertEqual(self.row(write)["detail"],
                         "hold disk: disk pressure: 10.0 GB free, floor 25 GB")
        self.assertEqual(self.row(tests)["detail"],
                         "hold disk: disk pressure: 10.0 GB free, floor 25 GB")
        self.assertEqual(self.row(write)["state"], "queued")
        self.assertEqual(self.row(tests)["state"], "queued")
        self.assertEqual(janitor.held_count(self.conn), 2)

    def test_the_gate_stays_closed_between_the_floor_and_floor_plus_five(self):
        write = self.submit("--kind", "write", "--in-place", label="hub-exec-write")
        free = {"gb": 10.0}
        broker = self.broker(free)
        self.assertEqual(broker.tick(), [])
        free["gb"] = 28.0
        self.assertEqual(broker.tick(), [])
        self.assertEqual(self.row(write)["state"], "queued")

    def test_the_gate_reopens_above_floor_plus_five(self):
        write = self.submit("--kind", "write", "--in-place", label="hub-exec-write")
        tests = self.submit("--res", "tests", label="hub-exec-tests")
        free = {"gb": 10.0}
        broker = self.broker(free)
        self.assertEqual(broker.tick(), [])
        free["gb"] = 30.0
        self.assertEqual(sorted(broker.tick()), sorted([write, tests]))
        self.assertEqual(janitor.held_count(self.conn), 0)

    def test_a_read_job_keeps_launching_under_pressure(self):
        read = self.submit(label="hub-exec-read")
        broker = self.broker({"gb": 1.0})
        self.assertEqual(broker.tick(), [read])

    def test_a_held_job_is_queued_not_failed(self):
        write = self.submit("--kind", "write", "--in-place", label="hub-exec-write")
        broker = self.broker({"gb": 1.0})
        broker.tick()
        self.assertEqual(self.row(write)["state"], "queued")
        self.assertIsNone(self.row(write)["finished_at"])

    def test_pressure_runs_the_reapers_on_the_same_tick(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        broker = self.broker({"gb": 1.0})
        broker.tick()
        self.assertFalse(folder.exists())
        self.assertEqual([row["outcome"] for row in slots.read_events(self.home)], ["reclaimed"])
        self.assertEqual(self.row(job_id)["state"], "done")

    def test_no_pressure_waits_for_the_interval(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        folder = self.scratch_folder()
        broker = self.broker({"gb": 100.0})
        broker.tick()
        self.assertFalse(folder.exists())
        again = self.scratch_folder()
        broker.tick()
        self.assertTrue(again.exists())
        self.assertEqual(self.row(job_id)["state"], "done")


class HistoryTests(Base):
    def test_a_reap_is_recorded_with_its_bytes(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        self.scratch_folder()
        janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        rows = slots.read_events(self.home)
        self.assertEqual([row["outcome"] for row in rows], ["reclaimed"])
        self.assertEqual(rows[0]["kind"], "scratch")
        self.assertEqual(rows[0]["label"], LABEL)
        self.assertGreater(rows[0]["bytes"], 0)

    def test_history_shows_reclaimed_bytes_per_hour(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        self.scratch_folder()
        janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        report = slots.history(self.home, since_s=3600.0, now=self.now)
        self.assertEqual(report["totals"]["reclaims"], 1)
        self.assertGreater(report["totals"]["reclaimed_bytes"], 0)
        self.assertAlmostEqual(report["totals"]["reclaimed_per_hour"],
                               report["totals"]["reclaimed_bytes"], places=3)
        text = slots.format_history(report)
        self.assertIn("reclaimed", text)
        self.assertIn("/h", text)

    def test_a_retention_pass_is_recorded_too(self):
        job_id = self.job(finished_at=self.now - 8 * DAY)
        job_root = db.job_dir(self.home, job_id)
        run = job_root / "runs" / "abcd1234"
        run.mkdir(parents=True, exist_ok=True)
        stream = run / f"stream-{LABEL}.ndjson"
        stream.write_text("s" * 5000, encoding="utf-8")
        touch(stream, self.now - 8 * DAY)
        janitor.retention(self.home, [self.row(job_id)], now=self.now, keep_days=7.0)
        rows = slots.read_events(self.home)
        self.assertEqual([row["kind"] for row in rows], ["retention"])
        self.assertGreater(rows[0]["bytes"], 0)

    def test_slots_history_command_prints_the_reclaimed_rate(self):
        job_id = self.job(finished_at=self.now - 7200.0)
        self.scratch_folder()
        janitor.reap_scratch(self.home, [self.row(job_id)], now=self.now, grace_s=3600.0)
        out = io.StringIO()
        code = cli.main(["slots", "--history", "--since", "6h"], env=self.env, out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("reclaimed", out.getvalue())
        self.assertIn("/h", out.getvalue())


class StatusLineTests(Base):
    def test_status_prints_the_disk_line(self):
        out = io.StringIO()
        code = cli.main(["status"], env=self.env, out=out)
        self.assertEqual(code, 1, out.getvalue())  # no daemon in a temp home
        self.assertIn("disk:", out.getvalue())
        self.assertIn("floor 25 GB", out.getvalue())
        self.assertIn("job(s) held", out.getvalue())

    def test_slots_prints_the_disk_line(self):
        out = io.StringIO()
        code = cli.main(["slots"], env=self.env, out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("disk:", out.getvalue())
        self.assertIn("floor 25 GB", out.getvalue())

    def test_the_floor_comes_from_broker_json(self):
        (self.home / "broker.json").write_text(json.dumps({"slots": {"disk_floor_gb": 3}}), encoding="utf-8")
        out = io.StringIO()
        cli.main(["slots"], env=self.env, out=out)
        self.assertIn("floor 3 GB", out.getvalue())


class OnePassTests(Base):
    def test_run_reaps_scratch_and_shrinks_job_folders_in_one_pass(self):
        job_id = self.job(finished_at=self.now - 8 * DAY)
        folder = self.scratch_folder()
        job_root = db.job_dir(self.home, job_id)
        run = job_root / "runs" / "abcd1234"
        run.mkdir(parents=True, exist_ok=True)
        stream = run / f"stream-{LABEL}.ndjson"
        stream.write_text("s" * 5000, encoding="utf-8")
        touch(stream, self.now - 8 * DAY)
        report = janitor.run(self.home, self.conn, now=self.now, settings=janitor.settings())
        self.assertFalse(folder.exists())
        self.assertFalse(stream.exists())
        self.assertTrue((job_root / "prompt.md").is_file())
        self.assertGreater(janitor.reclaimed(report), 0)
        self.assertIn("janitor:", janitor.format_pass(report))


if __name__ == "__main__":
    unittest.main()


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


class PassWorktreeTests(Base):
    """A passed job's auto worktree goes; any other outcome, or a dirty tree, keeps it."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        self.trashed = []

    def settings(self, **over):
        cfg = janitor.settings()
        cfg.update(over)
        return cfg

    def wt_job(self, *, verdict="PASS", state="done", finished_ago=3600.0, prompt="Do the thing.\n"):
        job_id = self.job(state=state, verdict=verdict, finished_at=self.now - finished_ago, kind="write",
                          prompt=prompt)
        path = self.tmp / "worktrees" / "repo" / f"agentq-{job_id}"
        git(self.repo, "worktree", "add", "-q", "-b", f"agentq/{job_id}", str(path), "main")
        (path / ".venv").mkdir()
        (path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        git(path, "add", ".gitignore")
        git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
        self.conn.execute("UPDATE jobs SET workspace=?, orig_workspace=? WHERE id=?",
                          (str(path), str(self.repo), job_id))
        self.conn.commit()
        return job_id, path

    def reap(self, cwds=lambda: [], **over):
        return janitor.reap_pass_worktrees(self.home, db.select_jobs(self.conn), now=self.now,
                                           settings=self.settings(**over), cwds=cwds,
                                           empty=self.trashed.append)

    def test_a_passed_clean_worktree_is_removed_and_its_branch_kept(self):
        job_id, path = self.wt_job()
        report = self.reap()
        self.assertEqual([item["job_id"] for item in report["removed"]], [job_id])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repo, "worktree", "list").stdout)
        self.assertEqual(git(self.repo, "branch", "--list", f"agentq/{job_id}").stdout.strip(), f"agentq/{job_id}")
        self.assertEqual(len(self.trashed), 1)
        self.assertEqual([row["kind"] for row in slots.read_events(self.home)], ["worktree"])

    def test_blocked_partial_dead_and_cancelled_jobs_keep_their_worktree(self):
        paths = [self.wt_job(verdict="BLOCKED")[1], self.wt_job(verdict="PARTIAL")[1],
                 self.wt_job(state="failed", verdict="DEAD")[1], self.wt_job(state="cancelled", verdict=None)[1]]
        report = self.reap()
        self.assertEqual(report["removed"], [])
        self.assertTrue(all(path.exists() for path in paths))

    def test_a_pass_inside_the_grace_is_kept(self):
        _, path = self.wt_job(finished_ago=60.0)
        self.assertEqual(self.reap()["removed"], [])
        self.assertTrue(path.exists())

    def test_uncommitted_work_keeps_the_worktree(self):
        _, path = self.wt_job()
        (path / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        report = self.reap()
        self.assertEqual(report["removed"], [])
        self.assertEqual(report["kept"][0]["reason"], "uncommitted or untracked files")
        self.assertTrue(path.exists())

    def test_a_process_inside_the_worktree_keeps_it(self):
        _, path = self.wt_job()
        report = self.reap(cwds=lambda: [os.path.realpath(path / "sub")])
        self.assertEqual(report["kept"][0]["reason"], "a process is working inside it")
        self.assertTrue(path.exists())

    def test_a_failed_process_scan_keeps_everything(self):
        _, path = self.wt_job()
        report = self.reap(cwds=lambda: None)
        self.assertEqual(report["kept"][0]["reason"], "process scan failed")
        self.assertTrue(path.exists())

    def test_keep_worktree_in_the_front_matter_opts_out(self):
        _, path = self.wt_job(prompt="---\nkeep_worktree: true\n---\nDo the thing.\n")
        self.assertEqual(self.reap()["kept"][0]["reason"], "keep_worktree")
        self.assertTrue(path.exists())

    def test_a_folder_on_another_branch_is_never_removed(self):
        job_id, path = self.wt_job()
        git(path, "checkout", "-q", "-b", "someone-else")
        self.assertIn("not agentq/", self.reap()["kept"][0]["reason"])
        self.assertTrue(path.exists())

    def test_the_pass_limit_caps_removals(self):
        paths = [self.wt_job()[1] for _ in range(3)]
        report = self.reap(pass_worktrees_per_pass=2)
        self.assertEqual(len(report["removed"]), 2)
        self.assertEqual(sum(path.exists() for path in paths), 1)

    def test_a_job_without_an_auto_worktree_is_ignored(self):
        self.job(finished_at=self.now - 3600.0)
        self.assertEqual(self.reap(), {"removed": [], "kept": []})

    @unittest.skipUnless(janitor.wtreap.shutil.which("lsof", path="/usr/sbin:/usr/bin:/sbin"), "no lsof")
    def test_the_process_scan_finds_lsof_outside_a_launchd_path(self):
        with mock.patch.dict(os.environ, {"PATH": "/bin"}):
            self.assertIsNotNone(janitor.wtreap.process_cwds())
