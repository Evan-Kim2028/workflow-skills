"""Aggregate card rules: pure folding, band order, shared axis, wiring.

Node proof for the pure helpers in static/cards/aggregate.js (the same
eval-harness pattern the page-rule tests use for cards/*), plus wiring
checks that the registry, index.html and DESIGN.md carry the one-line
entries this card needs.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
CARDS = STATIC / "cards"


class AggregateRulesTests(unittest.TestCase):
    def _run_node(self, harness):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agg.js"
            path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(path), str(CARDS / "aggregate.js")],
                capture_output=True,
                text=True,
            )
        return proc

    def test_pure_helpers_node(self):
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
global.AdPage = global.AdPage || {};
function stub() {}
["el","s","CW","axes","timeTicks","fmtN","fmtUsd","showTip","hideTip","api","keyOf","drawMain","colorOf","niceMax","go"].forEach(function (k) { global[k] = stub; });
global.state = {ws: "all", view: "exec", wss: []};
global.secCache = {};
eval(fs.readFileSync(process.argv[2], "utf8"));
assert.strictEqual(typeof aggFoldBuckets, "function", "aggFoldBuckets missing");
assert.strictEqual(typeof aggCumulative, "function", "aggCumulative missing");
assert.strictEqual(typeof aggBandOrder, "function", "aggBandOrder missing");
assert.strictEqual(typeof aggXDomain, "function", "aggXDomain missing");
assert.strictEqual(typeof aggUndatedTotal, "function", "aggUndatedTotal missing");
// folding: tokens sum, Other/unassigned fold to one muted band
var folded = aggFoldBuckets([
  {t: 100, key: "WS1", input: 10, output: 5, cache_read: 0, cache_write: 0, cost_usd: 1.5},
  {t: 100, key: "unassigned", input: 4, output: 0, cache_read: 0, cache_write: 0, cost_usd: 0.5},
  {t: 200, key: "WS1", input: 6, output: 0, cache_read: 0, cache_write: 0, cost_usd: null},
  {t: 200, key: "Other", input: 2, output: 0, cache_read: 0, cache_write: 0, cost_usd: 0.2},
], "tokens");
assert.deepStrictEqual(folded.times, [100, 200]);
assert.strictEqual(folded.byWs["WS1"][0], 15);
assert.strictEqual(folded.byWs["Other"][0], 4);
assert.strictEqual(folded.byWs["Other"][1], 2);
var cost = aggFoldBuckets([
  {t: 100, key: "WS1", input: 10, output: 5, cache_read: 0, cache_write: 0, cost_usd: 1.5},
  {t: 100, key: "WS2", input: 1, output: 1, cache_read: 1, cache_write: 1, cost_usd: null},
], "cost");
assert.strictEqual(cost.byWs["WS1"][0], 1.5);
assert.strictEqual(cost.byWs["WS2"][0], 0);
// cumulative folding
var cum = aggCumulative({A: [1, 2, 3], B: [5, 0, 1]}, 3);
assert.deepStrictEqual(cum.cumByWs.A, [1, 3, 6]);
assert.deepStrictEqual(cum.cumByWs.B, [5, 5, 6]);
assert.deepStrictEqual(cum.totals, {A: 6, B: 6});
// band order: largest at bottom, Other first regardless of size
assert.deepStrictEqual(aggBandOrder({WS1: 10, WS2: 30, WS3: 20}), ["WS2", "WS3", "WS1"]);
assert.deepStrictEqual(aggBandOrder({WS1: 100, Other: 1, WS2: 50}), ["Other", "WS1", "WS2"]);
// shared x-domain: earliest candidate to now
var dom = aggXDomain([300, 100, 200], 1000);
assert.strictEqual(dom.t0, 100);
assert.strictEqual(dom.t1, 1000);
var dom2 = aggXDomain([], 1000);
assert.ok(dom2.t0 < 1000 && dom2.t1 === 1000);
// undated baseline total
assert.strictEqual(aggUndatedTotal({WS1: {undated: 3}, WS2: {undated: 2}}), 5);
assert.strictEqual(aggUndatedTotal({}), 0);
console.log("aggregate pure helpers ok");
'''
        proc = self._run_node(harness)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_wscolor_node(self):
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
eval(fs.readFileSync(process.argv[2], "utf8"));
assert.strictEqual(typeof AdPage.wsColor, "function", "wsColor missing");
var a = AdPage.wsColor("WS1", null);
var b = AdPage.wsColor("WS1", null);
assert.strictEqual(a, b, "wsColor not stable");
assert.strictEqual(AdPage.wsColor("unassigned", null), "var(--other)");
assert.strictEqual(AdPage.wsColor("Other", null), "var(--other)");
assert.ok(AdPage.wsColor("WS1", "done").indexOf("color-mix") >= 0, "done not desaturated");
assert.ok(AdPage.wsColor("WS9", null).indexOf("var(--s") === 0, "not a palette var");
console.log("wscolor ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ws.js"
            path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(path), str(STATIC / "core" / "wscolors.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_wiring(self):
        reg = (STATIC / "cards" / "registry.js").read_text(encoding="utf-8")
        self.assertIn('{id: "aggregate", order: 24, scope: "overview"', reg)
        run_idx = reg.index('{id: "running"')
        agg_idx = reg.index('{id: "aggregate"')
        self.assertLess(agg_idx, run_idx, "aggregate must sit before running")
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('/static/cards/aggregate.css', page)
        self.assertIn('/static/cards/aggregate.js', page)
        self.assertIn('/static/core/wscolors.js', page)
        css_pos = page.index('/static/cards/timeline.css')
        agg_css_pos = page.index('/static/cards/aggregate.css')
        self.assertGreater(agg_css_pos, css_pos)
        js_pos = page.index('/static/cards/timeline.js')
        agg_js_pos = page.index('/static/cards/aggregate.js')
        self.assertGreater(agg_js_pos, js_pos)
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("aggregated-progress card", design.lower())


if __name__ == "__main__":
    unittest.main()
