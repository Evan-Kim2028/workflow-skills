"""Tests for the pi cg-gate extension (plugins/pi-cg-gate/).

Two layers, both without a model:

* the pure matching rule, run through Node against the same 47-command table
  the lab prototype used (plus rows for the MCP tool and the safety valve);
* the extension handlers, loaded with a fake `pi` object and a fake `cg`
  script, so session_start / tool_call / before_agent_start are exercised
  end to end.

Node 24 runs the erasable TypeScript directly; there is no build step. The
whole module skips when `node` is missing. No live state, no model calls.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "pi-cg-gate"
RULE = PLUGIN / "search-rule.ts"
EXTENSION = PLUGIN / "index.ts"
INSTALLER = ROOT / "scripts" / "install_hooks.sh"

NODE = shutil.which("node")

# The prototype's 47-command table, unchanged.
COMMANDS = [
    # blocked: recursive or multi-file code search before any cg call
    ('grep -rn "merge policy" .', True),
    ("grep -R foo src/", True),
    ("grep --recursive foo .", True),
    ("grep -rn foo src/app.py", False),
    ("grep -n foo src/a.py src/b.py", True),
    ("grep -n foo *.py", True),
    ("grep -rn foo --exclude-dir=node_modules .", True),
    ("grep -rn foo --include='*.py' .", True),
    ("grep -rn foo --include='*.py' --include='*.md' .", True),
    ("FOO=1 grep -rn foo .", True),
    ("time rg foo", True),
    ("rg foo", True),
    ('rg "merge policy" src/', True),
    ("rg -n foo src/", True),
    ("rg foo | head -20", True),
    ("ag foo", True),
    ("git grep -n foo", True),
    ('find . -name \'*.py\' | grep -n "merge"', False),
    ("find . -type f | grep foo", False),
    ("ls scripts | grep test", False),
    ("find . -name '*.py' | xargs grep -n foo", True),
    ("grep -rn foo . | head", True),
    ("cd src && rg foo", True),
    # allowed: non-search, docs-only, single file, pipe content, listings
    ('grep -n "foo" src/app.py', False),
    ("grep -c foo scripts/ci/merge_policy.py", False),
    ("cat app.log | grep -n error", False),
    ("echo hi | grep hi", False),
    ("grep -n foo < config.yaml", False),
    ("grep -n foo docs/design.md", False),
    ("grep -rn foo docs/", False),
    ("grep -rn foo --include='*.md' .", False),
    ("grep -rn foo --include='*.md'", False),
    ("grep -rn foo *.log", False),
    ("grep -rn foo /tmp/x.log", False),
    ("rg -n foo *.md", False),
    ("rg -g '*.md' foo notes/", False),
    ("rg foo docs/", False),
    ("rg --files", False),
    ("git grep -n foo -- docs/", False),
    ("git grep -n foo -- src/app.py", False),
    ("git log --grep=foo", False),
    ("find . -name '*.py'", False),
    ("find . -name '*.md' | grep toc", False),
    ("ls -la src/", False),
    ("git status", False),
    ("python -m pytest tests/test_merge_policy.py -q", False),
    ("sed -n '1,40p' scripts/ci/merge_policy.py", False),
    ("cat package.json", False),
]

# Every exact input quoted in the review, plus the small local fixes. Each row
# failed before the fix; the name in the comment is the review finding.
REGRESSIONS = [
    # finding 5: heredoc bodies are file content, not commands
    ("cat > /tmp/example.py <<'PY'\ngrep -rn foo .\nPY", False),
    ("cat > /tmp/example.py <<PY\ngrep -rn foo .\nPY", False),
    ("cat > /tmp/example.py <<-PY\n\tgrep -rn foo .\n\tPY", False),
    # finding 6: value-taking options, extensionless file, rg types, git grep
    ("grep -C 2 -n foo src/app.py", False),
    ("grep -m 1 -n foo docs/design.md", False),
    ("grep -n foo Makefile", False),
    ("rg -t md foo", False),
    ("git grep -A 2 foo -- src/app.py", False),
    # finding 2: a later cg does not excuse an earlier search
    ('rg foo; cg explore "foo"', True),
    ("cg --help; rg foo", True),
    # finding 3: negations and directory targets are not docs-only
    ("rg -g '!*.md' foo .", True),
    ("find . -type f ! -name '*.md' | xargs grep foo", True),
    ("find . -name '*.md' -prune -o -type f -print | xargs grep foo", True),
    ("rg foo src/v1.2/", True),
    ("find . -name '*.py' | sort | xargs grep foo", True),
    # finding 4: grouping and git global options
    ("(cd src && grep -rn foo .)", True),
    ("{ grep -rn foo .; }", True),
    ("git -C src grep -n foo", True),
    # deliberately not caught: nested shells and command substitution
    ("bash -lc 'rg foo'", False),
    ('echo "$(rg foo)"', False),
]

# runsCodeGraph is the "actually ran a query" test used for result marking.
CODEGRAPH_CALLS = [
    ('cg explore "foo"', True),
    ("codegraph node foo", True),
    ("cg --help", False),
    ("cg --where", False),
    ("cg", False),
    ("rg foo", False),
]

RULE_SCRIPT = r"""
import { gateDecision, runsCodeGraph, searchIntent, blockReason, shellTokens,
         stripHeredocs, mentionsCodeGraph }
  from %(rule)s;
