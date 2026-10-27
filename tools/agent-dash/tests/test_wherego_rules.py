"""Where-it-goes card rules: folded shares, window math, colour map, wiring.

Client: ``static/cards/wherego.js`` (one stacked bar, three toggles) plus
``static/core/wscolors.js`` (the shared stable workstream colour map).
Synthetic node proofs for the pure helpers and static checks for the
registry/index wiring, following how existing tests evaluate card JS.
"""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSPACES",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def functions(src):
    """Split a script into bodies by two-space 'function name(' headers."""
    heads = [m for m in re.finditer(r"\n  function (\w+)\s*\(", src)]
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
        yield m.group(1), src[m.start():end]


class SharesOfTests(unittest.TestCase):
    """Pure node proofs: folding, unassigned, shares, empty input."""

    HARNESS = r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
eval(fs.readFileSync(process.argv.slice(1)[0], "utf8"));
var sharesOf = window.AdPage.sharesOf;
var hoursOf = window.AdPage.wheregoHours;
function toks(key, v) { return {t: 1700000000, key: key, input: v, output: 0, cache_read: 0, cache_write: 0, cost_usd: 0.01}; }
// Folding: keys under 2% of the 24 h total fold into one Other segment.
var rows = [toks("WS20", 6200), toks("WS21", 3000), toks("WS22", 500), toks("WS23", 50), toks("WS24", 30), toks("unassigned", 120)];
var out = sharesOf(rows, "tokens");
assert.strictEqual(out.length, 5, JSON.stringify(out));
assert.strictEqual(out[0].key, "WS20");
assert.strictEqual(out[0].total, 6200);
var other = out.filter(function (r) { return r.key === "Other"; });
assert.strictEqual(other.length, 1, JSON.stringify(out));
assert.strictEqual(other[0].total, 80, JSON.stringify(out));
// unassigned stays its own segment even under the threshold.
var un = out.filter(function (r) { return r.key === "unassigned"; });
assert.strictEqual(un.length, 1, JSON.stringify(out));
assert.strictEqual(un[0].total, 120);
// Sorted by share, shares sum to 1.
for (var i = 1; i < out.length; i++) assert.ok(out[i - 1].total >= out[i].total, JSON.stringify(out));
var sum = out.reduce(function (a, r) { return a + r.share; }, 0);
assert.ok(Math.abs(sum - 1) < 1e-9, String(sum));
// Cost metric reads cost_usd, not token fields.
var crows = [{t: 1, key: "WS1", input: 9999, output: 9999, cache_read: 0, cache_write: 0, cost_usd: 1.5},
             {t: 1, key: "WS2", input: 1, output: 1, cache_read: 0, cache_write: 0, cost_usd: 0.5}];
