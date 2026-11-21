"""pg_scratch: the quiet, size-capped settings a scratch Postgres is started with.

Fake ``initdb``/``pg_ctl``/``psql`` binaries record their argv, so no real
Postgres runs here.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "pg_scratch.py"
sys.path.insert(0, str(SCRIPT.parent))

import pg_scratch as pg  # noqa: E402

FAKE = """#!/bin/sh
printf '%s\\n' "$@" >> "{record}"
{extra}
exit 0
"""


class FakeBin:
    """A temp bin dir whose initdb/pg_ctl/psql record their argv."""

    def __init__(self, base: Path, *, initdb_creates: bool = True):
        self.dir = base / "bin"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.record = base / "argv.txt"
        self.record.write_text("", encoding="utf-8")
        extra = ""
        if initdb_creates:
            # initdb -D <dir> ...: create the data dir with a PG_VERSION.
            extra = 'for a in "$@"; do case "$prev" in -D) mkdir -p "$a"; : > "$a/PG_VERSION";; esac; prev="$a"; done'
        for name in ("initdb", "pg_ctl", "psql"):
            path = self.dir / name
            path.write_text(FAKE.format(record=self.record, extra=extra), encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def calls(self) -> list:
        return [line for line in self.record.read_text(encoding="utf-8").splitlines() if line]


class SettingsTests(unittest.TestCase):
    def test_quiet_settings_are_all_present(self):
        settings = dict(item.split("=", 1) for item in pg.QUIET_SETTINGS)
        self.assertEqual(settings["log_min_messages"], "warning")
        self.assertEqual(settings["log_statement"], "none")
        self.assertEqual(settings["log_min_duration_statement"], "-1")
        self.assertEqual(settings["log_min_error_statement"], "panic")
        self.assertEqual(settings["log_checkpoints"], "off")
        self.assertEqual(settings["log_connections"], "off")
        self.assertEqual(settings["log_disconnections"], "off")
        self.assertEqual(settings["logging_collector"], "on")
        self.assertEqual(settings["log_rotation_size"], "50MB")
        self.assertEqual(settings["log_truncate_on_rotation"], "on")

    def test_server_options_carry_every_setting(self):
        options = pg.server_options(55931, Path("/tmp/sock"))
        self.assertEqual(options[:6], ["-p", "55931", "-k", "/tmp/sock", "-c", "listen_addresses=127.0.0.1"])
        for setting in pg.QUIET_SETTINGS + pg.SPEED_SETTINGS:
            self.assertIn(setting, options)
        self.assertEqual(options.count("-c"), 1 + len(pg.QUIET_SETTINGS) + len(pg.SPEED_SETTINGS))

    def test_default_dir_follows_agent_scratch(self):
        old = os.environ.get("AGENT_SCRATCH")
        os.environ["AGENT_SCRATCH"] = "/tmp/scratch-x"
        try:
            self.assertEqual(pg.default_dir(), Path("/tmp/scratch-x/pg17"))
        finally:
            if old is None:
                os.environ.pop("AGENT_SCRATCH", None)
            else:
                os.environ["AGENT_SCRATCH"] = old

    def test_default_dir_without_agent_scratch(self):
        old = os.environ.pop("AGENT_SCRATCH", None)
        try:
            self.assertEqual(pg.default_dir(), Path.home() / ".cache" / "agent-dash" / "scratch" / "pg17")
        finally:
            if old is not None:
                os.environ["AGENT_SCRATCH"] = old

    def test_cluster_env_sets_a_locale(self):
        old = {key: os.environ.pop(key, None) for key in ("LC_ALL", "LANG")}
        try:
            env = pg._cluster_env()
            self.assertEqual(env["LC_ALL"], "C")
            self.assertEqual(env["LANG"], "C")
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value


class StartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fake = FakeBin(self.base)
        self.datadir = self.base / "pg17"

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_passes_the_quiet_settings_to_pg_ctl(self):
        code = pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir), "--port", "55931"])
        self.assertEqual(code, 0)
        argv = self.fake.calls()
        self.assertIn("start", argv)
        self.assertIn("-D", argv)
        self.assertIn(str(self.datadir), argv)
        self.assertIn("-l", argv)
        self.assertIn(str(self.datadir / "startup.log"), argv)
        options = argv[argv.index("-o") + 1]
        for setting in pg.QUIET_SETTINGS:
            self.assertIn(setting, options)
        self.assertIn("55931", options)
        self.assertTrue((self.datadir / "PG_VERSION").is_file())

    def test_start_prints_the_connection_details(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir), "--port", "55932"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("PGPORT=55932", out)
        self.assertIn(f"PGHOST={self.datadir}", out)
        self.assertIn("postgresql://postgres@127.0.0.1:55932/postgres", out)
        self.assertIn("pg17.log", out)

    def test_start_reuses_an_initialised_data_dir(self):
        self.datadir.mkdir(parents=True)
        (self.datadir / "PG_VERSION").write_text("17\n", encoding="utf-8")
        code = pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir), "--port", "55933"])
        self.assertEqual(code, 0)
        self.assertNotIn("-U", self.fake.calls())

    def test_no_init_skips_initdb(self):
        code = pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir),
                        "--port", "55934", "--no-init"])
        self.assertEqual(code, 0)
        self.assertNotIn("-U", self.fake.calls())

    def test_stop_calls_pg_ctl_stop(self):
        code = pg.main(["stop", "--dir", str(self.datadir), "--bin", str(self.fake.dir)])
        self.assertEqual(code, 0)
        argv = self.fake.calls()
        self.assertIn("stop", argv)
        self.assertIn("-m", argv)
        self.assertIn("fast", argv)

    def test_a_failing_pg_ctl_start_is_reported(self):
        (self.fake.dir / "pg_ctl").write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n", encoding="utf-8")
        (self.fake.dir / "pg_ctl").chmod(0o755)
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            code = pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir), "--port", "55935"])
        self.assertEqual(code, 1)
        self.assertIn("boom", err.getvalue())

    def test_usage_errors(self):
        import io
        from contextlib import redirect_stderr

        for argv in ([], ["bogus"], ["start", "--nope"], ["start", "--dir"]):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(pg.main(argv), 2)

    def test_a_bad_port_is_a_usage_error(self):
        import io
        from contextlib import redirect_stderr

        with redirect_stderr(io.StringIO()):
            self.assertEqual(pg.main(["start", "--dir", str(self.datadir), "--bin", str(self.fake.dir),
                                      "--port", "soon"]), 2)


if __name__ == "__main__":
    unittest.main()
