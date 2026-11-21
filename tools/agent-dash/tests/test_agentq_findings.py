"""The filler findings reader: label parsing, the index, the report lookup, the line check and its cache, the
filters, and the CLI.

Offline. The report tree and the agentq home are temp folders, and git is a fake that answers ``rev-parse`` and
``show`` from a dict of commit to file content.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import cli  # noqa: E402
from agentq import findings as FD  # noqa: E402

NOW = 1_800_000_000.0
REPO = "workflow-skills"
SHORT = "workflow-ski"
MODULE = "tools/agent-dash/agentq/broker.py"
DIGEST = "0a1b2c3d"
OLD = "a" * 40
MAIN = "b" * 40


def report_text(commit=OLD, module=MODULE, findings=()):
    body = f"# Audit: {module}\n\n- Repo: {REPO}\n- Module: `{module}` (10 lines)\n- Commit: {commit}\n\n## Findings\n\n"
    return body + "".join(f"- **{s}** `{loc}` — {title}\n" for s, loc, title in findings)


class FakeGit:
    """``rev-parse origin/main`` and ``show <sha>:<path>`` from a dict of (sha, path) to content."""

    def __init__(self, files, main=MAIN):
        self.files = files
        self.main = main
        self.calls = []

    def __call__(self, workspace, *args):
        self.calls.append(args)
        if args[:2] == ("rev-parse", "origin/main"):
            return None if self.main is None else self.main + "\n"
        if args[0] == "show":
            sha, _, path = args[1].partition(":")
            return self.files.get((sha, path))
        return None


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "reports"
        self.ws = self.tmp / "repo"
        self.ws.mkdir()
        self.write_config()

    def write_config(self, repos=None):
        data = {"filler": {
            "report_dir": str(self.root),
            "repos": repos if repos is not None else {REPO: {"workspace": str(self.ws)}},
        }}
        (self.home / "backlog-seed.json").write_text(json.dumps(data), encoding="utf-8")

    def write_report(self, kind, name, text):
        path = self.root / "filler" / REPO / kind / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_index(self, blocks):
        path = self.root / "filler" / "INDEX.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Filler findings\n\n" + "".join(blocks), encoding="utf-8")
        return path

    def block(self, stamp, label, findings):
        body = f"## {stamp} — {label}\n"
        return body + "".join(f"- **{s}** `{loc}` — {t}\n" for s, loc, t in findings) + "\n"


class LabelTests(unittest.TestCase):
    def test_an_audit_label_gives_repo_kind_and_tail(self):
        label = f"hub-exec-backlog-filler-docs-{SHORT}-tools-agent-dash-agentq-broker-py-{DIGEST}"
        self.assertEqual(FD.parse_label(label, {REPO: SHORT}), (REPO, "docs", f"tools-agent-dash-agentq-broker-py-{DIGEST}"))

    def test_a_pr_label_gives_the_number_as_the_tail(self):
        label = f"hub-exec-backlog-filler-pr-{SHORT}-1234"
        self.assertEqual(FD.parse_label(label, {REPO: SHORT}), (REPO, "pr", "1234"))

    def test_a_review_label_is_parsed_like_an_exec_one(self):
        label = f"hub-review-backlog-filler-bugs-{SHORT}-x-{DIGEST}"
        self.assertEqual(FD.parse_label(label, {REPO: SHORT}), (REPO, "bugs", f"x-{DIGEST}"))

    def test_a_label_that_is_not_filler_is_ignored(self):
        for label in ("", "hub-exec-ws45-exec-1", "hub-exec-backlog-filler-nope-x-1", "backlog-filler-docs-x-1"):
            self.assertIsNone(FD.parse_label(label, {REPO: SHORT}), label)

    def test_the_longest_repo_short_name_wins(self):
        shorts = {"a": "workflow", "b": "workflow-ski"}
        label = f"hub-exec-backlog-filler-docs-workflow-ski-x-{DIGEST}"
        self.assertEqual(FD.parse_label(label, shorts)[0], "b")


class IndexTests(Temp):
    def test_blocks_are_newest_first_with_their_findings(self):
        text = (self.block("2026-11-21 10:00Z", "l1", [("high", "a.py:1", "one")])
                + self.block("2026-11-20 10:00Z", "l2", [("low", "b.py:2", "two")]))
        blocks = FD.index_blocks(text)
        self.assertEqual([b[1] for b in blocks], ["l1", "l2"])
        self.assertEqual(blocks[0][2], [("high", "a.py:1", "one")])

    def test_a_key_comment_does_not_break_a_block(self):
        text = "## 2026-11-21 10:00Z — l1\n<!-- key: abc -->\n- **high** `a.py:1` — one\n\n"
        self.assertEqual(FD.index_blocks(text)[0][2], [("high", "a.py:1", "one")])

    def test_a_location_range_uses_its_first_line(self):
        self.assertEqual(FD.split_location("a.py:231-232"), ("a.py", 231))
        self.assertEqual(FD.split_location("a.py:7"), ("a.py", 7))
        self.assertEqual(FD.split_location("a.py"), ("a.py", None))


class ReportPathTests(Temp):
    def test_an_audit_report_is_found_by_the_label_digest(self):
        path = self.write_report("docs", f"tools-agent-dash-agentq-broker-py-{DIGEST}", "x")
        self.assertEqual(FD.report_path(self.root, REPO, "docs", f"tools-agent-dash-agentq-broker-py-{DIGEST}"), path)

    def test_a_pr_report_is_found_by_its_number(self):
        path = self.write_report("pr", "1234", "x")
        self.assertEqual(FD.report_path(self.root, REPO, "pr", "1234"), path)

    def test_a_missing_report_is_none(self):
        self.assertIsNone(FD.report_path(self.root, REPO, "docs", f"x-{DIGEST}"))
        self.assertIsNone(FD.report_path(self.root, REPO, "pr", "9"))

    def test_the_header_gives_the_commit_and_module(self):
        path = self.write_report("docs", "x", report_text())
        self.assertEqual(FD.read_header(path), (OLD, MODULE))

    def test_a_pr_header_names_its_merge_commit(self):
        path = self.write_report("pr", "1", "# PR\n\nMerge commit: `" + OLD + "`\n\n## Findings\n")
        self.assertEqual(FD.read_header(path)[0], OLD)

    def test_a_header_without_a_commit_is_none(self):
        path = self.write_report("pr", "1", "# PR\n\n## Findings\n")
        self.assertEqual(FD.read_header(path), (None, None))


class LineCheckTests(Temp):
    def check(self, files, main=MAIN):
        return FD.LineCheck(self.home, run=FakeGit(files, main=main))

    def test_an_identical_line_is_same(self):
        git = FakeGit({(OLD, MODULE): "a\nb\nc\n", (MAIN, MODULE): "a\nb\nc\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "same")

    def test_a_different_line_is_changed(self):
        git = FakeGit({(OLD, MODULE): "a\nb\nc\n", (MAIN, MODULE): "a\nB\nc\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "changed")

    def test_a_file_missing_at_main_is_gone(self):
        git = FakeGit({(OLD, MODULE): "a\nb\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "gone")

    def test_a_line_past_the_end_is_gone(self):
        git = FakeGit({(OLD, MODULE): "a\n", (MAIN, MODULE): "a\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:9"), "gone")

    def test_a_report_without_a_commit_is_unknown(self):
        git = FakeGit({})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), None, MODULE, f"{MODULE}:1"), "unknown")
        self.assertEqual(git.calls, [])

    def test_a_repo_without_origin_main_is_unknown(self):
        git = FakeGit({}, main=None)
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:1"), "unknown")

    def test_a_bare_file_name_is_resolved_under_the_report_module(self):
        git = FakeGit({(OLD, MODULE): "a\nb\n", (MAIN, MODULE): "a\nb\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, "broker.py:2"), "same")

    def test_a_second_check_of_the_same_commit_reads_nothing(self):
        git = FakeGit({(OLD, MODULE): "a\nb\n", (MAIN, MODULE): "a\nb\n"})
        first = FD.LineCheck(self.home, run=git)
        first.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2")
        first.save()
        reads = len(git.calls)
        second = FD.LineCheck(self.home, run=git)
        self.assertEqual(second.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "same")
        self.assertEqual(len(git.calls), reads)  # neither the line nor origin/main is read again

    def test_a_stale_origin_main_is_resolved_again(self):
        git = FakeGit({(OLD, MODULE): "a\nb\n", (MAIN, MODULE): "a\nb\n"})
        first = FD.LineCheck(self.home, run=git, now=lambda: 1000.0)
        first.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2")
        first.save()
        reads = len(git.calls)
        later = FD.LineCheck(self.home, run=git, now=lambda: 1000.0 + FD.MAIN_SHA_TTL_S + 1)
        later.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2")
        self.assertEqual(len(git.calls), reads + 1)  # only the origin/main resolve

    def test_a_malformed_cache_file_is_an_empty_cache(self):
        (self.home / FD.CACHE_NAME).write_text("{not json", encoding="utf-8")
        git = FakeGit({(OLD, MODULE): "a\n", (MAIN, MODULE): "a\n"})
        self.assertEqual(FD.LineCheck(self.home, run=git).status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:1"), "same")

    def test_an_empty_line_is_cached_as_present_not_missing(self):
        git = FakeGit({(OLD, MODULE): "a\n\nc\n", (MAIN, MODULE): "a\n\nc\n"})
        first = FD.LineCheck(self.home, run=git)
        self.assertEqual(first.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "same")
        first.save()
        second = FD.LineCheck(self.home, run=git)
        self.assertEqual(second.status(REPO, str(self.ws), OLD, MODULE, f"{MODULE}:2"), "same")


class OpenFindingsTests(Temp):
    def setUp(self):
        super().setUp()
        self.write_report("docs", f"broker-py-{DIGEST}", report_text(findings=[("high", f"{MODULE}:2", "still open")]))
        self.write_report("pr", "7", "# PR\n\nMerge commit: `" + OLD + "`\n")
        self.write_index([
            self.block("2026-11-21 12:00Z", f"hub-exec-backlog-filler-docs-{SHORT}-broker-py-{DIGEST}",
                       [("high", f"{MODULE}:2", "still open"), ("medium", f"{MODULE}:3", "a medium one")]),
            self.block("2026-11-21 11:00Z", f"hub-exec-backlog-filler-pr-{SHORT}-7",
                       [("high", "scripts/x.py:9", "from a pr review")]),
            self.block("2026-11-20 11:00Z", f"hub-exec-backlog-filler-docs-{SHORT}-broker-py-{DIGEST}",
                       [("high", f"{MODULE}:2", "still open")]),
        ])
        self.git = FakeGit({(OLD, MODULE): "a\nb\nc\n", (MAIN, MODULE): "a\nb\nc\n"})

    def open(self, **kw):
        kw.setdefault("check", FD.LineCheck(self.home, run=self.git))
        return FD.open_findings(self.home, **kw)

    def test_high_findings_come_newest_first_and_deduped(self):
        result = self.open()
        self.assertEqual([r["title"] for r in result["rows"]], ["still open", "from a pr review"])
        self.assertEqual(result["total"], 2)

    def test_the_severity_filter_keeps_only_that_severity(self):
        self.assertEqual([r["title"] for r in self.open(severity="medium")["rows"]], ["a medium one"])

    def test_the_repo_filter_keeps_only_that_repo(self):
        self.assertEqual(self.open(repo="other")["rows"], [])

    def test_the_path_filter_matches_a_full_path_or_a_bare_name(self):
        self.assertEqual([r["title"] for r in self.open(paths=[MODULE])["rows"]], ["still open"])
        self.assertEqual([r["title"] for r in self.open(paths=["broker.py"])["rows"]], ["still open"])
        self.assertEqual(self.open(paths=["nothing.py"])["rows"], [])

    def test_the_limit_caps_the_rows_but_not_the_total(self):
        result = self.open(limit=1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["total"], 2)

    def test_each_row_carries_its_report_and_line_status(self):
        row = self.open()["rows"][0]
        self.assertTrue(row["report"].endswith(f"docs/broker-py-{DIGEST}.md"))
        self.assertEqual(row["status"], "same")

    def test_a_missing_index_is_an_error_not_a_crash(self):
        (self.root / "filler" / "INDEX.md").unlink()
        self.assertIn("no filler index", self.open()["error"])

    def test_a_missing_report_dir_is_an_error(self):
        (self.home / "backlog-seed.json").write_text("{}", encoding="utf-8")
        self.assertIn("report_dir", self.open()["error"])

    def test_format_rows_is_one_line_per_row(self):
        self.assertEqual(len(FD.format_rows(self.open())), 2)


class CliTests(Temp):
    def setUp(self):
        super().setUp()
        self.write_report("docs", f"broker-py-{DIGEST}", report_text(findings=[("high", f"{MODULE}:2", "still open")]))
        self.write_index([self.block("2026-11-21 12:00Z", f"hub-exec-backlog-filler-docs-{SHORT}-broker-py-{DIGEST}",
                                     [("high", f"{MODULE}:2", "still open")])])

    def run_cli(self, argv):
        out = io.StringIO()
        code = cli.main(argv, env={"AGENTQ_HOME": str(self.home)}, out=out, now=lambda: NOW)
        return code, out.getvalue()

    def test_the_cli_prints_a_row_and_an_answer(self):
        code, text = self.run_cli(["filler", "findings", "--repo", REPO])
        self.assertEqual(code, 0)
        self.assertIn("still open", text)
        self.assertIn("ANSWER: 1 open high finding(s)", text)

    def test_the_cli_json_flag_prints_the_whole_result(self):
        code, text = self.run_cli(["filler", "findings", "--json"])
        self.assertEqual(code, 0)
        # Every command ends with its ANSWER line, ``--json`` included (``agentq ls --json`` does the same).
        body = text.split("\nANSWER: ")[0]
        self.assertEqual(json.loads(body)["total"], 1)

    def test_the_cli_reports_a_missing_index_as_a_failure(self):
        (self.root / "filler" / "INDEX.md").unlink()
        code, text = self.run_cli(["filler", "findings"])
        self.assertEqual(code, 1)
        self.assertIn("no filler index", text)

    def test_the_default_output_stays_under_twenty_lines(self):
        # A worker runs this before touching a module, so the default must stay short: rows plus the ANSWER line.
        self.write_index([self.block(f"2026-11-{day:02d} 12:00Z",
                                     f"hub-exec-backlog-filler-docs-{SHORT}-broker-py-{DIGEST}",
                                     [("high", f"{MODULE}:{day}", f"finding {day}")])
                          for day in range(1, 26)])
        code, text = self.run_cli(["filler", "findings"])
        self.assertEqual(code, 0)
        self.assertLessEqual(len(text.strip().splitlines()), 20)
        self.assertIn("ANSWER: 25 open high finding(s)", text)


if __name__ == "__main__":
    unittest.main()
