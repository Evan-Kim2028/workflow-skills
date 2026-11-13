"""Postgres janitor: fake ps, lsof, psql and pg_ctl runners, temp data dirs.

Nothing here starts or stops a real server. Each test builds a FakeHost whose
process table, cwd map, client counts and stop results are set by the test,
and checks which postmasters are kept or stopped and why.
"""
import contextlib
import io
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import pgjanitor  # noqa: E402

HOMEBREW_DIR = "/opt/homebrew/var/postgresql@17"


def ps_row(pid, ppid, etime, datadir, user="me", argv0="/opt/homebrew/bin/postgres"):
    return f"{pid} {ppid} {user} {etime} {argv0} -D {datadir} -p 5432"


def backend_row(pid, ppid, user="me"):
    return f"{pid} {ppid} {user} 00:10 postgres: checkpointer"


def _padded(result):
    return result if len(result) == 3 else (result[0], result[1], "")


def pid_file(pid, datadir, port="5432", socket_dir="/tmp/sock"):
    return [str(pid), datadir, "1700000000", port, socket_dir, "127.0.0.1", "123"]


class FakeHost(pgjanitor.Host):
    """Machine access replaced by test data. Records every stop, signal and psql call."""

    def __init__(self, root, ps, cwds=None, psql_results=None, pid_files=None, has=("psql", "pg_ctl"),
                 pg_ctl_rc=0, scratch=None, alive=None):
        self.user = "me"
        self.home = os.path.join(root, "home")
        self._root = root
        self._ps = ps
        self._cwds = cwds
        self.psql_results = [_padded(r) for r in (psql_results or [(0, "0")])]
        self._pid_files = pid_files or {}
        self._has = set(has)
        self.pg_ctl_rc = pg_ctl_rc
        self._scratch = scratch or {}
        self.alive_pids = set(alive or [])
        self.calls = []

    def ps_text(self):
        return self._ps

    def cwd_text(self):
        if self._cwds is None:
            return None
        return "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, cwd in self._cwds.items())

    def has(self, name):
        return name in self._has

    def roots(self):
        return (self._root,)

    def pid_file_lines(self, datadir):
        return self._pid_files.get(datadir)

    def psql(self, args):
        self.calls.append(("psql", args))
        if len(self.psql_results) > 1:
            return self.psql_results.pop(0)
        return self.psql_results[0]

    def pg_ctl_stop(self, datadir):
        self.calls.append(("pg_ctl", datadir))
        return self.pg_ctl_rc

    def scratch_verdict(self, datadir):
        return self._scratch.get(datadir)

    def alive(self, pid):
        return pid in self.alive_pids

    def signal(self, pid, sig):
        self.calls.append(("signal", pid, sig))
        if sig == signal.SIGTERM:
            self.alive_pids.discard(pid)

    def sleep(self, seconds):
        return None

    def stopped(self):
        return [c for c in self.calls if c[0] == "pg_ctl"]


class PgJanitorTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="pgjanitor-"))
        self.scratch_pg = os.path.join(self.root, "scratch", "WS33", "ws33-exec-x-r3", "pgdata")
        os.makedirs(self.scratch_pg)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def judge_one(self, host, min_age_s=3600.0):
        rows = pgjanitor.plan(host, min_age_s)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_homebrew_service_is_never_touched(self):
        host = FakeHost(
            self.root,
            ps=ps_row(100, 1, "8-00:00:00", HOMEBREW_DIR),
            cwds={100: HOMEBREW_DIR},
            pid_files={HOMEBREW_DIR: pid_file(100, HOMEBREW_DIR)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "protected path")
        self.assertEqual(host.stopped(), [])

    def test_young_cluster_is_kept(self):
        host = FakeHost(
            self.root,
            ps=ps_row(101, 1, "00:10:00", self.scratch_pg),
            cwds={101: self.scratch_pg},
            pid_files={self.scratch_pg: pid_file(101, self.scratch_pg)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "younger than --min-age")
        self.assertEqual(host.stopped(), [])

    def test_cluster_with_active_client_is_kept(self):
        host = FakeHost(
            self.root,
            ps=ps_row(102, 1, "2-00:00:00", self.scratch_pg),
            cwds={102: self.scratch_pg},
            psql_results=[(0, "2\n")],
            pid_files={self.scratch_pg: pid_file(102, self.scratch_pg)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "2 client connection(s)")
        self.assertEqual(host.stopped(), [])

    def test_orphaned_scratch_cluster_is_stopped_on_apply_only(self):
        ps = "\n".join(
            [ps_row(103, 1, "2-00:00:00", self.scratch_pg), backend_row(104, 103)]
        )
        pid_files = {self.scratch_pg: pid_file(103, self.scratch_pg)}

        dry = FakeHost(self.root, ps=ps, cwds={103: self.scratch_pg}, pid_files=pid_files, alive=[103])
        out = io.StringIO()
        with mock.patch.object(pgjanitor, "Host", return_value=dry), contextlib.redirect_stdout(out):
            self.assertEqual(pgjanitor.cmd_postgres([]), 0)
        self.assertEqual(dry.stopped(), [])
        self.assertIn("would stop", out.getvalue())
        self.assertIn("ANSWER: 1 of 1 local postgres servers would be stopped", out.getvalue())

        live = FakeHost(self.root, ps=ps, cwds={103: self.scratch_pg}, pid_files=pid_files, alive=[103])
        out = io.StringIO()
        with mock.patch.object(pgjanitor, "Host", return_value=live), contextlib.redirect_stdout(out):
            self.assertEqual(pgjanitor.cmd_postgres(["--apply"]), 0)
        self.assertEqual(live.stopped(), [("pg_ctl", self.scratch_pg)])
        self.assertIn("stopped", out.getvalue())
        self.assertIn("ANSWER: stopped 1 orphaned local postgres server(s) of 1.", out.getvalue())
        self.assertTrue(os.path.isdir(self.scratch_pg), "data dir must never be deleted")

    def test_unknown_client_check_keeps(self):
        host = FakeHost(
            self.root,
            ps=ps_row(105, 1, "2-00:00:00", self.scratch_pg),
            cwds={105: self.scratch_pg},
            psql_results=[(-1, "")],
            pid_files={self.scratch_pg: pid_file(105, self.scratch_pg)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["reason"], "client check did not answer")
        self.assertEqual(host.stopped(), [])

    def test_unreadable_process_cwd_keeps_everything(self):
        host = FakeHost(
            self.root,
            ps=ps_row(106, 1, "2-00:00:00", self.scratch_pg),
            cwds=None,
            pid_files={self.scratch_pg: pid_file(106, self.scratch_pg)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(host.stopped(), [])

    def test_missing_ps_raises_and_stops_nothing(self):
        host = FakeHost(self.root, ps=None)
        with self.assertRaises(RuntimeError):
            pgjanitor.plan(host, 3600.0)
        self.assertEqual(host.stopped(), [])

    def test_relative_data_dir_resolves_through_cwd_or_keeps(self):
        rel_dir = os.path.join(self.scratch_pg)
        host = FakeHost(
            self.root,
            ps=ps_row(107, 1, "2-00:00:00", "pg/data").replace("-D pg/data", "-D pgdata"),
            cwds={107: self.scratch_pg},
            pid_files={rel_dir: pid_file(107, rel_dir)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["action"], "stop", row)

        unknown = FakeHost(
            self.root,
            ps=ps_row(108, 1, "2-00:00:00", "pgdata"),
            cwds={108: os.path.join(self.root, "elsewhere")},
        )
        row = self.judge_one(unknown)
        self.assertEqual(row["reason"], "relative data dir, cwd unknown")

    def test_no_psql_uses_orphan_rule(self):
        pid_files = {self.scratch_pg: pid_file(109, self.scratch_pg)}
        orphan = FakeHost(
            self.root,
            ps="\n".join([
                ps_row(109, 1, "2-00:00:00", self.scratch_pg),
                backend_row(110, 109),
            ]),
            cwds={109: self.scratch_pg, 110: self.scratch_pg},
            pid_files=pid_files,
            has=("pg_ctl",),
        )
        row = self.judge_one(orphan)
        self.assertEqual(row["action"], "stop")
        self.assertEqual([c for c in orphan.calls if c[0] == "psql"], [], "psql must not run without the binary")

        busy = FakeHost(
            self.root,
            ps="\n".join([
                ps_row(111, 1, "2-00:00:00", self.scratch_pg),
                ps_row(112, 1, "1-00:00:00", "/elsewhere", argv0="sleep").replace("-D /elsewhere -p 5432", ""),
            ]),
            cwds={111: self.scratch_pg, 112: self.scratch_pg},
            pid_files=pid_files,
            has=("pg_ctl",),
        )
        row = self.judge_one(busy)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "no client check; a process works in the data dir")

        launcher = FakeHost(
            self.root,
            ps=ps_row(113, 4000, "2-00:00:00", self.scratch_pg),
            cwds={113: self.scratch_pg},
            pid_files=pid_files,
            has=("pg_ctl",),
        )
        row = self.judge_one(launcher)
        self.assertEqual(row["reason"], "no client check; launcher still running")

    def test_refused_role_falls_back_to_orphan_rule(self):
        pid_files = {self.scratch_pg: pid_file(124, self.scratch_pg)}
        refused = FakeHost(
            self.root,
            ps=ps_row(124, 1, "2-00:00:00", self.scratch_pg),
            cwds={124: self.scratch_pg},
            psql_results=[(2, "", 'psql: error: FATAL:  role "me" does not exist')],
            pid_files=pid_files,
        )
        row = self.judge_one(refused)
        self.assertEqual(row["action"], "stop")
        self.assertIn("psql role refused", row["reason"])

        unreachable = FakeHost(
            self.root,
            ps=ps_row(125, 1, "2-00:00:00", self.scratch_pg),
            cwds={125: self.scratch_pg},
            psql_results=[(2, "", "could not connect to server: Connection refused")],
            pid_files={self.scratch_pg: pid_file(125, self.scratch_pg)},
        )
        self.assertEqual(self.judge_one(unreachable)["reason"], "client check did not answer")

    def test_client_backend_in_process_table_keeps_even_when_role_refused(self):
        pid_files = {self.scratch_pg: pid_file(126, self.scratch_pg)}
        busy = FakeHost(
            self.root,
            ps="\n".join([
                ps_row(126, 1, "2-00:00:00", self.scratch_pg),
                backend_row(127, 126),
                "128 126 me 00:02 postgres: me app [local] idle",
            ]),
            cwds={126: self.scratch_pg},
            psql_results=[(2, "", 'FATAL:  role "me" does not exist')],
            pid_files=pid_files,
        )
        row = self.judge_one(busy)
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "1 client backend(s) in process table")
        self.assertEqual(busy.stopped(), [])

    def test_pytest_cluster_needs_psql_and_a_finished_run(self):
        run_dir = os.path.join(self.root, "pytest-of-me", "pytest-7")
        datadir = os.path.join(run_dir, "cluster", "data")
        os.makedirs(datadir)
        pid_files = {datadir: pid_file(114, datadir)}
        no_psql = FakeHost(
            self.root,
            ps=ps_row(114, 1, "2-00:00:00", datadir),
            cwds={114: datadir},
            pid_files=pid_files,
            has=("pg_ctl",),
        )
        self.assertEqual(self.judge_one(no_psql)["reason"], "pytest cluster needs psql to prove the run finished")

        live_run = FakeHost(
            self.root,
            ps="\n".join([
                ps_row(115, 1, "2-00:00:00", datadir),
                f"116 1 me 01:00:00 python -m pytest --basetemp {run_dir}",
            ]),
            cwds={115: datadir},
            pid_files=pid_files,
        )
        self.assertEqual(self.judge_one(live_run)["reason"], "pytest run still live")

        finished = FakeHost(
            self.root,
            ps=ps_row(117, 1, "2-00:00:00", datadir),
            cwds={117: datadir},
            pid_files=pid_files,
        )
        self.assertEqual(self.judge_one(finished)["action"], "stop")

    def test_repo_checkout_is_kept(self):
        repo = os.path.join(self.root, "repo")
        datadir = os.path.join(repo, "pgdata")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(datadir)
        host = FakeHost(
            self.root,
            ps=ps_row(118, 1, "2-00:00:00", datadir),
            cwds={118: datadir},
            pid_files={datadir: pid_file(118, datadir)},
        )
        row = self.judge_one(host)
        self.assertEqual(row["reason"], "inside a repo checkout")
        self.assertEqual(host.stopped(), [])

    def test_other_users_servers_are_not_listed(self):
        host = FakeHost(
            self.root,
            ps=ps_row(119, 1, "2-00:00:00", self.scratch_pg, user="someone-else"),
            cwds={119: self.scratch_pg},
        )
        self.assertEqual(pgjanitor.plan(host, 3600.0), [])

    def test_scratch_run_still_running_keeps(self):
        host = FakeHost(
            self.root,
            ps=ps_row(120, 1, "2-00:00:00", self.scratch_pg),
            cwds={120: self.scratch_pg},
            pid_files={self.scratch_pg: pid_file(120, self.scratch_pg)},
            scratch={self.scratch_pg: "scratch run still running"},
        )
        row = self.judge_one(host)
        self.assertEqual(row["reason"], "scratch run still running")

    def test_recheck_before_apply_keeps_a_server_that_became_busy(self):
        ps = ps_row(121, 1, "2-00:00:00", self.scratch_pg)
        host = FakeHost(
            self.root,
            ps=ps,
            cwds={121: self.scratch_pg},
            psql_results=[(0, "0"), (0, "3")],
            pid_files={self.scratch_pg: pid_file(121, self.scratch_pg)},
            alive=[121],
        )
        rows = pgjanitor.plan(host, 3600.0)
        applied = pgjanitor._apply(host, rows, 3600.0)
        self.assertEqual(applied[0]["action"], "keep")
        self.assertIn("3 client connection(s)", applied[0]["reason"])
        self.assertEqual(host.stopped(), [])

    def test_pg_ctl_failure_falls_back_to_sigterm_only_for_the_named_pid(self):
        pid_files = {self.scratch_pg: pid_file(122, self.scratch_pg)}
        host = FakeHost(self.root, ps="", pid_files=pid_files, pg_ctl_rc=1, alive=[122])
        ok, how = pgjanitor.stop(host, self.scratch_pg, 122)
        self.assertTrue(ok)
        self.assertEqual(how, "SIGTERM")
        signals = [c for c in host.calls if c[0] == "signal"]
        self.assertEqual(signals, [("signal", 122, signal.SIGTERM)])
        self.assertFalse(any(sig == signal.SIGKILL for _, _, sig in signals))

        mismatch = FakeHost(
            self.root, ps="", pid_files={self.scratch_pg: pid_file(999, self.scratch_pg)},
            pg_ctl_rc=1, alive=[122],
        )
        ok, how = pgjanitor.stop(mismatch, self.scratch_pg, 122)
        self.assertFalse(ok)
        self.assertEqual([c for c in mismatch.calls if c[0] == "signal"], [])

    def test_relative_flag_values_and_etime_parse(self):
        self.assertEqual(pgjanitor.parse_etime("02-23:36:38"), 2 * 86400 + 23 * 3600 + 36 * 60 + 38)
        self.assertEqual(pgjanitor.parse_etime("10:07"), 607.0)
        self.assertIsNone(pgjanitor.parse_etime("soon"))
        procs = pgjanitor.parse_ps("  42     1 me 01:02:03 /opt/x/postgres -D data -p 5\n")
        self.assertEqual(procs[0].pid, 42)
        self.assertEqual(pgjanitor.data_dir_arg(procs[0].argv), "data")

    def test_usage_and_bad_duration_are_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(pgjanitor.cmd_postgres(["--min-age", "soon"]), 2)
            self.assertEqual(pgjanitor.cmd_postgres(["--bogus"]), 2)

    def test_no_servers_answers_plainly(self):
        host = FakeHost(self.root, ps=backend_row(1, 0))
        out = io.StringIO()
        with mock.patch.object(pgjanitor, "Host", return_value=host), contextlib.redirect_stdout(out):
            self.assertEqual(pgjanitor.cmd_postgres([]), 0)
        self.assertIn("ANSWER: no local postgres servers owned by me", out.getvalue())


if __name__ == "__main__":
    unittest.main()