import { readFileSync } from "node:fs";
const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
let failures = 0;
for (const [command, expected] of spec.commands) {
  const decision = gateDecision(command, false);
  if (decision.block !== expected) {
    failures++;
    console.log(`FAIL expected ${expected ? "BLOCK" : "ALLOW"} ${JSON.stringify(command)} -> ${decision.block ? "BLOCK" : "ALLOW"}`);
  }
}
for (const [command, expected] of spec.regressions) {
  const decision = gateDecision(command, false);
  if (decision.block !== expected) {
    failures++;
    console.log(`FAIL regression expected ${expected ? "BLOCK" : "ALLOW"} ${JSON.stringify(command)} -> ${decision.block ? "BLOCK" : "ALLOW"}`);
  }
}
for (const [command, expected] of spec.codeGraphCalls) {
  if (runsCodeGraph(command) !== expected) {
    failures++;
    console.log(`FAIL runsCodeGraph(${JSON.stringify(command)}) expected ${expected}`);
  }
}
for (const command of spec.afterCg) {
  if (gateDecision(command, true).block) {
    failures++;
    console.log(`FAIL after-cg should allow: ${command}`);
  }
}
for (const command of spec.cgCommands) {
  if (!runsCodeGraph(command) || gateDecision(command, false).block) {
    failures++;
    console.log(`FAIL cg command not allowed: ${command}`);
  }
}
const intent = searchIntent('grep -rn "merge policy" .');
if (!intent.search) {
  failures++;
  console.log("FAIL expected a search intent");
} else {
  const reason = blockReason(intent);
  for (const needle of ['cg explore "merge policy"', "cg node", "cg callers", "cg impact"]) {
    if (!reason.includes(needle)) {
      failures++;
      console.log(`FAIL reason missing ${needle}`);
    }
  }
}
const tokens = shellTokens('grep -rn "a | b" .');
if (tokens.join("|") !== "grep|-rn|a | b|.") {
  failures++;
  console.log(`FAIL shellTokens: ${JSON.stringify(tokens)}`);
}
const heredoc = "cat > /tmp/x.py <<'PY'\ngrep -rn foo .\nPY\n";
if (stripHeredocs(heredoc).includes("grep")) {
  failures++;
  console.log("FAIL heredoc body not stripped");
}
if (!mentionsCodeGraph('cg --help') || runsCodeGraph('cg --help')) {
  failures++;
  console.log("FAIL cg --help should be mentioned but not a work call");
}
console.log(`failures=${failures}`);
process.exit(failures === 0 ? 0 : 1);
"""

HANDLER_SCRIPT = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = await import(pathToFileURL(spec.extension).href + "?v=" + Date.now());
const handlers = {};
const pi = {
  on(name, fn) {
    if (spec.failResultHook && name === "tool_result") throw new Error("no tool_result");
    (handlers[name] ||= []).push(fn);
  },
};
mod.default(pi);
const out = [];
for (const step of spec.steps) {
  const kind = step.kind === "tool_call_throw" ? "tool_call" : step.kind;
  const fns = handlers[kind] || [];
  if (fns.length === 0) { out.push({ missing: step.kind }); continue; }
  let result;
  if (step.kind === "tool_call_throw") {
    const ev = {};
    Object.defineProperty(ev, "toolName", { get() { throw new Error("boom"); } });
    result = await fns[0](ev, {});
  } else {
    result = await fns[0](step.event ?? {}, step.ctx ?? {});
  }
  out.push(result === undefined ? null : result);
}
console.log(JSON.stringify(out));
"""


