"""Rules for the overview pulse card: the KPI strip and its payload.

The pulse card is a KPI strip only (workstreams in progress, agents
running, tokens and cost for the fixed 24 h window, finished agents,
with hourly token bars); the workstream momentum chart is gone.
pulseEnsure fetches the per-workstream hour buckets the tokens cell
folds into 24 hourly bars, and the spec contract pins overview-only
with the section name "pulse".
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def load_pulse_harness(extra=""):
    return (
        r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
global.window = window;
eval(fs.readFileSync(process.argv[2], "utf8"));
const A = window.AdPage;
assert.strictEqual(typeof A.pulseEnsure, "function", "AdPage.pulseEnsure is not exported");
assert.strictEqual(typeof window.pulseEnsure, "function", "pulseEnsure is not a plain global");
assert.strictEqual(typeof A.pulseWsKey, "function", "AdPage.pulseWsKey is not exported");
assert.strictEqual(typeof A.pulseCard, "function", "AdPage.pulseCard is not exported");
assert.deepStrictEqual(Object.keys(A.PULSE).sort(),
  ["WINDOW_H"].sort(),
  "PULSE thresholds drifted");
''' + extra
    )


class PulseRules(unittest.TestCase):
    def _run(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "pulse.js"
            harness_path.write_text(load_pulse_harness(extra), encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "cards" / "pulse.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_failed_fetch_retries_once_per_minute(self):
        """A rejecting api (usage) runs at most once per minute stamp,
        even though drawMain calls the ensure again at once after each
        failure; the next minute retries."""
        self._run(r'''
const counts = {usage: 0};
let nowMs = 1700000000000 - (1700000000000 % 60000) + 5000;
Date.now = () => nowMs;
global.state = {view: "exec", ws: "all", wss: []};
global.secCache = {pulse: 1};
global.api = (path) => {
  counts.usage++;
  return Promise.reject(new Error("500"));
};
let draws = 0;
global.drawMain = () => { draws++; A.pulseEnsure(); };
const flush = () => new Promise((r) => setTimeout(r, 5));
(async () => {
  A.pulseEnsure();
  await flush();
  assert.strictEqual(counts.usage, 1, "usage refetched inside the failed minute");
  assert.ok(draws >= 1, "failure did not redraw the section");
  A.pulseEnsure();
  await flush();
  assert.strictEqual(counts.usage, 1, "same-minute redraw refetched usage");
  nowMs += 60000;
  A.pulseEnsure();
  await flush();
  assert.strictEqual(counts.usage, 2, "next minute did not retry usage");
  console.log("failed fetch retry ok");
})().catch((e) => { console.error(e); process.exit(1); });
''')

    def test_usage_payload_feeds_hourly_bars(self):
        """The tokens KPI folds every workstream's hour cells into 24
        hourly bars through the usage payload (pulseEnsure stamps it on
        state.pulseUsage); without it the cell reads skeleton."""
        self._run(r'''
let nowMs = 1700000000000;
Date.now = () => nowMs;
const H = Math.floor(nowMs / 1000 / 3600) * 3600;
const cells = [
  {t: H - 3600, key: "WS1", input: 400, output: 200, cache_read: 0, cache_write: 0},
  {t: H, key: "WS2", input: 200, output: 100, cache_read: 0, cache_write: 0},
];
global.state = {view: "exec", ws: "all", wss: []};
global.secCache = {};
global.api = (path) => Promise.resolve({buckets: cells});
let draws = 0;
global.drawMain = () => { draws++; };
const flush = () => new Promise((r) => setTimeout(r, 5));
(async () => {
  A.pulseEnsure();
  await flush();
  const p = global.state.pulseUsage;
  assert.ok(p && Array.isArray(p.buckets), "usage payload not stamped on state");
  assert.strictEqual(p.buckets.length, 2, "usage cells lost");
  assert.strictEqual(A.pulseWsKey().indexOf("pulse|"), 0, "pulse section key lost its payload stamp");
  assert.ok(draws >= 1, "payload arrival did not redraw the section");
  console.log("usage payload ok");
})().catch((e) => { console.error(e); process.exit(1); });
''')

    def test_pulse_spec_contract(self):
        """The spec is overview-only and names the pulse section."""
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "pulse_spec.js"
            harness_path.write_text(
                r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
global.window = window;
global.keyOf = (v) => JSON.stringify(v);
global.planSum = {};
global.state = {ws: "all", wss: [], pulseUsage: {since: 0, buckets: []}};
eval(fs.readFileSync(process.argv[2], "utf8"));
const snap = {counts: {}, totals: {}, running: []};
const spec = window.pulseSpec(global.state, snap);
assert.ok(Array.isArray(spec), "overview spec is not a section tuple");
assert.strictEqual(spec[0], "pulse", "section name drifted");
assert.strictEqual(typeof spec[2], "function", "section render is not a function");
assert.strictEqual(window.pulseSpec({ws: "WS1"}, snap), null, "pulse renders on a workstream tab");
console.log("spec contract ok");
''',
                encoding="utf-8",
            )
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "cards" / "pulse.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_pulse_keeps_page_rules(self):
        """The card file obeys the page rules the static suite pins:
        compact numbers only, no hex colours (tokens.css owns the
        palette), no text notifications — and the momentum chart and
        its drill-down are fully gone."""
        import re

        src = (STATIC / "cards" / "pulse.js").read_text()
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for bad in (".toFixed(", "toLocaleString("):
            self.assertNotIn(bad, code, f"pulse.js formats a number with {bad}; use fmtN/fmtUsd")
        self.assertNotRegex(code, r"#[0-9a-fA-F]{3,8}\b", "pulse.js hard-codes a hex colour; use tokens.css")
        for bad in ("toast", "banner", "Notification", "notify"):
            self.assertNotIn(bad, code, f"pulse.js adds a text notification ({bad})")
        for gone in (
            "momentumFor", "pulseHourly", "pulsePhaseColor", "pulsePhaseShares",
            "pulseFoldRuns", "pulseMileTicks", "pulseProgEnsure", "pulseOpenRows",
            "pulseRowOpen", "pulseSetRow", "pulseOpenKey", "pulseProgGet",
            "pulseProgStamps", "pulseFillDetail", "pulseDetailBody", "pulseRow(",
            "pulseProgChart",
            "MOMENT", "PULSE_PHASES", "pulse-mom", "pulse-detail", "pulse-row",
            "pulse-glyph", "pulse-dot", "pulse-chev", "pulse-scroll",
        ):
            self.assertNotIn(gone, code, f"pulse.js still carries removed momentum code ({gone})")
        css = (STATIC / "cards" / "pulse.css").read_text()
        for gone in ("pulse-mom", "pulse-grid", "pulse-row", "pulse-detail", "pulse-glyph", "pulse-dot", "pulse-scroll", "pulse-strip", "pulse-agent-t"):
            self.assertNotIn(gone, css, f"pulse.css still carries removed momentum rules ({gone})")
