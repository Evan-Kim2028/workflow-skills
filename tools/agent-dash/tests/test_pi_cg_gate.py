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
    ('find . -name \'*.py\' | grep -n "merge"', True),
    ("find . -type f | grep foo", True),
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

RULE_SCRIPT = r"""
import { gateDecision, runsCodeGraph, searchIntent, blockReason, shellTokens }
  from %(rule)s;
import { readFileSync } from "node:fs";
const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
let failures = 0;
for (const [command, expected] of spec.commands) {
  const decision = gateDecision(command, false);
  if (decision.block !== expected) {
    failures++;
    console.log(`FAIL expected ${expected ? "BLOCK" : "ALLOW"} ${command} -> ${decision.block ? "BLOCK" : "ALLOW"}`);
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
console.log(`failures=${failures}`);
process.exit(failures === 0 ? 0 : 1);
"""

HANDLER_SCRIPT = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = await import(pathToFileURL(spec.extension).href + "?v=" + Date.now());
const handlers = {};
const pi = { on(name, fn) { (handlers[name] ||= []).push(fn); } };
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

    def run_steps(self, steps, cg=None, extra_env=None):
        spec = write_json(
            self.dir,
            "scenario.json",
            {"extension": str(EXTENSION), "steps": steps},
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
                self.bash("rg foo"),
            ]
        )
        self.assertEqual(out, [None, None, None])

    def test_cg_bash_command_counts_as_use(self):
        out = self.run_steps(
            [self.session(), self.bash('cg explore "merge policy"'), self.bash("rg foo")]
        )
        self.assertEqual(out, [None, None, None])

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
                for p in (agent / "extensions").iterdir()
                if p.name.startswith(".cg-gate.")
            ]
            self.assertEqual(leftovers, [])

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