var cout = sharesOf(crows, "cost");
assert.strictEqual(cout[0].key, "WS1");
assert.strictEqual(cout[0].total, 1.5);
assert.strictEqual(cout[1].share, 0.25);
// A server-folded Other absorbs the small keys instead of doubling.
var prows = [toks("Other", 100), toks("WS99", 10), toks("WS1", 10000)];
var pout = sharesOf(prows, "tokens");
var poth = pout.filter(function (r) { return r.key === "Other"; });
assert.strictEqual(poth.length, 1, JSON.stringify(pout));
assert.strictEqual(poth[0].total, 110, JSON.stringify(pout));
// Empty and all-zero inputs read empty, never NaN shares.
assert.deepStrictEqual(sharesOf([], "tokens"), []);
assert.deepStrictEqual(sharesOf(null, "cost"), []);
assert.deepStrictEqual(sharesOf([toks("WS1", 0)], "tokens"), []);
// Window math: 24 hourly bars, live anchor on fresh data.
var nowS = 1700085600.0, wall = Math.floor(nowS / 3600) * 3600;
var w = hoursOf(nowS, null);
assert.strictEqual(w.hours.length, 24);
assert.strictEqual(w.t1 - w.t0, 24 * 3600);
assert.strictEqual(w.live, true);
assert.strictEqual(w.hours[23], wall);
// Stale data anchors on the newest data hour, never the wall clock.
var ws = hoursOf(nowS, wall - 30 * 3600);
assert.strictEqual(ws.live, false);
assert.strictEqual(ws.hours[23], Math.floor((wall - 30 * 3600) / 3600) * 3600);
console.log("wherego node proof ok");
'''

    def test_pure_helpers(self):
        proc = subprocess.run(
            ["node", "-e", self.HARNESS, str(STATIC / "cards" / "wherego.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("wherego node proof ok", proc.stdout)


class WsColorsTests(unittest.TestCase):
    """The shared map is stable, muted where it must be, collision-free."""

    HARNESS = r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
eval(fs.readFileSync(process.argv.slice(1)[0], "utf8"));
var wsColor = window.AdPage.wsColor;
assert.strictEqual(typeof wsColor, "function");
// Stable: the same id always reads the same palette slot.
assert.strictEqual(wsColor("WS20", null), wsColor("WS20", null));
assert.ok(/^var\(--s\d+\)$/.test(wsColor("WS20", null)), wsColor("WS20", null));
// Muted keys never take a series colour.
["unassigned", "Other", "other", "", null].forEach(function (k) {
  assert.strictEqual(wsColor(k, null), "var(--other)", String(k));
});
// Done workstreams desaturate toward muted.
var done = wsColor("WS1", {wss: [{ws: "WS1", state: "done"}]});
assert.ok(done.indexOf("color-mix") >= 0 && done.indexOf("var(--muted)") >= 0, done);
// The top eight active workstreams take distinct slots.
var peers = [];
for (var i = 1; i <= 8; i++) peers.push({ws: "WS" + i, state: "active"});
var seen = peers.map(function (w) { return wsColor(w.ws, {wss: peers}); });
assert.strictEqual(new Set(seen).size, 8, JSON.stringify(seen));
console.log("wscolors node proof ok");
'''

    def test_stable_and_muted(self):
        proc = subprocess.run(
            ["node", "-e", self.HARNESS, str(STATIC / "core" / "wscolors.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("wscolors node proof ok", proc.stdout)


class WheregoStaticRulesTests(unittest.TestCase):
    """Registry/index wiring plus the static rules the page pins."""

    def test_registry_entry(self):
        src = (STATIC / "cards" / "registry.js").read_text()
        self.assertIn('{id: "wherego", order: 22, scope: "overview"', src)
        lines = src.splitlines()
        tiles_open = next(i for i, line in enumerate(lines) if '{id: "tiles"' in line)
        tiles_close = next(i for i in range(tiles_open, len(lines)) if lines[i].strip() == "}},")
        wherego = next(i for i, line in enumerate(lines) if '{id: "wherego"' in line)
        self.assertEqual(wherego, tiles_close + 1, "wherego must sit on its own line right after the tiles entry")

    def test_index_order(self):
        page = (STATIC / "index.html").read_text()
        self.assertLess(
            page.index("/static/cards/tiles.css"), page.index("/static/cards/wherego.css")
        )
        self.assertLess(
            page.index("/static/cards/tiles.js"), page.index("/static/cards/wherego.js")
        )
        self.assertLess(
            page.index("/static/core/theme.js"), page.index("/static/core/wscolors.js")
        )

    def test_chart_and_hover_rules(self):
        src = (STATIC / "cards" / "wherego.js").read_text()
        for bad in (".toFixed(", "toLocaleString()"):
            self.assertNotIn(bad, src, f"wherego.js formats a number with {bad}")
        bodies = dict(functions(src))
        self.assertIn("wheregoChart", bodies)
        self.assertIn("axes(", bodies["wheregoChart"], "the stacked chart must use axes()")
        self.assertIn("timeTicks(", bodies["wheregoChart"])
        for needle in ('role: "img"', '"aria-label"', "viewBox", '"data-key"', '"data-v"', "showTip("):
            self.assertIn(needle, src, f"wherego.js lost {needle}")

    def test_toggle_and_storage(self):
        src = (STATIC / "cards" / "wherego.js").read_text()
        for label in ("Tokens by workstream", "Cost by workstream", "Tokens by model"):
            self.assertIn(label, src, f"wherego.js lost the {label} pill")
        self.assertIn("aria-pressed", src)
        bodies = dict(functions(src))
        self.assertIn("wheregoMode", bodies)
        for name, body in (("wheregoMode", bodies["wheregoMode"]),):
            if "localStorage" in body:
                self.assertIn("try", body, f"{name} touches localStorage unguarded")
                self.assertIn("catch", body, f"{name} touches localStorage unguarded")
        self.assertIn(
            "try { window.localStorage.setItem", src, "the mode write touches localStorage unguarded"
        )

    def test_wscolors_file_shape(self):
        src = (STATIC / "core" / "wscolors.js").read_text()
        for needle in ("AdPage.wsColor", "color-mix", "var(--muted)", "var(--other)"):
            self.assertIn(needle, src, f"wscolors.js lost {needle}")


if __name__ == "__main__":
    unittest.main()