def run_node(script, args, env=None, timeout=120):
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        return subprocess.run(
            [NODE, path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    finally:
        os.unlink(path)


def write_json(directory, name, payload):
    path = Path(directory) / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def fake_cg(directory, name, body):
    path = Path(directory) / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@unittest.skipUnless(NODE, "node is not installed")
class SearchRuleTest(unittest.TestCase):
    def test_command_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = write_json(
                tmp,
                "spec.json",
                {
                    "commands": COMMANDS,
                    "regressions": REGRESSIONS,
                    "codeGraphCalls": CODEGRAPH_CALLS,
                    "afterCg": ['grep -rn "merge policy" .', "rg foo", "git grep -n foo"],
                    "cgCommands": [
                        'cg explore "merge policy"',
                        "PATH=/x:$PATH codegraph node foo",
                        "cd /tmp && cg query foo",
                    ],
                },
            )
            proc = run_node(RULE_SCRIPT % {"rule": json.dumps(str(RULE))}, [spec])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("failures=0", proc.stdout)

    def test_table_has_47_rows(self):
        self.assertEqual(len(COMMANDS), 47)


@unittest.skipUnless(NODE, "node is not installed")
class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.ok = fake_cg(self.dir, "cg-ok", "exit 0")
        self.noindex = fake_cg(self.dir, "cg-noindex", "exit 3")
        self.slow = fake_cg(self.dir, "cg-slow", "sleep 6\nexit 0")

    def tearDown(self):
        self.tmp.cleanup()

    def run_steps(self, steps, cg=None, extra_env=None, fail_result_hook=False):
        spec = write_json(
            self.dir,
            "scenario.json",
            {
                "extension": str(EXTENSION),
                "steps": steps,
                "failResultHook": fail_result_hook,
            },
        )
        env = {**os.environ, "PI_CG_GATE_CG": cg or self.ok}
        env.pop("PI_CG_GATE", None)
        if extra_env:
            env.update(extra_env)
        proc = run_node(HANDLER_SCRIPT, [spec], env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def session(self, cwd=None):
        return {"kind": "session_start", "ctx": {"cwd": cwd or self.dir}}

    def bash(self, command):
        return {"kind": "tool_call", "event": {"toolName": "bash", "input": {"command": command}}}

    def bash_result(self, command, is_error=False):
        return {
            "kind": "tool_result",
            "event": {
                "toolName": "bash",
                "toolCallId": "call-1",
                "input": {"command": command},
                "isError": is_error,
            },
        }

    def mcp_result(self, is_error=False):
        return {
            "kind": "tool_result",
            "event": {
                "toolName": "mcp__codegraph__explore",
                "toolCallId": "call-2",
                "input": {},
                "isError": is_error,
            },
        }

    def prompt(self, base="BASE PROMPT"):
        return {"kind": "before_agent_start", "event": {"systemPrompt": base}}

    def test_active_session_blocks_and_adds_rule(self):
        out = self.run_steps(
            [self.session(), self.bash('grep -rn "merge policy" .'), self.prompt()]
        )
        self.assertIsNone(out[0])
        self.assertTrue(out[1]["block"])
        self.assertIn('cg explore "merge policy"', out[1]["reason"])
        self.assertIn("cg works from a worktree too", out[2]["systemPrompt"])
        self.assertTrue(out[2]["systemPrompt"].startswith("BASE PROMPT\n\n"))

    def test_no_index_is_inactive(self):
        out = self.run_steps(
            [self.session(), self.bash('grep -rn "merge policy" .'), self.prompt()],
            cg=self.noindex,
        )
        self.assertEqual(out, [None, None, None])

    def test_missing_binary_is_inactive(self):
        out = self.run_steps(
            [self.session(), self.bash("rg foo"), self.prompt()],
            cg=os.path.join(self.dir, "does-not-exist"),
        )
        self.assertEqual(out, [None, None, None])

    def test_timeout_is_inactive(self):
        out = self.run_steps(
            [self.session(), self.bash("rg foo"), self.prompt()], cg=self.slow
        )
        self.assertEqual(out, [None, None, None])

    def test_kill_switch(self):
        out = self.run_steps(
            [self.session(), self.bash("rg foo"), self.prompt()],
            extra_env={"PI_CG_GATE": "off"},
        )
        self.assertEqual(out, [None, None, None])

    def test_safety_valve_opens_after_three_blocks(self):
        out = self.run_steps(
            [self.session()] + [self.bash("rg foo") for _ in range(4)]
        )
        self.assertTrue(out[1]["block"])
        self.assertTrue(out[2]["block"])
        self.assertTrue(out[3]["block"])
        self.assertIsNone(out[4])

    def test_mcp_codegraph_tool_counts_as_use(self):
        out = self.run_steps(
            [
                self.session(),
                {"kind": "tool_call", "event": {"toolName": "mcp__codegraph__explore", "input": {}}},
                self.mcp_result(),
                self.bash("rg foo"),
            ]
        )
        self.assertEqual(out, [None, None, None, None])

    def test_failed_mcp_call_does_not_open_gate(self):
        out = self.run_steps(
            [
                self.session(),
                {"kind": "tool_call", "event": {"toolName": "mcp__codegraph__explore", "input": {}}},
                self.mcp_result(is_error=True),
                self.bash("rg foo"),
            ]
        )
        self.assertTrue(out[3]["block"])

    def test_cg_bash_command_counts_as_use(self):
        out = self.run_steps(
            [
                self.session(),
                self.bash('cg explore "merge policy"'),
                self.bash_result('cg explore "merge policy"'),
                self.bash("rg foo"),
            ]
        )
        self.assertEqual(out, [None, None, None, None])

    def test_cg_help_does_not_open_gate(self):
        out = self.run_steps(
            [
                self.session(),
                self.bash("cg --help"),
                self.bash_result("cg --help"),
                self.bash("rg foo"),
            ]
        )
        self.assertTrue(out[3]["block"])

    def test_failed_cg_call_does_not_open_gate(self):
        out = self.run_steps(
            [
                self.session(),
                self.bash('cg explore "merge policy"'),
                self.bash_result('cg explore "merge policy"', is_error=True),
                self.bash("rg foo"),
            ]
        )
        self.assertTrue(out[3]["block"])

    def test_missing_result_hook_falls_back_to_the_call(self):
        out = self.run_steps(
            [self.session(), self.bash('cg explore "merge policy"'), self.bash("rg foo")],
            fail_result_hook=True,
        )
        self.assertEqual(out, [None, None, None])

    def test_probe_ignoring_sigterm_settles_and_leaves_no_child(self):
        pidfile = os.path.join(self.dir, "cg.pid")
        stubborn = fake_cg(
            self.dir,
            "cg-stubborn",
            'trap "" TERM\necho $$ > "$CG_PIDFILE"\nwhile :; do sleep 1; done',
        )
        started = time.monotonic()
        out = self.run_steps(
            [self.session(), self.bash("rg foo"), self.prompt()],
            cg=stubborn,
            extra_env={"CG_PIDFILE": pidfile},
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 7.0, f"probe took {elapsed:.1f}s")
        # The gate is inactive when the probe does not settle in time.
        self.assertEqual(out, [None, None, None])
        self.assertTrue(os.path.exists(pidfile), "probe child never started")
        pid = int(Path(pidfile).read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_non_bash_tool_is_never_blocked(self):
        out = self.run_steps(
            [self.session(), {"kind": "tool_call", "event": {"toolName": "read", "input": {"path": "x"}}}]
        )
        self.assertEqual(out, [None, None])

    def test_exception_fails_open(self):
        out = self.run_steps([self.session(), {"kind": "tool_call_throw"}])
        self.assertEqual(out, [None, None])

    def test_prompt_rule_appended_once_and_identical(self):
        out = self.run_steps([self.session(), self.prompt(), self.prompt()])
        first, second = out[1]["systemPrompt"], out[2]["systemPrompt"]
        self.assertEqual(first, second)
        self.assertEqual(first.count("For code search, call `cg` first"), 1)
        # A second session produces the same appended text.
        other = self.run_steps([self.session(), self.prompt()])
        self.assertEqual(other[1]["systemPrompt"], first)

    def test_prompt_rule_is_byte_identical_across_sessions(self):
        a = self.run_steps([self.session(), self.prompt("A")])
        b = self.run_steps([self.session(), self.prompt("B")])
        rule_a = a[1]["systemPrompt"][len("A\n\n"):]
        rule_b = b[1]["systemPrompt"][len("B\n\n"):]
        self.assertEqual(rule_a, rule_b)
        self.assertNotIn(self.dir, rule_a)


@unittest.skipUnless(NODE, "node is not installed")
class InstallerTest(unittest.TestCase):
    def run_installer(self, home, *args, agent_dir=None):
        env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
        env.pop("PI_CODING_AGENT_DIR", None)
        if agent_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        return subprocess.run(
            ["sh", str(INSTALLER), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_installs_into_agent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            proc = self.run_installer(home, "--pi-cg-gate", agent_dir=agent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dest = agent / "extensions" / "cg-gate"
            self.assertEqual(
                (dest / "index.ts").read_bytes(), (PLUGIN / "index.ts").read_bytes()
            )
            self.assertEqual(
                (dest / "search-rule.ts").read_bytes(),
                (PLUGIN / "search-rule.ts").read_bytes(),
            )

    def test_default_agent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            proc = self.run_installer(home, "--pi-cg-gate")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((home / ".pi/agent/extensions/cg-gate/index.ts").is_file())

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            self.assertEqual(
                self.run_installer(home, "--pi-cg-gate", agent_dir=agent).returncode, 0
            )
            dest = agent / "extensions" / "cg-gate" / "index.ts"
            before = dest.read_bytes()
            second = self.run_installer(home, "--pi-cg-gate", agent_dir=agent)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already installed", second.stdout)
            self.assertEqual(dest.read_bytes(), before)

    def test_changed_copy_is_swapped_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            self.assertEqual(
                self.run_installer(home, "--pi-cg-gate", agent_dir=agent).returncode, 0
            )
            dest = agent / "extensions" / "cg-gate" / "index.ts"
            dest.write_text("stale\n", encoding="utf-8")
            proc = self.run_installer(home, "--pi-cg-gate", agent_dir=agent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(dest.read_bytes(), (PLUGIN / "index.ts").read_bytes())
            leftovers = [
                p.name
                for p in (agent / "extensions" / "cg-gate").iterdir()
                if ".tmp." in p.name
            ]
            self.assertEqual(leftovers, [])

    def test_update_keeps_destination_directory_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            self.assertEqual(
                self.run_installer(home, "--pi-cg-gate", agent_dir=agent).returncode, 0
            )
            dest = agent / "extensions" / "cg-gate"
            inode = dest.stat().st_ino
            (dest / "index.ts").write_text("stale\n", encoding="utf-8")
            proc = self.run_installer(home, "--pi-cg-gate", agent_dir=agent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # The directory is never moved away and back: a reader always finds it.
            self.assertEqual(dest.stat().st_ino, inode)
            self.assertEqual(
                (dest / "index.ts").read_bytes(), (PLUGIN / "index.ts").read_bytes()
            )

    def test_update_removes_files_no_longer_shipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            self.assertEqual(
                self.run_installer(home, "--pi-cg-gate", agent_dir=agent).returncode, 0
            )
            dest = agent / "extensions" / "cg-gate"
            (dest / "stale.ts").write_text("old\n", encoding="utf-8")
            proc = self.run_installer(home, "--pi-cg-gate", agent_dir=agent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((dest / "stale.ts").exists())
            self.assertTrue((dest / "index.ts").is_file())

    def test_uninstall_removes_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "agent"
            self.assertEqual(
                self.run_installer(home, "--pi-cg-gate", agent_dir=agent).returncode, 0
            )
            dest = agent / "extensions" / "cg-gate"
            self.assertTrue(dest.is_dir())
            proc = self.run_installer(home, "--uninstall", agent_dir=agent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
