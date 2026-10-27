"""Rules for the overview pulse card: the momentum classifier and its payload.

The classifier (momentumFor) and the bucket folder (pulseHourly) are pure
functions on window.AdPage, node-driven with no DOM, one fixture per
class: stuck, slowing, moving, idle, fresh start, plus the ratio
boundary and the current-hour fraction floor. A second node pass pins
the spec contract: overview-only, section name "pulse".
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
assert.strictEqual(typeof A.momentumFor, "function", "AdPage.momentumFor is not exported");
assert.strictEqual(typeof window.momentumFor, "function", "momentumFor is not a plain global");
assert.strictEqual(typeof A.pulseHourly, "function", "AdPage.pulseHourly is not exported");
assert.deepStrictEqual(Object.keys(A.PULSE).sort(),
  ["BASE_H", "CRIT_IDLE_S", "CURRENT_MIN_FRAC", "MAX_DOTS", "RECENT_H",
   "SLOW_RATIO", "STUCK_IDLE_S", "WARN_IDLE_S", "WINDOW_H"].sort(),
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

    def test_momentum_classes_in_node(self):
        """One fixture per class: stuck (quiet, stalled, silent),
        slowing, moving, idle, and a fresh start."""
        self._run(r'''
/* Fixed clock at half past the hour: the current-hour fraction is 0.5. */
const H = 1700000000 - (1700000000 % 3600);
const now = H + 1800;
function zeros() { return new Array(24).fill(0); }
function steady(v) {
  const a = zeros();
  for (let i = 16; i < 24; i++) a[i] = v;
  return a;
}
function baseOnly(v) {
  const a = zeros();
  for (let i = 16; i < 22; i++) a[i] = v;
  return a;
}
/* stuck: live agents, fresh activity, but nothing in the last 2 h
   against a nonzero base. */
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 60}, baseOnly(100), [], now), "stuck");
/* stuck: a stalled running row pins it even with fresh tokens. */
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 60}, steady(100), [{stalled: true}], now), "stuck");
/* stuck: silent past the idle threshold with live agents. */
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 1500}, baseOnly(100), [], now), "stuck");
/* slowing: recent under half the base rate (ratio 0.125). */
const slow = zeros();
for (let i = 16; i < 22; i++) slow[i] = 100;
slow[22] = 25;
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 60}, slow, [], now), "slowing");
/* moving: recent at the base rate. */
assert.strictEqual(A.momentumFor({live_agents: 2, last_activity: now - 60}, steady(100), [], now), "moving");
/* moving: the ratio boundary (0.5) still counts as moving. */
const edge = zeros();
for (let i = 16; i < 22; i++) edge[i] = 100;
edge[22] = 100;
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 60}, edge, [], now), "moving");
/* moving: a fresh start (no base, recent tokens). */
const fresh = zeros();
fresh[23] = 40;
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 30}, fresh, [], now), "moving");
/* idle: nothing live, nothing in the window. */
assert.strictEqual(A.momentumFor({live_agents: 0, last_activity: 0}, zeros(), [], now), "idle");
/* idle: old tokens outside the window still read idle with no agents. */
const old = zeros();
old[0] = 500;
assert.strictEqual(A.momentumFor({live_agents: 0, last_activity: now - 20 * 3600}, old, [], now), "idle");
/* idle is the fallthrough: live agents with zero tokens anywhere. */
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: now - 60}, zeros(), [], now), "idle");
console.log("momentum classes ok");
''')

    def test_current_hour_fraction_floor(self):
        """At the top of the hour the partial bucket still counts a
        quarter, so a fresh agent's first tokens read moving."""
        self._run(r'''
const H = 1700000000 - (1700000000 % 3600);
const fresh = new Array(24).fill(0);
fresh[23] = 100;
assert.strictEqual(A.momentumFor({live_agents: 1, last_activity: H - 10}, fresh, [], H), "moving");
console.log("fraction floor ok");
''')

    def test_pulse_hourly_folder(self):
        """Hour cells land in the right buckets, missing hours read 0,
        other workstreams are ignored."""
        self._run(r'''
const H = 1700000000 - (1700000000 % 3600);
function cell(t, key, n) {
  return {t: t, key: key, input: n, output: 0, cache_read: 0, cache_write: 0};
}
const cells = [
  cell(H - 23 * 3600, "WS1", 10),
  cell(H, "WS1", 20),
  cell(H - 5 * 3600, "WS1", 5),
  cell(H, "WS2", 999),
];
const got = A.pulseHourly(cells, "WS1", H);
assert.strictEqual(got.length, 24, "always 24 buckets");
assert.strictEqual(got[0], 10, "oldest hour lands first");
assert.strictEqual(got[23], 20, "current hour lands last");
assert.strictEqual(got[18], 5, "middle hour lands at 24-6");
assert.strictEqual(got[1], 0, "missing hours read 0");
assert.deepStrictEqual(A.pulseHourly(cells, "WS9", H), new Array(24).fill(0), "unknown workstream reads flat");
assert.deepStrictEqual(A.pulseHourly(null, "WS1", H), new Array(24).fill(0), "no payload reads flat");
console.log("hourly folder ok");
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
        palette), no text notifications, and clickable rows are real
        buttons."""
        import re

        src = (STATIC / "cards" / "pulse.js").read_text()
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for bad in (".toFixed(", "toLocaleString("):
            self.assertNotIn(bad, code, f"pulse.js formats a number with {bad}; use fmtN/fmtUsd")
        self.assertNotRegex(code, r"#[0-9a-fA-F]{3,8}\b", "pulse.js hard-codes a hex colour; use tokens.css")
        for bad in ("toast", "banner", "Notification", "notify"):
            self.assertNotIn(bad, code, f"pulse.js adds a text notification ({bad})")
        self.assertIn('type: "button"', code, "momentum rows are not real <button> elements")
        self.assertIn("DashNav.openExec", code, "momentum rows never open the workstream tab")
