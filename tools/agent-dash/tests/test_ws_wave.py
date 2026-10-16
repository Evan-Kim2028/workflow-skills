"""Parse check and a stubbed run of the ws-wave workflow script."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "workflows" / "ws-wave.js"

DRIVER = r"""
let args
const calls = []
function agent(prompt, opts) {
  calls.push({ label: opts.label, phase: opts.phase, model: opts.model, effort: opts.effort, prompt })
  const label = opts.label
  const row = {
    status: 'done',
    verdict: 'PASS',
    summary: label,
    findings: '',
    commits: 'abc',
    tier: 'used-tier:' + label,
    model: 'used:' + label,
  }
  if (label === 'ws7-A-a-rev1') {
    row.verdict = 'REQUEST_CHANGES'
    row.findings = '1. bug - a.py:1 - wrong - fix it'
  } else if (/-rev\d+$/.test(label)) {
    row.verdict = 'APPROVE'
  }
  if (label === 'ws7-X-x') {
    row.status = 'failed'
    row.verdict = 'BLOCKED'
    row.commits = ''
  }
  return row
}
function log() {}
function phase() {}
async function pipeline(items, first, second) {
  return Promise.all(items.map(async (item) => second(await first(item), item)))
}
async function parallel(items, fn) {
  return Promise.all(items.map((item) => fn(item)))
}
async function __wsWave() {
___WS_WAVE_SOURCE___
}
function waveArgs(lanes, extra) {
  return Object.assign({
    ws: 'WS7',
    repo: '/repo',
    runDir: '/runs',
    template: '/template.md',
    runner: '/bin/run_worker.sh',
    dash: '/bin/agent_dash.py',
    wrapperModel: 'sonnet',
    wrapperEffort: 'high',
    trailer: 'Co-Authored-By: Test <t@example.com>',
    lanes: lanes,
  }, extra || {})
}
async function main() {
  const grokModel = 'grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]'
  args = waveArgs([
    {
      task: 'A', slug: 'a', branch: 'ws7/a', worktree: '/wt/a', brief: 'build A', tests: 'pytest a.py',
      tier: 'grok', model: grokModel, reviewTier: 'composer', reviewModel: 'composer-2.5',
      fixTier: 'glm', fixModel: 'glm-5.3', review: true, reviewFocus: 'the index lists every note',
    },
    {
      task: 'B', slug: 'b', branch: 'ws7/b', worktree: '/wt/b', brief: 'build B',
      deps: ['A'], tier: 'muse', model: 'muse-spark-1.3-contributor', review: true,
    },
    { task: 'X', slug: 'x', branch: 'ws7/x', worktree: '/wt/x', brief: 'build X', review: true },
    {
      task: 'C', slug: 'c', branch: 'ws7/c', worktree: '/wt/c', brief: 'build C',
      deps: ['X'], tier: 'grok', model: grokModel, review: true,
    },
    {
      task: 'S', slug: 's', branch: 'ws7/s', worktree: '/wt/s', brief: 'build S',
      deps: ['A'], base: 'dep:A', tier: 'composer', model: 'composer-2.5', review: false,
    },
    {
      task: 'Q', slug: 'q', branch: 'ws7/q', worktree: '/wt/q', brief: 'build Q',
      review: false, oneWay: true, reviewFocus: 'flag is set', tier: 'glm',
    },
  ])
  const ok = await __wsWave()
  const okCalls = calls.slice()
  async function message(lanes) {
    args = waveArgs(lanes)
    try {
      await __wsWave()
      return ''
    } catch (err) {
      return String(err && err.message ? err.message : err)
    }
  }
  const cycle = await message([
    { task: 'A', slug: 'a', branch: 'ws7/a', worktree: '/wt/a', brief: 'a', deps: ['B'] },
    { task: 'B', slug: 'b', branch: 'ws7/b', worktree: '/wt/b', brief: 'b', deps: ['A'] },
  ])
  const baseCycle = await message([
    { task: 'A', slug: 'a', branch: 'ws7/a', worktree: '/wt/a', brief: 'a', base: 'dep:B' },
    { task: 'B', slug: 'b', branch: 'ws7/b', worktree: '/wt/b', brief: 'b', base: 'dep:A' },
  ])
  const unknown = await message([
    { task: 'C', slug: 'c', branch: 'ws7/c', worktree: '/wt/c', brief: 'c', deps: ['missing'] },
  ])
  const unknownBase = await message([
    { task: 'C', slug: 'c', branch: 'ws7/c', worktree: '/wt/c', brief: 'c', base: 'dep:nope' },
  ])
  const oneWay = await message([
    { task: 'Z', slug: 'z', branch: 'ws7/z', worktree: '/wt/z', brief: 'z', oneWay: true },
  ])
  process.stdout.write(JSON.stringify({ ok, calls: okCalls, cycle, baseCycle, unknown, unknownBase, oneWay }))
}
main().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err))
  process.exit(1)
})
"""


class WsWaveTest(unittest.TestCase):
    def test_script_parses(self):
        text = SCRIPT.read_text(encoding="utf-8").replace("export const meta", "const meta")
        wrapped = "async function __wsWaveCheck() {\n" + text + "\n}\n"
        proc = subprocess.run(["node", "--check", "-"], input=wrapped, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_review_focus_and_one_way_are_in_the_script(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "You MUST check and report each of these, each with evidence (command plus output, or file:line):",
            text,
        )
        self.assertIn("A check without evidence counts as failed.", text)
        self.assertIn("is oneWay and needs reviewFocus", text)
        self.assertIn("lane.review === false && !lane.oneWay", text)
        self.assertIn("checks", text)
        self.assertIn('Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>', text)
        self.assertIn("If the verdict starts with APPROVE, run:", text)

    def test_dependencies_tiers_and_models(self):
        source = SCRIPT.read_text(encoding="utf-8").replace("export const meta", "const meta")
        driver = DRIVER.replace("___WS_WAVE_SOURCE___", source)
        proc = subprocess.run(["node", "-"], input=driver, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        data = json.loads(proc.stdout)
        calls = data["calls"]
        ok = data["ok"]
        by_task = {lane["task"]: lane for lane in ok["lanes"]}

        def one(label):
            found = [row for row in calls if row["label"] == label]
            self.assertEqual(len(found), 1, label)
            return found[0]

        def at(label):
            for i, row in enumerate(calls):
                if row["label"] == label:
                    return i
            self.fail(label)

        early = []
        for row in calls:
            if "-rev" in row["label"] or "-fix" in row["label"]:
                break
            early.append(row["label"])
        self.assertEqual(set(early), {"ws7-A-a", "ws7-X-x", "ws7-Q-q"})
        self.assertLess(at("ws7-A-a"), at("ws7-A-a-rev1"))
        self.assertLess(at("ws7-A-a-rev1"), at("ws7-A-a-fix1"))
        self.assertLess(at("ws7-A-a-fix1"), at("ws7-A-a-rev2"))
        self.assertLess(at("ws7-A-a-rev2"), at("ws7-B-b"))
        self.assertLess(at("ws7-A-a-rev2"), at("ws7-S-s"))
        self.assertFalse(any(row["label"].startswith("ws7-C") for row in calls))

        for row in calls:
            self.assertEqual(row["model"], "sonnet")
            self.assertEqual(row["effort"], "high")

        grok = "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]"
        build_a = one("ws7-A-a")
        self.assertEqual(build_a["phase"], "Build")
        self.assertIn(
            "bash /bin/run_worker.sh grok ws7-A-a /wt/a /runs/ws7-A-a.md /runs 540 '%s'" % grok,
            build_a["prompt"],
        )
        self.assertIn("The diff base is origin/main.", build_a["prompt"])
        self.assertIn("git -C /wt/a log --oneline origin/main..HEAD", build_a["prompt"])
        self.assertIn("python3 /bin/agent_dash.py ws task WS7 A wip", build_a["prompt"])
        self.assertIn("worktree add -b ws7/a /wt/a origin/main", build_a["prompt"])
        self.assertIn("Co-Authored-By: Test <t@example.com>", build_a["prompt"])

        rev_a = one("ws7-A-a-rev1")
        self.assertEqual(rev_a["phase"], "Review")
        self.assertIn(
            "bash /bin/run_worker.sh composer ws7-A-a-rev1 /wt/a /runs/ws7-A-a-rev1.md /runs 540 'composer-2.5'",
            rev_a["prompt"],
        )
        self.assertIn("against origin/main", rev_a["prompt"])
        self.assertIn("python3 /bin/agent_dash.py ws task WS7 A pr", rev_a["prompt"])
        self.assertIn("You MUST check and report each of these", rev_a["prompt"])
        self.assertIn("the index lists every note", rev_a["prompt"])

        fix_a = one("ws7-A-a-fix1")
        self.assertIn(
            "bash /bin/run_worker.sh glm ws7-A-a-fix1 /wt/a /runs/ws7-A-a-fix1.md /runs 540 'glm-5.3'",
            fix_a["prompt"],
        )
        self.assertIn("Co-Authored-By: Test <t@example.com>", fix_a["prompt"])

        self.assertIn(
            "bash /bin/run_worker.sh muse ws7-B-b /wt/b /runs/ws7-B-b.md /runs 540 'muse-spark-1.3-contributor'",
            one("ws7-B-b")["prompt"],
        )
        self.assertIn(
            "bash /bin/run_worker.sh muse ws7-B-b-rev1 /wt/b /runs/ws7-B-b-rev1.md /runs 540 ''",
            one("ws7-B-b-rev1")["prompt"],
        )

        stacked = one("ws7-S-s")["prompt"]
        self.assertIn("The diff base is ws7/a.", stacked)
        self.assertIn("python3 /bin/agent_dash.py ws task WS7 S wip", stacked)
        self.assertIn("git -C /repo worktree add -b ws7/s /wt/s ws7/a", stacked)
        self.assertIn("git -C /wt/s log --oneline ws7/a..HEAD", stacked)
        self.assertIn(
            "bash /bin/run_worker.sh composer ws7-S-s /wt/s /runs/ws7-S-s.md /runs 540 'composer-2.5'",
            stacked,
        )

        qrev = one("ws7-Q-q-rev1")["prompt"]
        self.assertIn("flag is set", qrev)
        self.assertIn("bash /bin/run_worker.sh glm ws7-Q-q-rev1", qrev)

        self.assertEqual(by_task["A"]["final"], "APPROVE")
        self.assertEqual(by_task["A"]["model"], "used:ws7-A-a")
        self.assertEqual(by_task["A"]["runs"][0], {"role": "build", "tier": "used-tier:ws7-A-a", "model": "used:ws7-A-a"})
        roles = [row["role"] for row in by_task["A"]["runs"]]
        self.assertEqual(roles, ["build", "review", "fix", "review"])
        self.assertEqual(by_task["A"]["runs"][1]["model"], "used:ws7-A-a-rev1")
        self.assertEqual(by_task["A"]["runs"][2]["tier"], "used-tier:ws7-A-a-fix1")
        self.assertEqual(by_task["B"]["final"], "APPROVE")
        self.assertEqual(by_task["S"]["final"], "built")
        self.assertEqual(by_task["S"]["runs"], [{"role": "build", "tier": "used-tier:ws7-S-s", "model": "used:ws7-S-s"}])
        self.assertEqual(by_task["Q"]["final"], "APPROVE")
        self.assertEqual(by_task["X"]["final"], "build-failed")
        self.assertEqual(by_task["C"]["final"], "skipped-dep")
        self.assertEqual(by_task["C"]["dependency"], "X")
        self.assertEqual(by_task["C"]["runs"], [])
        self.assertEqual(ok["skipped"], [{"task": "C", "dependency": "X", "branch": "ws7/c"}])
        self.assertEqual([row["task"] for row in ok["needsLead"]], ["X"])
        self.assertEqual([row["task"] for row in ok["built"]], ["S"])
        self.assertEqual([row["task"] for row in ok["approved"]], ["A", "B", "Q"])

        self.assertIn("cycle", data["cycle"])
        self.assertIn("A", data["cycle"])
        self.assertIn("B", data["cycle"])
        self.assertIn("cycle", data["baseCycle"])
        self.assertIn("unknown", data["unknown"])
        self.assertIn("missing", data["unknown"])
        self.assertIn("unknown", data["unknownBase"])
        self.assertIn("nope", data["unknownBase"])
        self.assertIn("oneWay", data["oneWay"])
        self.assertIn("reviewFocus", data["oneWay"])
