"""install.sh against a temp HOME. Never touches the real ~/.claude."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INSTALL = REPO / "install.sh"
AGENT_DASH = REPO / "tools" / "agent-dash"
SCRIPTS = ["worker-watch.sh", "muse-usage.py", "hub-usage.sh", "memcap.sh"]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.claude = self.home / ".claude"
        self.backups = self.home / ".local" / "share" / "agent-dash" / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home)}
        return subprocess.run(
            ["sh", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=120
        )

    def test_links_template_and_scripts_back_into_the_repo(self):
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        tpl = self.claude / "templates" / "worker-common.md"
        self.assertTrue(tpl.is_symlink())
        self.assertEqual(os.readlink(tpl), str(AGENT_DASH / "templates" / "worker-common.md"))
        for name in SCRIPTS:
            link = self.claude / "scripts" / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(os.readlink(link), str(AGENT_DASH / "scripts" / name))
            self.assertTrue(link.exists(), name)
        self.assertTrue((self.claude / "tools" / "agent-dash").is_symlink())

    def test_second_run_is_a_no_op(self):
        self.assertEqual(self.run_install().returncode, 0)
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("linked ", res.stdout)
        self.assertNotIn("backed up", res.stdout)
        self.assertFalse(self.backups.exists())

    def test_real_file_is_moved_to_backups_not_overwritten(self):
        scripts = self.claude / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "memcap.sh").write_text("old local copy\n", encoding="utf-8")
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((scripts / "memcap.sh").is_symlink())
        moved = list(self.backups.glob("*/scripts/memcap.sh"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "old local copy\n")

    def test_real_template_is_moved_to_backups(self):
        tpl_dir = self.claude / "templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "worker-common.md").write_text("stale\n", encoding="utf-8")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertTrue((tpl_dir / "worker-common.md").is_symlink())
        moved = list(self.backups.glob("*/templates/worker-common.md"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "stale\n")

    def test_wrong_symlink_is_replaced(self):
        scripts = self.claude / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "hub-usage.sh").symlink_to(self.home / "nowhere")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(
            os.readlink(scripts / "hub-usage.sh"), str(AGENT_DASH / "scripts" / "hub-usage.sh")
        )
        self.assertFalse(self.backups.exists())

    def test_dry_run_touches_nothing(self):
        res = self.run_install("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("dry-run: would link", res.stdout)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.backups.exists())

    def test_dry_run_leaves_real_file_in_place(self):
        scripts = self.claude / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "memcap.sh").write_text("keep me\n", encoding="utf-8")
        res = self.run_install("--dry-run")
        self.assertIn("would move", res.stdout)
        self.assertFalse((scripts / "memcap.sh").is_symlink())
        self.assertEqual((scripts / "memcap.sh").read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse(self.backups.exists())

    def test_unknown_argument_exits_2(self):
        res = self.run_install("--bogus")
        self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()


class MemcapRulesTest(unittest.TestCase):
    """memcap.sh loads machine-specific refusals from outside the repo."""

    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memcap.sh"

    def _run(self, rules_body):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.sh"
            if rules_body is not None:
                rules.write_text(rules_body, encoding="utf-8")
            env = dict(os.environ, AGENT_DASH_MEMCAP_RULES=str(rules))
            return subprocess.run(
                ["bash", str(self.SCRIPT), "64", "echo", "ran"],
                capture_output=True, text=True, env=env, timeout=30,
            )

    def test_runs_when_no_rule_file_exists(self):
        out = self._run(None)
        self.assertEqual(out.returncode, 0)
        self.assertIn("ran", out.stdout)

    def test_a_rule_can_refuse_and_sees_the_command(self):
        out = self._run('if [[ "$*" == *echo* ]]; then echo refused >&2; exit 2; fi\n')
        self.assertEqual(out.returncode, 2)
        self.assertIn("refused", out.stderr)
        self.assertNotIn("ran", out.stdout)
