"""Static rules for the dashboard pages: every chart has axes, every number is compact."""
import os
import subprocess

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import re
import tempfile
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
FORMATTERS = ("function fmtN", "function fmtUsd", "function fmtDur", "function fmtDate")


def pages():
    """Yield (path, script text) for every page script under static/."""
    for p in sorted(STATIC.glob("*.html")) + sorted(STATIC.glob("*.js")):
        text = p.read_text()
        if p.suffix == ".html":
            text = "\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))
        if text.strip():
            yield p, text


def functions(src):
    """Split a script into top-level-ish function bodies by 'function name(' headers."""
    heads = [m for m in re.finditer(r"\n  function (\w+)\s*\(", src)]
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
        yield m.group(1), src[m.start():end]


def _core_scripts():
    """static/core/*.js in the order index.html loads them (execution order)."""
    page = (STATIC / "index.html").read_text()
    names = re.findall(r'<script src="(/static/core/[^"]+\.js)"></script>', page)
    return [(STATIC / src[len("/static/"):]).read_text() for src in names]


def _card_scripts():
    """static/cards/*.js in the order index.html loads them (execution order)."""
    page = (STATIC / "index.html").read_text()
    names = re.findall(r'<script src="(/static/cards/[^"]+\.js)"></script>', page)
    return [(STATIC / src[len("/static/"):]).read_text() for src in names]


def inline_scripts():
    """One logical script: static/core/*.js then static/cards/*.js in
    script-tag order, then the executable inline script of static/index.html."""
    text = (STATIC / "index.html").read_text()
    inline = "\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))
    return "\n".join(_core_scripts() + _card_scripts() + [inline])


def _extract_taskof(path, name, func="taskOf"):
    """The page's taskOf source with its name swapped, so node can run it
    against the same samples as dash/taskmatch.py. taskOf lives in a card
    file now, so fall back to the card scripts in script-tag order."""
    text = Path(path).read_text()
    if f"function {func}(" not in text:
        for cand in _card_scripts():
            if f"function {func}(" in cand:
                text = cand
                break
    start = text.index(f"function {func}(")
    brace = text.index("{", start)
    depth = 0
    for pos in range(brace, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[start:pos + 1].replace(f"function {func}(", f"function {name}(", 1)
    raise AssertionError(f"{func} body never closes in {path}")


class PageRules(unittest.TestCase):
    def test_shell_has_no_favicon_404(self):
        """The shell inlines an empty icon so no view logs a favicon
        404 (one console error and one failed request per load)."""
        page = (STATIC / "index.html").read_text()
        self.assertIn('rel="icon"', page, "index.html lost its icon link")

    def test_every_chart_has_axes(self):
        for path, src in pages():
            for name, body in functions(src):
                if 'role: "img"' in body and "aria-label" in body and "data-diagram" not in body and name != "axes":
                    is_chart = "viewBox" in body and ("rect" in body or "path" in body)
                    if is_chart:
                        self.assertIn("axes(", body, f"{path.name}:{name} draws a chart without axes()")

    def test_numbers_are_compact(self):
        for path, src in pages():
            for name, body in functions(src):
                if any(body.lstrip("\n ").startswith(f) for f in FORMATTERS):
                    continue
                for bad in (".toFixed(", "toLocaleString()"):
                    self.assertNotIn(bad, body, f"{path.name}:{name} formats a number with {bad}; use fmtN/fmtUsd/fmtDur")

    def _hover_value(self, src, start):
        """One hover-text value expression starting at offset start: stops
        at the first top-level comma, closing bracket or semicolon, so
        neighbouring data-v/data-t attributes (exact, for tests) never
        count as hover text. Strings and nested brackets are honoured."""
        depth = 0
        j = start
        quote = None
        escaped = False
        while j < len(src):
            ch = src[j]
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
            elif ch in ("'", '"', "`"):
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            elif ch == ";" and depth == 0:
                break
            j += 1
        return src[start:j]

    def test_hover_text_has_no_raw_numbers(self):
        """Hover text is compact-only: no title/tooltip/aria-label value
        formats a raw number (String(, Math.round(, toLocaleString,
        toFixed) outside format.js. The exact value stays in data-v/data-t
        attributes and the details drawer. Word-valued titles (a verdict, a
        status, an id, slider epochs announced via valuetext) carry a
        page-rule-allow comment naming why they are not numbers."""
        roots = [STATIC / "cards", STATIC / "core"]
        paths = sorted(p for root in roots for p in root.glob("*.js"))
        paths += [STATIC / name for name in ("models.js", "tabs.js", "sidenav.js", "plan.js", "tokencache.js")]
        inline = "\n".join(re.findall(r"<script>(.*?)</script>", (STATIC / "index.html").read_text(), re.S))
        pats = [
            re.compile(r"(?<![\w$])title\s*:"),
            re.compile(r"\.title\s*="),
            re.compile(r'setAttribute\(\s*"title"\s*,'),
            re.compile(r'"aria-label"\s*:'),
            re.compile(r"data-tip\s*="),
            re.compile(r"<title[\s>]"),
        ]
        bad = []
        for path in paths:
            src = path.read_text()
            lines = src.splitlines()
            for lineno, line in enumerate(lines, 1):
                if "page-rule-allow" in line:
                    continue
                for pat in pats:
                    m = pat.search(line)
                    if not m:
                        continue
                    off = sum(len(item) + 1 for item in lines[: lineno - 1]) + m.end()
                    val = self._hover_value(src, off)
                    for raw in ("String(", "Math.round(", "toLocaleString", ".toFixed("):
                        if raw in val:
                            bad.append(f"{path.name}:{lineno} formats a raw number into hover text: {line.strip()[:140]}")
                            break
                if 'showTip(' in line and "page-rule-allow" not in line:
                    tail = line[line.index('showTip('):]
                    for raw in ("String(", "Math.round(", "toLocaleString", ".toFixed("):
                        if raw in tail:
                            bad.append(f"{path.name}:{lineno} formats a raw number into a tooltip: {line.strip()[:140]}")
                            break
        self.assertEqual(bad, [], "raw numbers in hover text:\n" + "\n".join(bad[:20]))
        inline_bad = [m for m in ("String(", "toLocaleString", ".toFixed(") if m in inline]
        self.assertEqual(inline_bad, [], f"index.html inline script formats raw hover numbers: {inline_bad}")

    def test_hover_builders_stay_exported(self):
        """Every card and core hover path routes through a named compact
        builder, so a later edit cannot drift back to raw concatenation
        without deleting the builder this pins."""
        expected = {
            "format.js": ["function fmtPct(", "function fmtBytes("],
            "cards/activity.js": ["function actLegendTip(", "function actTimelineAria(", "function actConcAria("],
            "cards/burnup.js": ["function burnupLegendTip(", "function burnupTasksAria(", "function burnupDoneRows(", "function burnupWsRow("],
            "cards/depgraph.js": ["function depAriaTip(", "function depTaskLine(", "function depTipRows("],
            "cards/phases.js": ["function phaseSegTip(", "function phaseFmt(", "function phaseStackTip("],
            "cards/progress.js": ["function progLegendTip(", "function progMileRows(", "function progStepRows(", "function progBarRows(", "function progTakeaway("],
            "cards/plan-progress.js": ["function planAttText(", "function planSecTip(", "function planColbarTip(", "function planBigAria(", "function planBigTip("],
            "cards/tables.js": ["function tipRows("],
            "cards/tiles.js": ["function tileTokensTitle(", "function tileTokensApproxTitle(", "function tileCostTitle(", "function tileCostApproxTitle(", "function tileFinTitle(", "function tileFinApproxTitle(", "function tileLiveTitle(", "function tileLiveApproxTitle("],
            "cards/ws-cards.js": ["function wsProgAria(", "function wsProgTitle(", "function wsPlanTitle("],
            "cards/project-home.js": ["function phProgAria("],
            "cards/planned.js": ["function plProgAria("],
            "core/manage.js": ["function mgProgAria("],
            "core/switcher.js": ["function swCountAria("],
        }
        missing = []
        for name, markers in expected.items():
            text = (STATIC / name).read_text()
            for marker in markers:
                if marker not in text:
                    missing.append(f"{name} lost {marker}")
        self.assertEqual(missing, [], "tooltip builders went missing:\n" + "\n".join(missing))

    def test_hover_builders_are_compact(self):
        """Every tooltip text builder renders big numbers compact: 123456789
        tokens, 1234567.891 USD and 98765 seconds leave no 5+ digit run and
        no comma-separated thousands in any hover string."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
var BYNAME = {};
process.argv.slice(1).forEach(function (p) { BYNAME[p.split("/").pop()] = p; });
function load(n) { eval(fs.readFileSync(BYNAME[n], "utf8")); }
var window = {};
function stubEl(tag, attrs, kids) {
  var n = {tag: tag, attrs: attrs || {}, kids: [], style: {}};
  n.appendChild = function (c) { if (c) n.kids.push(c); return c; };
  n.setAttribute = function (k, v) { n.attrs[k] = String(v); };
  (kids || []).forEach(function (c) { if (c) n.kids.push(c); });
  return n;
}
var el = stubEl;
var document = {createTextNode: function (t) { return {text: String(t)}; }, addEventListener: function () {}, getElementById: function () { return null; }, body: {}};
function tierKey(r) { return "muse:claude-opus"; }
function fullName(k) { return "Muse"; }
function tokTotal(t) { t = t || {}; return (t.input || 0) + (t.output || 0) + (t.cache_read || 0) + (t.cache_write || 0); }
load("format.js");
var AD = window.AD;
var fmtN = AD.fmtN, fmtUsd = AD.fmtUsd, fmtDur = AD.fmtDur, fmtPct = AD.fmtPct, fmtBytes = AD.fmtBytes;
["activity.js", "burnup.js", "depgraph.js", "phases.js", "plan-progress.js", "tables.js", "tiles.js", "ws-cards.js", "project-home.js", "planned.js", "manage.js", "switcher.js", "progress.js"].forEach(load);
var W = window;
var seen = [];
function hover(s) { seen.push(String(s)); return s; }
assert.strictEqual(fmtN(123456789), "123.5M");
assert.strictEqual(fmtN(null), "-");
assert.strictEqual(fmtUsd(1234567.891), "$1.2M");
assert.strictEqual(fmtDur(98765), "27h 26m");
assert.strictEqual(fmtPct(0.6233), "62%");
assert.strictEqual(fmtPct(0.043), "4.3%");
assert.strictEqual(fmtBytes(950), "950 B");
assert.strictEqual(fmtBytes(1234567), "1.2 MB");
assert.strictEqual(fmtBytes(0), "0 B");
assert.strictEqual(fmtBytes(null), "–");
assert.strictEqual(fmtBytes(undefined), "–");
assert.strictEqual(W.actLegendTip("Claude", 123456789), "Claude 123.5M tokens");
assert.strictEqual(W.planColbarTip(123456, 789012), "123.5K of 789K done in this section");
assert.strictEqual(W.swCountAria(123456), "Switch workstream, 123.5K open workstreams. Shortcut: slash.");
hover(W.actLegendTip("Claude", 123456789));
hover(W.actTimelineAria(123456, 120000, 3000, 400, 56, 7));
hover(W.actConcAria("Claude", 98765));
hover(W.burnupLegendTip("WS9", 123456789, "tokens", fmtN, false));
hover(W.burnupTasksAria(123456, 789012));
W.burnupDoneRows(123456, 789012).forEach(function (r) { hover(r.v); });
W.burnupWsRow(123456, "c").forEach(function (r) { hover(r.v); });
hover(W.depTaskLine({done: 123456, total: 789012, pct: 16}));
W.depTipRows({state: "active", progress: {done: 123456, total: 789012, pct: 16}, live_agents: 123456789}, 12345, 6789).forEach(function (r) { hover(r.v); });
hover(W.depAriaTip(12345, 1234567, 234567, 34567));
W.phaseSegTip("Planning", "cost", 1234567.891, 9876543.21, "var(--x)").forEach(function (r) { hover(r.v); });
W.phaseSegTip("Execution", "tokens", 123456789, 234567890, "var(--y)").forEach(function (r) { hover(r.v); });
hover(W.phaseFmt("cost", 1234567.891));
W.phaseStackDays({days: [1700000000], series: {planning: {tokens: [123456789]}, execution: {tokens: [23456789]}}}, "tokens").stacks.forEach(function (col) { W.phaseStackTip("tokens", col.segs, col.total).forEach(function (r) { hover(r.v); hover(r.k); }); });
W.phaseStackDays({days: [1700000000], series: {planning: {cost: [1234567.891]}, execution: {cost: [234567.891]}}}, "cost").stacks.forEach(function (col) { W.phaseStackTip("cost", col.segs, col.total).forEach(function (r) { hover(r.v); hover(r.k); }); });
var bar = W.phaseBar({planning: {tokens: 123456789}, execution: {tokens: 23456789}});
hover(bar.title);
hover(bar.attrs["aria-label"]);
hover(W.planAttText({runs: 123456, reviews: 7890, sentBack: 1234}));
hover(W.planSecTip({done: 123456, wip: 23456, pr: 3456, prog: 456, todo: 78901, deferred: 1234}, 54321, 4321));
hover(W.planColbarTip(123456, 789012));
hover(W.planBigAria(123456, 789012));
hover(W.planBigTip({done: 123456, wip: 23456, pr: 3456, prog: 456, todo: 78901, deferred: 1234}));
W.tipRows({tokens: {input: 90000000, output: 20000000, cache_read: 10000000, cache_write: 3456789}, elapsed_s: 98765, status: "done", verdict: "PASS"}).forEach(function (r) { hover(r.v); });
hover(W.tileTokensTitle(123456789, 98765432));
hover(W.tileTokensApproxTitle(123456789, 98765432));
hover(W.tileCostTitle(1234567.891, 234567.891));
hover(W.tileCostApproxTitle(1234567.891, 234567.891));
hover(W.tileFinTitle(123456, 23456));
hover(W.tileFinApproxTitle(123456, 23456));
hover(W.tileLiveTitle(123456, 23456));
hover(W.tileLiveApproxTitle(123456, 23456));
hover(JSON.stringify(W.tileCountChip(123456789, 100, "vs 24 h ago", W.tileLiveTitle(123456789, 100))));
hover(JSON.stringify(W.tilePctChip(123456789, 100000000, "vs prior 24 h", W.tileTokensTitle(123456789, 100000000))));
hover(W.wsProgAria("WS9", 123456, 789012, 16));
hover(W.wsProgTitle(123456, {pr: 23456, prog: 3456, wip: 456, todo: 78901, deferred: 1234}, 789012));
hover(W.wsPlanTitle(123456, {pr: 23456, prog: 3456, wip: 456, todo: 78901, deferred: 1234}, 789012));
hover(W.phProgAria("WS9", 123456, 789012));
hover(W.plProgAria("WS9", 123456, 789012));
hover(W.mgProgAria("WS9", 123456, 789012, 16));
hover(W.swCountAria(123456));
assert.strictEqual(W.progLegendTip("✓ Done", 0.6233), "✓ Done 62% of tasks");
hover(W.progLegendTip("✓ Done", 0.6233));
W.progMileRows(123456, 789012, 98765).forEach(function (r) { hover(r.v); });
W.progStepRows(123456, 789012, 3456).forEach(function (r) { hover(r.v); });
W.progBarRows(123456).forEach(function (r) { hover(r.v); });
hover(W.progTakeaway(123456, 789012, 67, "60%", 98765, 23456));
assert.ok(seen.length >= 40, "only " + seen.length + " hover strings reached the check");
var bad = seen.filter(function (s) { return /\d{5,}/.test(s) || /\d,\d{3}/.test(s); });
assert.deepStrictEqual(bad, [], "raw numbers survived in hover text: " + JSON.stringify(bad.slice(0, 5)));
console.log("hover builders proof ok: " + seen.length + " strings compact");
'''
        files = (
            [STATIC / "format.js"]
            + [STATIC / "cards" / name for name in ("activity.js", "burnup.js", "depgraph.js", "phases.js", "plan-progress.js", "tables.js", "tiles.js", "ws-cards.js", "project-home.js", "planned.js", "progress.js")]
            + [STATIC / "core" / name for name in ("manage.js", "switcher.js")]
        )
        proc = subprocess.run(
            ["node", "-e", harness] + [str(p) for p in files],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("hover builders proof ok", proc.stdout)

    def test_drawer_bytes_use_fmt_bytes(self):
        """Run-details byte rows go through fmtBytes ("950 B", "1.2 MB",
        "–" when unknown), never fmtN with a glued-on "B" ("-B")."""
        drawer = (STATIC / "core" / "drawer.js").read_text()
        self.assertNotIn('+ "B"', drawer, "the drawer still glues a B onto a count")
        for marker in ('["Output", fmtBytes(r.out_bytes)',
                        '["Errors", fmtBytes(r.err_bytes)',
                        'fmtBytes(r.rss_kb * 1024)'):
            self.assertIn(marker, drawer, f"the drawer lost its byte formatter: {marker}")
        self.assertIn("fmtBytes", drawer[:1500], "drawer.js no longer declares its fmtBytes need")

    def test_progress_comes_from_the_server(self):
        """Plan % done is computed once on the server. The pages read
        progress.pct/.done/.total; they never compute it from task counts."""
        script = inline_scripts()
        plan_js = (STATIC / "plan.js").read_text()
        for name, src in (("index.html", script), ("plan.js", plan_js)):
            hits = re.findall(r"Math\.round\s*\(\s*100\s*\*[^;]*?done", src, re.I)
            self.assertEqual(hits, [], f"{name} computes % done itself: {hits[:2]}")
            self.assertIn(".progress", src, f"{name} never reads the server progress object")

    def test_tiles_read_server_totals(self):
        """Token and cost tiles read the server snapshot totals for the tab's
        scope and the fixed 24 h window. They never re-sum usage buckets on
        the client."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tiles", bodies)
        body = bodies["tiles"]
        self.assertNotIn("state.usage", body, "tiles() re-sums client buckets; read snap.totals")
        self.assertIn("snap.totals", body)
        self.assertIn("totals_all_time", body)
        self.assertIn("data-tokens", body)

    def test_legend_total_is_the_selected_range(self):
        """The stack's token legend Total covers the selected range (the
        tile keeps the 24 h snapshot total), with data attributes for the
        browser check. No tile/chart cross line: a zoomed range legitimately
        differs from the tile."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("actTokenStrip", bodies)
        body = bodies["actTokenStrip"]
        self.assertIn("data-legend-total", body)
        self.assertIn("data-chart-sum", body)
        self.assertIn("in the selected range", body)
        self.assertNotIn("snap.totals", body, "the range-scoped legend must not read the tile total")
        self.assertNotIn("xline", body, "a zoomed range must not trip the tile cross-check")

    def test_polling_pauses_when_hidden(self):
        """Background work no-ops while the tab is hidden: stream ticks,
        warmups, snapshot ticks, and the refresh intervals check first so
        a hidden tab costs no fetches and no renders."""
        script = inline_scripts()
        bodies = dict(functions(script))
        for name in ("stream", "warmTabs", "refreshSnapshot"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            self.assertIn(
                "tabHidden", bodies[name], f"{name} polls while the tab is hidden"
            )
        page = (STATIC / "index.html").read_text()
        self.assertGreaterEqual(
            page.count("document.hidden"), 3, "the refresh intervals never pause when hidden"
        )

    def test_unchanged_tick_skips_redraw(self):
        """A stream tick whose snapshot renders identically skips drawMain:
        the signature covers the payload minus its clock, and refreshAll
        reseeds it so the next tick compares against the latest draw."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("snapSig", bodies["refreshSnapshot"])
        self.assertIn("state.snapSig", bodies["refreshSnapshot"])
        self.assertIn("state.snapSig", bodies["refreshAll"])

    def test_refresh_paths_read_one_snapshot_response(self):
        """Tiles and chart come from one snapshot response: refreshAll,
        refreshSnapshot (stream ticks) and warmTabs request usage_group with
        the snapshot and never fetch /api/usage on their own."""
        script = inline_scripts()
        bodies = dict(functions(script))
        for name in ("refreshAll", "refreshSnapshot", "warmTabs"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            self.assertIn("/api/snapshot", bodies[name], f"{name} no longer reads the snapshot")
            self.assertIn(
                "usage_group", bodies[name], f"{name} fetches the snapshot without its usage buckets"
            )
            self.assertNotIn("/api/usage", bodies[name], f"{name} still races a second usage fetch")
        self.assertNotIn("loadUsage", script, "loadUsage is gone; the groupBy toggle refetches the snapshot")

    def test_refresh_snapshot_applies_totals_with_usage(self):
        """No path replaces snap.totals without snap.usage: refreshSnapshot
        assigns state.snap and state.usage together under the tab+group
        guard (one fixed 24 h window), and so does every other writer."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("refreshSnapshot", bodies)
        body = bodies["refreshSnapshot"]
        self.assertIn("state.snap =", body)
        self.assertIn("state.usage =", body)
        self.assertIn("ADC.applies", body, "refreshSnapshot lost the tab+group guard")
        for name in ("refreshAll", "refreshSnapshot"):
            self.assertEqual(
                bodies[name].count("state.snap ="),
                bodies[name].count("state.usage ="),
                f"{name} assigns snap and usage different numbers of times",
            )
            self.assertGreater(bodies[name].count("state.snap ="), 0, name)
        self.assertEqual(
            script.count("state.snap ="),
            script.count("state.usage ="),
            "some writer replaces snap.totals without snap.usage",
        )

    def test_groupby_toggle_refetches_the_snapshot(self):
        """The By model/workstream toggle refetches the snapshot with the new
        group instead of racing a lone usage fetch."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("actTokenStrip", bodies)
        body = bodies["actTokenStrip"]
        self.assertIn("state.groupBy =", body)
        self.assertIn("refreshSnapshot", body)

    def test_cache_key_and_stale_guard(self):
        script = (
            "const fs=require('fs');"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const A=globalThis.ADC;"
            "const assert=require('assert');"
            "assert.strictEqual(A.WINDOW_HOURS,24);"
            "assert.strictEqual(A.key('WS2','model'),'WS2|model');"
            "assert.strictEqual(A.key('all','workstream'),'all|workstream');"
            "assert.strictEqual(A.key('WS2','model'),A.key('WS2','model'));"
            "assert.notStrictEqual(A.key('WS2','model'),A.key('WS1','model'));"
            "assert.notStrictEqual(A.key('all','model'),A.key('all','workstream'));"
            "assert.strictEqual(A.fresh({at:1000},31000),true);"
            "assert.strictEqual(A.fresh({at:1000},31001),false);"
            "assert.strictEqual(A.fresh(null,1000),false);"
            "assert.strictEqual(A.applies('WS2|model',{ws:'WS2',groupBy:'model'}),true);"
            "assert.strictEqual(A.applies('WS2|model',{ws:'WS2',groupBy:'workstream'}),true);"
            "assert.strictEqual(A.applies('WS1|model',{ws:'WS2',groupBy:'model'}),false);"
            "assert.strictEqual(A.applies('all|model',{ws:'all',groupBy:'workstream'}),false);"
            "assert.strictEqual(A.applies('all|workstream',{ws:'all',groupBy:'workstream'}),true);"
            "assert.strictEqual(A.billed({input:1,output:2,cache_read:3,cache_write:4,reasoning:50}),10);"
            "assert.strictEqual(A.ago(1000,990),'just now');"
            "assert.strictEqual(A.ago(10000,9000),'17 m ago');"
            "assert.strictEqual(A.ago(100000,90000),'3 h ago');"
            "assert.strictEqual(A.ago(1000000,800000),'2 d ago');"
            "assert.strictEqual(A.ago(1000,null),null);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "tokencache.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_fixed_24h_window(self):
        """No range control remains and every windowed read uses one fixed
        rolling 24 h window (since = now - 24 h at each refresh)."""
        page = (STATIC / "index.html").read_text()
        script = inline_scripts()
        self.assertNotIn('id="range"', page, "the #range control remains")
        self.assertNotIn("data-h=", script, "a range button remains")
        self.assertNotIn("state.hours", script, "state.hours remains")
        self.assertNotIn('getElementById("range")', script, "the range handler remains")
        self.assertIn("WINDOW_HOURS = 24", script, "no single 24 h constant")
        self.assertIn("function since()", script)
        since_body = dict(functions(script))["since"]
        self.assertIn("WINDOW_HOURS", since_body, "since() does not go through the 24 h window")
        self.assertNotIn("state.hours", since_body)
        for label in ("Agents finished (24 h)", "Tokens (24 h)", "Est. cost (24 h)", "Finished (24 h)", "0 in 24 h"):
            self.assertIn(label, script, f"tile or table does not say {label!r}")
        self.assertNotIn("finished today", script.lower(), "the calendar-day label remains")
        self.assertNotIn("Finished in the last hour", script, "the 1 h finished table remains")
        self.assertIn("finished_in_range", script, "tiles do not read the windowed finished count")
        checker = (STATIC.parent / "scripts" / "page_check.mjs").read_text()
        self.assertNotIn("clickRange", checker, "page_check still clicks a range")
        self.assertNotIn("RANGES", checker, "page_check still loops ranges")
        self.assertIn("WINDOW_HOURS = 24", checker, "page_check has no fixed 24 h window")

    def test_tab_filter_uses_the_shared_completed_rule(self):
        """The tab filter and the "Where we are" status share one completed
        rule over the server progress object: both call ADT.isCompleted, and
        neither computes % done from task counts itself."""
        script = inline_scripts()
        bodies = dict(functions(script))
        for name in ("programPanel", "renderTabs"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            self.assertIn("isCompleted(", bodies[name], f"{name} no longer uses the shared completed rule")
        self.assertGreaterEqual(script.count("isCompleted("), 2)
        self.assertIn("ADT.isCompleted", script)
        self.assertIn(".progress", script)

    def test_tabs_helpers(self):
        script = (
            "const fs=require('fs');"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const T=globalThis.ADT;"
            "const assert=require('assert');"
            "assert.strictEqual(T.isCompleted({done:77,total:77}),true);"
            "assert.strictEqual(T.isCompleted({done:1,total:41}),false);"
            "assert.strictEqual(T.isCompleted({done:0,total:0}),false);"
            "assert.strictEqual(T.isCompleted(null),false);"
            "assert.strictEqual(T.isCompleted({done:0,total:17}),false);"
            "const rows=[{ws:'all',name:'Overall'},{ws:'WS1',progress:{done:77,total:77}},{ws:'WS2',progress:{done:1,total:41}},{ws:'WS9',progress:null}];"
            "assert.deepStrictEqual(T.applyOrder(rows,['WS2','WS1']).map(r=>r.ws),['all','WS2','WS1','WS9']);"
            "assert.deepStrictEqual(T.applyOrder(rows,['WS8','WS1']).map(r=>r.ws),['all','WS1','WS2','WS9']);"
            "assert.deepStrictEqual(T.applyOrder(rows,null).map(r=>r.ws),['all','WS1','WS2','WS9']);"
            "assert.deepStrictEqual(T.countByMode(rows),{all:3,active:2,completed:1});"
            "assert.deepStrictEqual(T.filterByMode(rows,'completed','all').map(r=>r.ws),['all','WS1']);"
            "assert.deepStrictEqual(T.filterByMode(rows,'active','all').map(r=>r.ws),['all','WS2','WS9']);"
            "const kept=T.filterByMode(rows,'completed','WS2');"
            "assert.deepStrictEqual(kept.map(r=>r.ws),['all','WS1','WS2']);"
            "assert.strictEqual(kept.filter(r=>r.ws==='WS2')[0].muted,true);"
            "assert.strictEqual(kept.filter(r=>r.ws==='all')[0].muted,false);"
            "assert.deepStrictEqual(T.move(['WS1','WS2','WS7'],'WS7',-1),['WS1','WS7','WS2']);"
            "assert.deepStrictEqual(T.move(['WS1','WS2','WS7'],'WS1',-1),['WS1','WS2','WS7']);"
            "assert.deepStrictEqual(T.move(['WS1','WS2'],'WS9',1),['WS1','WS2']);"
            "assert.strictEqual(T.completeTitle({done:77,total:77}),'complete: 77 of 77 tasks done');"
            "assert.strictEqual(T.normMode('bogus'),'all');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "tabs.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_run_to_task_rule_agrees_with_the_server(self):
        """The page's two taskOf copies and dash/taskmatch.py share one rule:
        exact label, /regex/ keys, WS1/WS2/WS7 built-ins, plan-id substring."""
        import json as _json
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from dash import taskmatch

        taskmap = {
            "WS1": {"ws1-schema": "12", "/^ws1-migrate/": "13"},
            "WS2": {"ws2-routes": "WS2.3"},
            "WS7": {"ws7-landing": "WS7.LANDING", "/^ws7-notes/": "WS7.NOTES"},
        }
        ids = {
            "WS1": ["12", "13", "84", "7"],
            "WS2": ["WS2.3", "WS2.4", "WS2.12b"],
            "WS7": ["WS7.LANDING", "WS7.NOTES", "WS7.CHECK"],
            "WS9": ["W1", "W10"],
        }
        samples = [
            ("WS1", "ws1-schema", "12"),
            ("WS1", "ws1-migrate-9", "13"),
            ("WS1", "impl WS1.84", "84"),
            ("WS1", "WS1.7", "7"),
            ("WS1", "review WS1.13", "13"),
            ("WS1", "nothing here", None),
            ("WS2", "ws2-routes", "WS2.3"),
            ("WS2", "fix WS2.4 soon", "WS2.4"),
            ("WS2", "ship WS2.12b", "WS2.12b"),
            ("WS2", "ws2-9-x", "WS2.9"),
            ("WS7", "ws7-landing", "WS7.LANDING"),
            ("WS7", "ws7-notes-2", "WS7.NOTES"),
            ("WS7", "gate P0-12 open", "P0-12"),
            ("WS7", "WS7.check-x", "WS7.CHECK"),
            ("WS9", "ws9-W10-fix", "W10"),
            ("WS9", "ws9-W1-x", "W1"),
            ("WS9", "unrelated", None),
        ]
        for ws, label, expected in samples:
            self.assertEqual(
                taskmatch.task_of(label, ws, taskmap, ids.get(ws, [])),
                expected,
                f"server rule drifts on {label!r}",
            )
        harness = (
            "var TASKMAP=" + _json.dumps(taskmap) + ";"
            "var IDS=" + _json.dumps(ids) + ";"
            "function planTaskIds(ws){return IDS[ws]||[];}"
            "function taskmapGet(){return TASKMAP;}"
            + _extract_taskof(STATIC / "index.html", "taskOfExec")
            + _extract_taskof(STATIC / "plan.js", "taskOfPlan")
            + "var SAMPLES=" + _json.dumps([[ws, label] for ws, label, _ in samples]) + ";"
            "var OUT=SAMPLES.map(function(s){"
            "return [taskOfExec({label:s[1]},s[0]),taskOfPlan({label:s[1]},s[0],IDS[s[0]]||[])];});"
            "console.log(JSON.stringify(OUT));"
        )
        proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        rows = _json.loads(proc.stdout)
        self.assertEqual(len(rows), len(samples))
        for (ws, label, expected), (exec_hit, plan_hit) in zip(samples, rows):
            self.assertEqual(exec_hit, expected, f"index.html taskOf drifts on {label!r}")
            self.assertEqual(plan_hit, expected, f"plan.js taskOf drifts on {label!r}")
        phantom = [
            ("WS7", "ws7-ops-gold-rebuild", None),
            ("WS1", "ws1-99-missing", None),
            ("WS1", "ws1-schema", "12"),
            ("WS7", "ws7-landing", "WS7.LANDING"),
        ]
        for ws, label, expected in phantom:
            self.assertEqual(
                taskmatch.task_of_verified(label, ws, taskmap, ids.get(ws, [])),
                expected,
                f"server verified rule drifts on {label!r}",
            )
            if expected is None:
                self.assertIsNotNone(
                    taskmatch.task_of(label, ws, taskmap, ids.get(ws, [])),
                    f"the raw rule no longer maps {label!r}; the guard must be what drops it",
                )
        vexec = _extract_taskof(STATIC / "index.html", "taskOfVerifiedExec", func="taskOfVerified").replace("taskOf(", "taskOfExec(")
        vplan = _extract_taskof(STATIC / "plan.js", "taskOfVerifiedPlan", func="taskOfVerified").replace("taskOf(", "taskOfPlan(")
        harness2 = (
            "var TASKMAP=" + _json.dumps(taskmap) + ";"
            "var IDS=" + _json.dumps(ids) + ";"
            "function planTaskIds(ws){return IDS[ws]||[];}"
            "function taskmapGet(){return TASKMAP;}"
            + _extract_taskof(STATIC / "index.html", "taskOfExec")
            + _extract_taskof(STATIC / "plan.js", "taskOfPlan")
            + vexec + vplan
            + "var PH=" + _json.dumps([[ws, label] for ws, label, _ in phantom]) + ";"
            "var VOUT=PH.map(function(s){"
            "return [taskOfVerifiedExec({label:s[1]},s[0]),taskOfVerifiedPlan({label:s[1]},s[0],IDS[s[0]]||[])];});"
            "console.log(JSON.stringify(VOUT));"
        )
        proc = subprocess.run(["node", "-e", harness2], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        vrows = _json.loads(proc.stdout)
        self.assertEqual(len(vrows), len(phantom))
        for (ws, label, expected), (exec_hit, plan_hit) in zip(phantom, vrows):
            self.assertEqual(exec_hit, expected, f"index.html verified guard drifts on {label!r}")
            self.assertEqual(plan_hit, expected, f"plan.js verified guard drifts on {label!r}")

    def test_plan_map_layout_helpers(self):
        """The plan card's layout helpers behave like dash/maplayout.py:
        shortChipLabel strips the WS prefix and caps at 14 chars, layoutOf
        passes the server layout through and falls back to null, and the
        exported taskOfVerified drops phantom ids against the plan ids."""
        script = (
            "const fs=require('fs');"
            "const src=fs.readFileSync(process.argv[1],'utf8');"
            "var window={};"
            "var state={plan:{ws:'WS7',groups:[{id:'g',tasks:[{id:'WS7.LANDING'},{id:'WS7.NOTES'}]}]}};"
            "var TASKMAPSEED={'WS7':{}};"
            "eval(src);"
            "window.TASKMAP=TASKMAPSEED;"
            "const assert=require('assert');"
            "assert.strictEqual(window.shortChipLabel('WS8.52'),'52');"
            "assert.strictEqual(window.shortChipLabel('74b'),'74b');"
            "assert.strictEqual(window.shortChipLabel('WS7.LANDING'),'LANDING');"
            "assert.strictEqual(window.shortChipLabel('op-rp-manifest-dryrun'),'op-rp-manifes\\u2026');"
            "assert.strictEqual(window.shortChipLabel('WS8.123456789012345'),'1234567890123\\u2026');"
            "assert.strictEqual(window.layoutOf(null),null);"
            "assert.strictEqual(window.layoutOf({}),null);"
            "assert.strictEqual(window.layoutOf({layout:null}),null);"
            "assert.strictEqual(window.layoutOf({layout:{density:'compact'}}).density,'compact');"
            "assert.strictEqual(window.taskOfVerified({label:'ws7-landing'},'WS7'),'WS7.LANDING');"
            "assert.strictEqual(window.taskOfVerified({label:'ws7-ops-gold-rebuild'},'WS7'),null);"
            "state.plan={ws:'WS9',groups:[]};" 
            "assert.strictEqual(window.taskOfVerified({label:'ws7-ops-gold-rebuild'},'WS7'),'WS7.OPS');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "cards" / "plan-progress.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_plan_map_layout_branches(self):
        """The plan card consumes the server layout and keeps every
        interaction: collapse and slim expanders, the arrows toggle,
        hover/pin focus, the one-line phases strip and the compact legend."""
        card = (STATIC / "cards" / "plan-progress.js").read_text()
        for marker in (
            "plan.layout",
            "more_token",
            "deferred_token",
            "hidden_done_ids",
            "slim-tile",
            "fold-rail",
            "phases_line",
            "phases-line",
            "map-legend",
            "arrows-why",
            "fanin",
            "tile-waits",
            "trcap",
            "more-btn",
            "aria-expanded",
            "foldedIds",
        ):
            self.assertIn(marker, card, f"plan-progress.js lost the layout branch: {marker}")
        for marker in (
            "function taskOfVerified(",
            "function shortChipLabel(",
            "function layoutOf(",
            "function makeChip(",
            "function buildTile(",
            "function slimTile(",
            "function paintStage(",
        ):
            self.assertEqual(
                len(re.findall(rf"function {marker[9:-1]}\(", card)),
                1,
                f"{marker} must be defined exactly once",
            )
        for kept in (
            "drawAllTaskEdges",
            "focusTask",
            "clearTask",
            "groupEnter",
            "arrows-toggle",
            "aria-pressed",
            "function rpCursorTime(",
            "function rpSeek(",
            "__adReplay",
        ):
            self.assertIn(kept, card, f"plan-progress.js lost kept behaviour: {kept}")
        css = (STATIC / "cards" / "plan-progress.css").read_text()
        for marker in (
            ".dag.fitted",
            ".dag.compact",
            ".slim-tile",
            ".more-btn",
            ".fanin",
            ".phases-line",
            ".map-legend",
            ".tile-waits",
            ".balance-note",
            ".fold-rail",
            "@media (max-width:640px)",
            "prefers-reduced-motion",
        ):
            self.assertIn(marker, css, f"plan-progress.css lost the layout rule: {marker}")
        page = (STATIC / "index.html").read_text()
        self.assertIn("/static/cards/plan-progress.css", page, "index.html never loads the card CSS")

    def test_plan_headline_has_no_phase_wording(self):
        """The plan progress headline is counts only ("N of M tasks done ·
        K to go · J agents working"): sections run in parallel, so no
        phase-sequence wording ("now in X (phase i of n)", "active in n of
        m phases", "next up", "every phase done")."""
        card = (STATIC / "cards" / "plan-progress.js").read_text()
        for gone in (
            "phaseTxt",
            "every phase done",
            "(phase ",
            "next up: ",
            "active in ",
            "now in ",
            "done in this phase",
            "Each tile is a stage of the plan, left to right",
            "done in this group",
            "Each tile is a group of tasks",
            "Work groups",
            "Later groups",
            "Agents are building tasks in this group",
            "every group done",
            "which group waits on",
        ):
            self.assertNotIn(gone, card, f"plan-progress.js still says {gone!r}")
        for kept in (
            "planHeadline",
            "tasks done",
            " to go",
            " working",
            "done in this section",
            "sections run in parallel",
            "Sections",
            "Each tile is a section of the plan",
        ):
            self.assertIn(kept, card, f"plan-progress.js lost {kept!r}")

    def test_plan_legend_is_always_visible(self):
        """The map legend is an always-visible list under the intro line,
        not a hover-only popover: labelled Task status / Agent family /
        Edges groups, family colours from the model palette. The old
        Legend hover popover is gone (owner request)."""
        card = (STATIC / "cards" / "plan-progress.js").read_text()
        for kept in (
            "planLegend",
            "legData = planLegend(",
            'role: "list"',
            "Plan map legend",
            "Task status",
            "Agent family",
            '"Edges"',
            "famColor",
            "arrows-why",
            "map-legend",
            "mleg-group",
        ):
            self.assertIn(kept, card, f"plan-progress.js lost the visible legend: {kept}")
        css = (STATIC / "cards" / "plan-progress.css").read_text()
        for marker in (".map-legend", ".mleg-group", ".mleg-item", ".fam-dot", ".phases-cap"):
            self.assertIn(marker, css, f"plan-progress.css lost the legend rule: {marker}")
        self.assertNotIn("legend-tip", card, "the Legend hover popover is back")
        self.assertNotIn("legendPop", card, "the Legend hover popover is back")
        self.assertNotIn(".legend-tip", css, "dead hover popover CSS is back")

    def test_plan_map_fits_the_page(self):
        """The plan progress card never pushes the page sideways: tiles wrap
        or scroll inside their own well, the legend row and the strips wrap
        inside the card, and no resize loop can grow the map. Regression pin
        for the sections-commit overflow, where a bare .sec nowrap rule
        (meant for the run-table section label) leaked into the section.sec
        card wrappers and, with the dag well set to visible overflow, sized
        the grid to thousands of pixels."""
        tables = (STATIC / "cards" / "tables.css").read_text()
        self.assertNotRegex(
            tables, r"(?m)^\.sec\s*\{",
            "tables.css has a bare .sec rule again: it matches the section.sec "
            "card wrappers and forces whole cards to nowrap",
        )
        self.assertIn("td .sec", tables, "the run-table section label lost its cell scope")
        css = (STATIC / "cards" / "plan-progress.css").read_text()
        self.assertIn(
            ".dag-scroll.fitted{overflow-x:auto",
            css,
            "the fitted dag well must scroll internally, not overflow visibly",
        )
        self.assertNotIn(
            ".dag-scroll.fitted{overflow-x:visible",
            css,
            "visible overflow on the dag well pushes the page sideways",
        )
        for marker in (
            "#exec-progress>*",
            "max-width:100%",
            ".dcol",
            "min-width:0",
            "overflow-wrap:anywhere",
        ):
            self.assertIn(marker, css, f"plan-progress.css lost the fit rule: {marker}")
        self.assertRegex(
            css, r"\.map-legend\{[^}]*flex-wrap:\s*wrap",
            "the always-visible legend must wrap inside the card",
        )
        card = (STATIC / "cards" / "plan-progress.js").read_text()
        self.assertNotIn(
            "ResizeObserver", card,
            "no ResizeObserver may size the map: an observer writing sizes "
            "it reads back is a feedback loop that grows with content",
        )

    def test_plan_legend_and_headline_helpers(self):
        """Node-driven pure test of the extracted builders: planHeadline
        formats counts with no phase wording, and planLegend covers every
        status plus the model-palette family colours in palette order."""
        script = (
            "const fs=require('fs');"
            "var window={};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "var ADM=window.ADM;"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "const assert=require('assert');"
            "const H=window.AdPage.cards.planHeadline,L=window.AdPage.cards.planLegend;"
            "assert.strictEqual(typeof H,'function');"
            "assert.strictEqual(typeof L,'function');"
            "assert.strictEqual(H(74,100,3),'74 of 100 tasks done \\u00B7 26 to go \\u00B7 3 agents working');"
            "assert.strictEqual(H(2,5,1),'2 of 5 tasks done \\u00B7 3 to go \\u00B7 1 agent working');"
            "assert.strictEqual(H(5,5,0),'5 of 5 tasks done \\u00B7 0 to go');"
            "['phase','stage','now in','next up','every'].forEach(function(w){"
            "assert.ok(H(1,4,2).toLowerCase().indexOf(w)===-1,'headline says '+w);});"
            "var d=L(['muse','claude','glm']);"
            "assert.deepStrictEqual(d.families.map(function(f){return f.fam;}),['claude','muse','glm']);"
            "assert.deepStrictEqual(d.families.map(function(f){return f.color;}),['var(--s1)','var(--s5)','var(--s6)']);"
            "assert.deepStrictEqual(d.families.map(function(f){return f.name;}),['Claude','muse','GLM']);"
            "['todo','wip','pr','prog','done','deferred'].forEach(function(k){"
            "assert.ok(d.status.some(function(s){return s.key===k&&s.glyph&&s.word&&s.cls;}),'status '+k+' missing');});"
            "assert.ok(d.status.some(function(s){return s.key==='just';}));"
            "assert.ok(d.status.some(function(s){return s.key==='sentback';}));"
            "assert.ok(d.edges.some(function(e){return e.style==='solid'&&e.color==='var(--good)';}));"
            "assert.ok(d.edges.some(function(e){return e.style==='dashed'&&e.color==='var(--muted)';}));"
            "assert.ok(d.edges.some(function(e){return e.glyph==='\\u25C7';}));"
            "assert.deepStrictEqual(L([]).families,[]);"
            "assert.deepStrictEqual(L(null).families,[]);"
            "assert.strictEqual(L(['zzz']).families[0].color,'var(--other)');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "models.js"), str(STATIC / "cards" / "plan-progress.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_replay_and_timeline_share_one_playhead(self):
        """The plan progress map and the run timeline play back together:
        both paint from the one rpCursorTime playhead, and the one rpSeek
        path moves them both."""
        script = inline_scripts()

        def body(name):
            start = script.index(f"function {name}(")
            brace = script.index("{", start)
            depth = 0
            for pos in range(brace, len(script)):
                if script[pos] == "{":
                    depth += 1
                elif script[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        return script[start : pos + 1]
            raise AssertionError(f"{name} body never closes")

        self.assertEqual(
            len(re.findall(r"function rpCursorTime\(", script)),
            1,
            "the playhead time source must be defined exactly once",
        )
        paint, seek, card = body("rpPaint"), body("rpSeek"), body("rpTimelineCard")
        self.assertIn("rpCursorTime(", paint, "the map no longer reads the shared playhead")
        self.assertIn("rpCursorTime(", seek, "seek no longer reads the shared playhead")
        self.assertIn("rpPaint(", seek, "seek no longer repaints both cards")
        self.assertIn("rpPaintTimeline(", paint, "the map paint no longer advances the timeline")
        self.assertIn("rpSeek(", card, "the timeline scrub no longer moves the shared playhead")

    def test_timeline_axis_uses_the_warp_mapping(self):
        """The timeline x axis is the same warp as the playhead: one
        time->position mapping (rpTimeToFrac over the warp points) positions
        the axis, ticks, strip, bars and cursor, inverting the rpCursorTime
        playhead. No second linear axis may sneak back into the card."""
        script = inline_scripts()

        def body(name):
            start = script.index(f"function {name}(")
            brace = script.index("{", start)
            depth = 0
            for pos in range(brace, len(script)):
                if script[pos] == "{":
                    depth += 1
                elif script[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        return script[start : pos + 1]
            raise AssertionError(f"{name} body never closes")

        for name in ("rpCursorTime", "rpWarpT", "rpWarpX", "rpTimeToFrac", "rpWarpBreaks", "rpRebuildTimeline"):
            self.assertEqual(
                len(re.findall(rf"function {name}\(", script)),
                1,
                f"{name} must be defined exactly once: one shared warp mapping",
            )
        cursor, to_frac = body("rpCursorTime"), body("rpTimeToFrac")
        self.assertIn("rpWarpT(", cursor, "the playhead no longer reads the warp")
        self.assertIn("RP.compress", cursor)
        self.assertIn("rpWarpX(", to_frac, "the axis no longer inverts the playhead warp")
        self.assertIn("RP.compress", to_frac)
        self.assertIn("RP.warp", to_frac)
        card = body("rpTimelineCard")
        self.assertIn("pw * rpTimeToFrac(t)", card, "the timeline axis is not the shared warp mapping")
        self.assertNotIn("(t - t0)", card, "a linear wall-clock axis sneaked back into the timeline card")
        self.assertIn("timeTicks(t0, t1, X", card, "ticks no longer sit on the shared axis")
        self.assertIn("minGap", card, "ticks lost their warp-aware thinning option")
        self.assertIn("rpWarpBreaks()", card, "squeezed idle stretches lost their break markers")
        self.assertIn("br.label", card, "break markers lost their plain-words labels")
        self.assertEqual(script.count("rpRebuildTimeline("), 2, "the warp toggle must rebuild the timeline card")
        self.assertIn("rpPackRuns(", card)
        pack = body("rpPackRuns")
        self.assertIn("rpTimeToFrac(", pack, "rows are no longer packed in warped axis space")

    def test_warp_helpers_round_trip(self):
        """AD.warpTime and AD.warpPos invert each other, and timeTicks
        thins crowded labels while keeping real dates and the end tick."""
        script = (
            "const fs=require('fs');"
            "const src=fs.readFileSync(process.argv[1],'utf8').replace('})(window);','})(global);');"
            "eval(src);"
            "const assert=require('assert');"
            "const pts=[{w:0,t:1000},{w:0.25,t:2000},{w:0.5,t:3000},{w:0.75,t:50000},{w:1,t:100000}];"
            "assert.strictEqual(AD.warpTime(pts,0,1000,100000),1000);"
            "assert.strictEqual(AD.warpTime(pts,1,1000,100000),100000);"
            "for(const f of [0.05,0.2,0.37,0.6,0.83,0.97]){"
            "const t=AD.warpTime(pts,f,1000,100000);"
            "assert.ok(Math.abs(AD.warpPos(pts,t,1000,100000)-f)<1e-9,'round trip at '+f);}"
            "for(const t of [1000,1500,2000,30000,99999,100000]){"
            "const f=AD.warpPos(pts,t,1000,100000);"
            "assert.ok(Math.abs(AD.warpTime(pts,f,1000,100000)-t)<1,'inverse at '+t);}"
            "assert.strictEqual(AD.warpTime(null,0.5,10,20),15);"
            "assert.strictEqual(AD.warpPos(null,15,10,20),0.5);"
            "var d=new Date();d.setHours(0,0,0,0);"
            "var t0=d.getTime()/1000,t1=t0+2*86400;"
            "var X=function(t){return (t-t0)/(t1-t0)*1000;};"
            "var full=AD.timeTicks(t0,t1,X,6);"
            "assert.strictEqual(full.length,5);"
            "var thin=AD.timeTicks(t0,t1,X,6,{minGap:1e9});"
            "assert.strictEqual(thin.length,2);"
            "assert.strictEqual(thin[thin.length-1].anchor,'end');"
            "var avoided=AD.timeTicks(t0,t1,X,6,{minGap:1e9,avoid:[full[0].x]});"
            "assert.strictEqual(avoided.length,1);"
            "var squeezed=AD.timeTicks(t0,t1,function(){return 500;},6,{minGap:10});"
            "assert.ok(squeezed.length>=1&&squeezed.length<=2);"
            "assert.ok(full.every(function(k){return typeof k.label==='string'&&k.label.length>0;}));"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "format.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_cost_tile_names_unpriced_tokens(self):
        """The cost tile excludes unpriced models' tokens and says so,
        naming the models; the priced state names nothing."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tiles", bodies)
        body = bodies["tiles"]
        self.assertIn("unpriced_tokens", body, "tiles() lost the unpriced token count")
        self.assertIn("unpriced_models", body, "tiles() lost the unpriced model names")
        self.assertIn("tokens unpriced", body, "cost tile no longer says whose tokens it excludes")
        self.assertIn("data-unpriced-tokens", body, "cost tile lost its unpriced data attribute")

    def test_tiles_delta_chips(self):
        """KPI tiles carry a change chip and a sparkline: deltas prefer the
        exact rolling priors in snap.prev_24h, fall back to the hour-bucket
        approximation labelled approx. in the chip tooltip, and render a
        prior-zero window as "new past 24 h", never an infinite percent."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tiles", bodies)
        body = bodies["tiles"]
        self.assertIn("prev_24h", body, "tiles() ignores the exact rolling priors")
        self.assertIn("approx.", body, "the bucket fallback never says approx.")
        self.assertIn("tilePctChip", bodies)
        self.assertIn("tileCountChip", bodies)
        self.assertIn("past 24 h", bodies["tilePctChip"], "prior-zero has no new-past-24h chip")
        self.assertIn("past 24 h", bodies["tileCountChip"], "live prior-zero has no new chip")
        self.assertNotIn("Infinity", script, "a delta can read as an infinite percent")
        self.assertIn("tileSpark(", body, "tiles() draws no sparkline")
        self.assertIn("tileSpark", bodies)
        self.assertIn("aria-hidden", bodies["tileSpark"], "the sparkline is not marked decorative")

    def test_stale_badge_reads_tick_freshness(self):
        """The tick carries seq + freshness; the page badges stale when the
        seq stays frozen for 3 poll intervals with an open SSE stream."""
        script = inline_scripts()
        bodies = dict(functions(script))
        page = (STATIC / "index.html").read_text()
        self.assertIn('id="stale-badge"', page, "no stale badge element")
        self.assertIn("updateStale", script, "no staleness check")
        self.assertIn("STALE_AFTER_S", script)
        self.assertIn("lastSeqChange", script, "the page never tracks seq age")
        self.assertIn("last_poll", script, "the tick freshness never reaches the page")
        self.assertIn("stream", bodies)
        self.assertIn("d.seq", bodies["stream"], "the tick handler ignores the seq")
        self.assertIn("refreshSnapshot", bodies["stream"], "a changed seq never refetches")

    def test_finished_truncation_is_visible(self):
        """The Finished table names its truncation ("Newest 50 of N") from
        the snapshot counts instead of silently showing 50 rows."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("finishedSub", script, "no truncation subtitle helper")
        self.assertIn("recent_shown", script)
        self.assertIn("recent_total", script)
        self.assertIn("finished_in_range", script)
        self.assertIn("Newest ", bodies["finishedSub"], "subtitle never says Newest N")
        self.assertIn(" of ", bodies["finishedSub"], "subtitle never says N of M")
        self.assertIn("finishedSub(", bodies["drawMain"], "the Finished table ignores the subtitle")

    def test_fetch_failures_are_loud(self):
        """A failed snapshot fetch shows an error banner and a failed-scope
        state, never a silent zero."""
        script = inline_scripts()
        bodies = dict(functions(script))
        page = (STATIC / "index.html").read_text()
        self.assertIn('id="err-banner"', page, "no error banner element")
        self.assertIn("request failed", script, "no failure text on the page")
        self.assertIn("data-failed-scope", script, "no failed-scope state")
        self.assertIn("showSnapError", script)
        self.assertIn("renderErrBanner", script)
        for name in ("refreshAll", "refreshSnapshot"):
            self.assertIn("showSnapError", bodies[name], f"{name} swallows a fetch failure")
        self.assertIn("request failed", bodies["tiles"], "tiles() reads zero on a failed scope")
        self.assertIn("snapError", bodies["drawMain"], "drawMain never consults the failure flag")

    def test_agent_areas_never_branch_on_prod_jobs(self):
        """Agent areas draw server-provided agent rows: no execution-view
        code branches on source "dagster". The one compact job number lives
        in the tiles ("N prod jobs", "N prod jobs ran"), and the replay
        guard (rp* functions, owned by another worker) is exempt."""
        script = inline_scripts()
        lines = script.splitlines()
        bad = []
        for number, line in enumerate(lines, 1):
            if '"dagster"' not in line and "'dagster'" not in line:
                continue
            owner = None
            for back in range(number, max(0, number - 400), -1):
                match = re.search(r"function (\w+)\s*\(", lines[back - 1])
                if match:
                    owner = match.group(1)
                    break
            if owner == "tiles" or (owner or "").startswith("rp"):
                continue
            bad.append(f"line {number} in {owner}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "execution view still branches on prod-job source")
        bodies = dict(functions(script))
        self.assertIn("prod jobs", bodies["tiles"], "live tile lost the prod-jobs number")
        self.assertIn("prod jobs ran", bodies["tiles"], "live tile lost the finished-jobs number")

    def test_page_has_no_doctor_ui(self):
        """The dashboard page shows no doctor output: no health pill, Health
        card, problem badge or plan-attention line. Doctor output is for the
        CLI (agent_dash.py doctor) and the API (GET /api/doctor) only; the
        owner decided against health or warning cards on the page."""
        page = (STATIC / "index.html").read_text()
        for marker in ("doctor-pill", "DashDoctor", "refreshDoctor", "docwarn", "exec-health", "plan needs attention"):
            self.assertNotIn(marker, page, f"index.html still has doctor UI: {marker}")
        self.assertFalse((STATIC / "doctor.js").exists(), "static/doctor.js is back")

    def test_chart_cards_expose_agent_data(self):
        """The stack strips carry raw values for agents: the token legend
        entries have data-key/data-v, the bar samples data-t/data-v, the
        peak bar a direct label; every live strip has its own y axis, a
        gradient fill and a pk annotation, and an empty window names what
        is missing ("No usage/runs/live agents in this window"), never a
        silent zero or a failure."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("actTokenStrip", bodies)
        tok = bodies["actTokenStrip"]
        for needle in ("data-key", "data-v", "data-t", "peaklab", "data-legend-total"):
            self.assertIn(needle, tok, f"actTokenStrip lost {needle!r}")
        self.assertIn("actConcStrip", bodies)
        conc = bodies["actConcStrip"]
        for needle in ("data-fam", "data-peak", "pk ", "linearGradient"):
            self.assertIn(needle, conc, f"actConcStrip lost {needle!r}")
        self.assertIn("actConcStrips", bodies)
        paint = bodies["actPaintStrips"]
        for needle in ("No live agents in this window", "data-failed-scope", "request failed"):
            self.assertIn(needle, paint, f"the stack hides an empty live strip: {needle!r}")

    def test_conc_strips_come_from_families_not_a_tool_list(self):
        """The live-agents strip list is built from the window's model
        families (actConcFams over ADM.FAMS), never a hard-coded tool list:
        a DeepSeek run earns a DeepSeek strip, and a family with no runs in
        the window gets no strip. The hover tooltip breaks the same
        families down to exact per-model counts."""
        src = (STATIC / "cards" / "activity.js").read_text()
        self.assertNotIn('["claude", "cursor", "muse", "glm"]', src)
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("actConcFams", bodies)
        strips = bodies["actConcStrips"]
        self.assertIn("actConcFams", strips)
        self.assertIn("ADM.FAMS", bodies["actConcFams"])
        tip = bodies["actConcStrip"]
        for needle in ("data-fam", "data-peak", "actConcFams(all)"):
            self.assertIn(needle, tip, f"actConcStrip lost {needle!r}")

    def test_conc_fams_node(self):
        """Node proof for the pure strip-family builder: model keys in give
        ADM.FAMS-ordered families out, DeepSeek included, empties dropped."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
assert.strictEqual(typeof actConcFams, "function", "actConcFams is not exported");
assert.deepStrictEqual(actConcFams(["glm-5.3-flash", "deepseek-flash"]), ["glm", "deepseek"]);
assert.deepStrictEqual(actConcFams(["deepseek-flash", "glm-5.3-flash", "deepseek-v4-pro"]), ["glm", "deepseek"]);
assert.deepStrictEqual(actConcFams(["glm-unknown"]), ["glm"]);
assert.deepStrictEqual(actConcFams(["other"]), ["other"]);
assert.deepStrictEqual(actConcFams([]), []);
assert.deepStrictEqual(actConcFams(null), []);
assert.strictEqual(ADM.famColor("deepseek"), "var(--s8)", "DeepSeek strip lost its --s8 hue");
console.log("conc fams proof ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "conc_fams.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "models.js"), str(STATIC / "cards" / "activity.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_unknown_duration_is_a_dash(self):
        script = (
            "const fs=require('fs');"
            "const src=fs.readFileSync(process.argv[1],'utf8').replace('})(window);','})(global);');"
            "eval(src);"
            "const dash='\\u2013';"
            "if(AD.fmtDur(null)!==dash) process.exit(2);"
            "if(AD.fmtDur(undefined)!==dash) process.exit(3);"
            "if(AD.fmtDur(0)!=='0s') process.exit(4);"
            "if(AD.fmtDur(90)!=='1m 30s') process.exit(5);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "format.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


    def test_timeline_source_contract(self):
        """The timeline strip keeps its integration points: the axis comes
        from the shared helpers, every bar carries its run id and outcome,
        clipped bars carry a visible cap, and numbers stay compact."""
        src = (STATIC / "cards" / "activity.js").read_text()
        for token in ("axes(", "timeTicks(", "data-run-id", "data-outcome", "data-clipped", "tl-clip"):
            self.assertIn(token, src, f"activity.js lost {token}")
        for bad in (".toFixed(", "toLocaleString()", '"dagster"', "'dagster'"):
            self.assertNotIn(bad, src, f"activity.js must not contain {bad}")

    def test_timeline_draws_every_run(self):
        """The timeline strip draws every run in the selected range, never
        a sample: the real actTimelineStrip() renders one bar per run
        (data-run-id) of a generated 640-run payload, every bar carries
        a data-outcome, the lane labels add up to the payload, a >400-row
        lane becomes a labelled dense strip that still expands to every
        bar, and a 2 h brush draws only the runs it covers with clipped
        bars carrying a visible edge cap."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
const files = process.argv.slice(2);
global.window = globalThis;
global.location = {search: ""};
function mkEl(tag) {
  return {tag: tag, attrs: {}, children: [], style: {}, dataset: {},
    textContent: "", __handlers: {},
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    getAttribute: function (k) { return this.attrs[k]; },
    appendChild: function (c) { this.children.push(c); return c; },
    insertBefore: function (c) { this.children.unshift(c); return c; },
    addEventListener: function (t, f) { (this.__handlers[t] = this.__handlers[t] || []).push(f); },
    getBoundingClientRect: function () { return {left: 0, width: 100, top: 0, height: 100}; }};
}
global.document = {
  createElement: mkEl,
  createElementNS: function (ns, t) { return mkEl(t); },
  createTextNode: function (t) { return {text: String(t)}; },
  getElementById: function (id) {
    if (id === "main") return {clientWidth: 1100};
    return mkEl("div");
  }
};
global.getComputedStyle = function () { return {getPropertyValue: function () { return ""; }}; };
global.innerWidth = 1440;
global.innerHeight = 900;
global.WINDOW_HOURS = 24;
global.openRun = function () {};
global.drawMain = function () {};
global.state = {runs: [], ws: "all"};
files.forEach(function (p) { eval(fs.readFileSync(p, "utf8")); });
["actTimelineStrip", "isAgent", "runEnd", "packRows", "outcomeOf", "outcomeColor"].forEach(function (n) {
  assert.strictEqual(typeof global[n], "function", n + " is not exported");
});
assert.strictEqual(typeof global.expanded, "object", "expanded lane state is not exported");
assert.deepStrictEqual(Object.keys(OUTCOME).sort(), ["blocked", "cancelled", "died", "ok", "part", "run", "stalled", "unk"]);
function walk(node, out) {
  out.push(node);
  (node.children || []).forEach(function (c) { if (c && c.children) walk(c, out); });
}
var GEO = {W: 1100, L: 150, R: 14, pw: 936};
var SEL = null;
function render() {
  if (!SEL) SEL = {from: T0, to: NOW};
  var nodes = [];
  walk(actTimelineStrip(SEL, {runs: state.runs}, NOW, GEO, false).node, nodes);
  return nodes;
}
function barsOf(nodes) {
  return nodes.filter(function (n) { return n.tag === "rect" && n.attrs.class === "tl-bar"; });
}
function textsOf(nodes, cls) {
  return nodes.filter(function (n) { return n.tag === "text" && n.attrs.class === cls; }).map(function (n) { return n.textContent; });
}
var NOW = Math.floor(Date.now() / 1000), T0 = NOW - 24 * 3600;
var SPECS = [
  {source: "claude-subagent", tier: "haiku", model: "claude-haiku-4"},
  {source: "grok", tier: "grok", model: "grok-4"},
  {source: "muse", tier: "muse", model: "muse-1"},
  {source: "glm", tier: "glm", model: "glm-4"}
];
var VERDICTS = ["PASS", "PARTIAL", "BLOCKED", "APPROVE", "APPROVE_WITH_CHANGES", "REQUEST_CHANGES", null];
function mkRun(i, spec, start, end, status, verdict) {
  return {run_id: "test:" + i, source: spec.source, tier: spec.tier, model: spec.model,
    label: "tl-" + i, workstream: "WS1", status: status, verdict: verdict,
    started_at: start, ended_at: end,
    elapsed_s: Math.max(0, (end == null ? NOW : end) - start), clock_source: "transcript"};
}
/* Scenario A: 640 runs across four families with moderate overlap. */
var runs = [];
for (var i = 0; i < 640; i++) {
  var spec = SPECS[i % 4], slot = Math.floor(i / 4) % 40;
  var start = T0 + 4000 + slot * 2000 + (i % 7), end = start + 1500;
  var status = "done", verdict = VERDICTS[i % VERDICTS.length];
  if (i % 40 === 7) { status = "running"; verdict = null; end = null; }
  else if (i % 40 === 13) { status = "killed"; verdict = null; }
  else if (i % 40 === 29) { status = "died_early"; verdict = null; }
  else if (i % 40 === 33) { status = "unknown"; verdict = null; }
  runs.push(mkRun(i, spec, start, end, status, verdict));
}
state.runs = runs;
state.ws = "all";
var nodes = render(), bars = barsOf(nodes);
assert.strictEqual(bars.length, runs.length, "bars " + bars.length + " != runs " + runs.length);
var labels = textsOf(nodes, "tl-lane-label").filter(function (t) { return t.indexOf("runs") >= 0; });
assert.ok(!labels.some(function (t) { return t.indexOf("(dense)") >= 0; }), "moderate overlap must not go dense");
var laneTotal = labels.reduce(function (a, t) { return a + Number(/(\d+) runs/.exec(t)[1]); }, 0);
assert.strictEqual(laneTotal, runs.length, "lane labels sum to " + laneTotal + ", payload has " + runs.length);
var outcomes = {};
bars.forEach(function (b) { outcomes[b.attrs["data-outcome"]] = true; });
["run", "ok", "part", "blocked", "died", "unk"].forEach(function (o) {
  assert.ok(outcomes[o], "no bar with outcome " + o);
});
bars.forEach(function (b) {
  assert.ok(b.__handlers.click && b.__handlers.click.length, "bar without click: " + b.attrs["data-run-id"]);
  assert.ok(b.__handlers.mousemove && b.__handlers.mousemove.length, "bar without hover: " + b.attrs["data-run-id"]);
  assert.ok(Number(b.attrs.width) >= 2, "bar narrower than 2px: " + b.attrs["data-run-id"]);
  assert.ok(Number(b.attrs.height) >= 2, "bar shorter than 2px: " + b.attrs["data-run-id"]);
});
var legend = nodes.filter(function (n) { return n.attrs.class === "legend"; })[0];
var legendText = JSON.stringify(legend);
["\u25CF running", "\u2713 finished", "\u25D0 partial", "\u2298 blocked", "\u2715 died", "\u25CB no verdict"].forEach(function (want) {
  assert.ok(legendText.indexOf(want) >= 0, "legend never says " + want);
});
["cancelled", "stalled"].forEach(function (absent) {
  assert.ok(legendText.indexOf(absent) < 0, "legend lists absent class " + absent);
});
/* Scenario B: one lane with 500 overlapping runs goes dense but keeps its count and its expand. */
runs = [];
for (var j = 0; j < 500; j++) runs.push(mkRun(j, SPECS[0], NOW - 3600, NOW - 600, "done", "PASS"));
for (var k = 500; k < 505; k++) runs.push(mkRun(k, SPECS[1], NOW - 3600 + (k - 500) * 100, NOW - 600, "done", "PASS"));
state.runs = runs;
Object.keys(expanded).forEach(function (key) { delete expanded[key]; });
nodes = render();
bars = barsOf(nodes);
assert.strictEqual(bars.length, 5, "dense lane must aggregate; other lane bars = 5, got " + bars.length);
labels = textsOf(nodes, "tl-lane-label").filter(function (t) { return t.indexOf("runs") >= 0; });
assert.ok(labels.some(function (t) { return t.indexOf("500 runs (dense)") >= 0; }), "dense label lost the true count: " + JSON.stringify(labels));
var heats = nodes.filter(function (n) { return n.attrs.class === "tl-heat"; });
assert.ok(heats.length > 0, "dense lane drew no heat cells");
var peak = Math.max.apply(null, heats.map(function (h) { return Number(h.attrs["data-dense-count"]); }));
assert.strictEqual(peak, 500, "dense peak " + peak + " != 500");
var denseFam = ADM.famOf(tierKey(runs[0]));
var hits = nodes.filter(function (n) { return n.attrs.class === "tl-lane-hit" && n.attrs["data-lane"] === denseFam; });
assert.strictEqual(hits.length, 1, "dense lane lost its expand hit area");
hits[0].__handlers.click.forEach(function (f) { f(); });
assert.strictEqual(expanded[denseFam], true, "dense lane click did not expand");
nodes = render();
bars = barsOf(nodes);
assert.strictEqual(bars.length, 505, "expanded dense lane bars " + bars.length + " != 505 runs");
bars.forEach(function (b) {
  assert.ok(Number(b.attrs.width) >= 2 && Number(b.attrs.height) >= 2, "expanded bar below 2px: " + b.attrs["data-run-id"]);
});
/* Scenario C: a workstream tab auto-opens one lane and still draws every run. */
Object.keys(expanded).forEach(function (key) { delete expanded[key]; });
runs = [];
for (var m = 0; m < 640; m++) {
  var sp2 = SPECS[m % 4], sl = Math.floor(m / 4) % 40;
  runs.push(mkRun(m, sp2, T0 + 4000 + sl * 2000 + (m % 7), T0 + 4000 + sl * 2000 + (m % 7) + 1500, "done", VERDICTS[m % VERDICTS.length]));
}
state.runs = runs;
state.ws = "WS1";
nodes = render();
bars = barsOf(nodes);
assert.strictEqual(bars.length, 640, "ws-tab bars " + bars.length + " != 640 runs");
assert.ok(textsOf(nodes, "tl-model-label").length > 0, "ws tab opened no per-model lane");
/* Scenario D: a 2 h brush draws only the runs it covers, on the same
   x domain the other strips share, with clipped bars capped. */
Object.keys(expanded).forEach(function (key) { delete expanded[key]; });
var B0 = NOW - 4 * 3600, B1 = NOW - 2 * 3600;
SEL = {from: B0, to: B1};
nodes = render();
bars = barsOf(nodes);
var want = runs.filter(function (r) { return runEnd(r, NOW) >= B0 && r.started_at <= B1; });
assert.strictEqual(bars.length, want.length, "brushed bars " + bars.length + " != covered runs " + want.length);
assert.ok(bars.length > 0 && bars.length < 640, "2 h brush must shrink the strip, got " + bars.length);
var svgs = nodes.filter(function (n) { return n.tag === "svg" && n.attrs["data-strip"] === "timeline"; });
assert.strictEqual(svgs.length, 1, "one timeline svg, got " + svgs.length);
assert.strictEqual(Number(svgs[0].attrs["data-x-from"]), B0, "strip x domain does not match the brush");
assert.strictEqual(Number(svgs[0].attrs["data-x-to"]), B1, "strip x domain does not match the brush");
var clipped = bars.filter(function (b) { return b.attrs["data-clipped"] !== undefined; });
assert.ok(clipped.length > 0, "no clipped bar in a brushed window");
var caps = nodes.filter(function (n) { return n.tag === "rect" && n.attrs.class === "tl-clip"; });
assert.ok(caps.length >= clipped.length, "clipped bars without a visible edge cap");
console.log("timeline every-run proof ok: 640 + 505 + 640 bars, brush clips");
'''
        files = [
            STATIC / "models.js",
            STATIC / "format.js",
            STATIC / "core" / "el.js",
            STATIC / "core" / "unattributed.js",
            STATIC / "core" / "charts.js",
            STATIC / "cards" / "timeline.js",
            STATIC / "cards" / "activity.js",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "tl_every_run.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path)] + [str(p) for p in files],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_project_header_wiring(self):
        """The header follows GET /api/project: the shell loads project.js,
        the h1 keeps its "Agent dashboard" fallback, and the core file
        fetches once per load and on focus with a fixtures path."""
        page = (STATIC / "index.html").read_text()
        self.assertIn('<script src="/static/core/project.js"></script>', page)
        self.assertLess(
            page.index("/static/core/drawer.js"), page.index("/static/core/project.js")
        )
        self.assertIn('<h1 id="title">Agent dashboard</h1>', page)
        self.assertIn(".proj-sub", page)
        core = (STATIC / "core" / "project.js").read_text()
        for needle in (
            "/api/project",
            "../fixtures/project.json",
            "Agent dashboard",
            "/ project tracker",
            "document.title",
            "applyChrome",
            'addEventListener("focus"',
            "MutationObserver",
            "loadProject",
            "applyProject",
        ):
            self.assertIn(needle, core, f"project.js lost {needle!r}")
        api = (STATIC / "core" / "api.js").read_text()
        self.assertIn('"/api/project": "project"', api, "FIXMAP never maps /api/project")
        fixture = STATIC.parent / "fixtures" / "project.json"
        self.assertTrue(fixture.is_file(), "fixtures/project.json is missing")
        self.assertTrue(json.loads(fixture.read_text(encoding="utf-8"))["name"].strip())

    def test_project_header_renders(self):
        """project.js shows the project name with its muted suffix (and the
        name in <title>), falls back without a name, and keeps the name
        across applyChrome view switches."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
const src = fs.readFileSync(process.argv[2], "utf8");
var focusListeners = [];
var titleEl = {children: [], _text: "Agent dashboard",
  appendChild: function (c) { this.children.push(c); return c; }};
Object.defineProperty(titleEl, "textContent", {
  get: function () {
    if (!this.children.length) return this._text;
    return this.children.map(function (c) { return c.text || c.textContent || ""; }).join(" ");
  },
  set: function (v) { this._text = String(v); this.children = []; },
});
Object.defineProperty(titleEl, "innerHTML", {
  get: function () { return ""; },
  set: function () { this.children = []; },
});
function mkTag(tag) {
  return {tag: tag, attrs: {}, children: [], textContent: "",
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    appendChild: function (c) { this.children.push(c); return c; }};
}
global.window = globalThis;
global.addEventListener = function (t, f) { focusListeners.push([t, f]); };
global.document = {
  title: "",
  getElementById: function (id) { return id === "title" ? titleEl : null; },
  createElement: mkTag,
  createTextNode: function (t) { return {text: String(t)}; },
};
global.state = {view: "exec", ws: "all"};
global.FIX = false;
global.applyChrome = function () {};
var mode = "named";
global.fetch = function () {
  var body = mode === "named" ? {name: "Demo project"} : {name: null};
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(body); }});
};
eval(src);
assert.strictEqual(typeof loadProject, "function");
assert.strictEqual(typeof applyProject, "function");
assert.ok(focusListeners.some(function (l) { return l[0] === "focus"; }), "no focus refetch");
setTimeout(function () {
  try {
    assert.ok(titleEl.textContent.indexOf("Demo project") >= 0, "header never shows the name: " + titleEl.textContent);
    assert.ok(titleEl.textContent.indexOf("/ project tracker") >= 0, "header never shows the suffix");
    assert.ok(global.document.title.indexOf("Demo project") >= 0, "title never shows the name");
    mode = "anon";
    loadProject();
    setTimeout(function () {
      try {
        assert.strictEqual(titleEl.textContent, "Agent dashboard", "no-name payload never falls back");
        assert.ok(global.document.title.indexOf("Agent dashboard") >= 0, "title never falls back");
        mode = "named";
        loadProject();
        setTimeout(function () {
          try {
            global.state.view = "plan";
            global.applyChrome();
            assert.ok(global.document.title.indexOf("Plan") === 0, "wrapped chrome lost the view prefix");
            assert.ok(global.document.title.indexOf("Demo project") >= 0, "wrapped chrome lost the name");
            console.log("project header proof ok");
          } catch (e) { console.error("ASSERT: " + e.message); process.exit(1); }
        }, 50);
      } catch (e) { console.error("ASSERT: " + e.message); process.exit(1); }
    }, 50);
  } catch (e) { console.error("ASSERT: " + e.message); process.exit(1); }
}, 50);
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "project_header.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "core" / "project.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("project header proof ok", proc.stdout)


    def test_project_view_routing(self):
        """?view=project|current|planned|archive route beside plan|exec;
        current is the exec alias; unknown views fall back to exec."""
        script = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "globalThis.location={search:'',pathname:'/'};"
            "globalThis.document={getElementById:function(){return null;}};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "const assert=require('assert');"
            "function route(search,path){globalThis.location={search:search,pathname:path};return readRoute();}"
            "assert.deepStrictEqual(route('?view=project','/'),{view:'project',ws:'all'});"
            "assert.deepStrictEqual(route('?view=planned','/'),{view:'planned',ws:'all'});"
            "assert.deepStrictEqual(route('?view=archive','/'),{view:'archive',ws:'all'});"
            "assert.deepStrictEqual(route('?view=current','/'),{view:'exec',ws:'all'});"
            "assert.deepStrictEqual(route('?view=current&ws=ws9','/'),{view:'exec',ws:'WS9'});"
            "assert.deepStrictEqual(route('?view=exec&ws=WS7','/'),{view:'exec',ws:'WS7'});"
            "assert.deepStrictEqual(route('?view=plan&ws=WS7','/'),{view:'plan',ws:'WS7'});"
            "assert.deepStrictEqual(route('?view=bogus','/'),{view:'exec',ws:'all'});"
            "assert.deepStrictEqual(route('','/plan'),{view:'plan',ws:'all'});"
            "assert.deepStrictEqual(route('','/'),{view:'exec',ws:'all'});"
            "assert.strictEqual(isProjectView(),false);"
            "state.view='planned';assert.strictEqual(isProjectView(),true);"
            "state.view='project';assert.strictEqual(isProjectView(),true);"
            "state.view='archive';assert.strictEqual(isProjectView(),true);"
            "state.view='plan';assert.strictEqual(isProjectView(),false);"
            "assert.strictEqual(projectLanding({name:'Demo'},[]),'project');"
            "assert.strictEqual(projectLanding({name:null},[{state:'active'},{state:'active'}]),'exec');"
            "assert.strictEqual(projectLanding(null,[]),'exec');"
            "assert.strictEqual(projectLanding({name:null},[{state:'active'},{state:'planned'}]),'project');"
            "assert.strictEqual(projectLanding({name:null},[{state:'paused'}]),'project');"
            "assert.strictEqual(projectLanding({name:null},[{}]),'exec');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "tabs.js"), str(STATIC / "core" / "state.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_tab_lifecycle_helpers(self):
        """Tabs keep active/paused/done states; absent or unknown reads as
        active, the same default the API uses."""
        script = (
            "const fs=require('fs');"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const T=globalThis.ADT;"
            "const assert=require('assert');"
            "assert.deepStrictEqual(T.TAB_STATES,['active','paused','done']);"
            "assert.strictEqual(T.wsStateOf({state:'planned'}),'planned');"
            "assert.strictEqual(T.wsStateOf({state:'paused'}),'paused');"
            "assert.strictEqual(T.wsStateOf({state:'done'}),'done');"
            "assert.strictEqual(T.wsStateOf({state:'archived'}),'archived');"
            "assert.strictEqual(T.wsStateOf({state:'dropped'}),'dropped');"
            "assert.strictEqual(T.wsStateOf({}),'active');"
            "assert.strictEqual(T.wsStateOf(null),'active');"
            "assert.strictEqual(T.wsStateOf({state:'flying'}),'active');"
            "assert.strictEqual(T.isTabState('active'),true);"
            "assert.strictEqual(T.isTabState('paused'),true);"
            "assert.strictEqual(T.isTabState('done'),true);"
            "assert.strictEqual(T.isTabState('planned'),false);"
            "assert.strictEqual(T.isTabState('archived'),false);"
            "assert.strictEqual(T.isTabState('dropped'),false);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "tabs.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_project_views_are_readonly(self):
        """The dashboard stays read-only except the one write action: the
        only non-GET fetch on the page is apiPost's POST (archive/restore
        with the action header), and the Archive/Restore buttons keep the
        copy-command affordance beside the real button (clipboard with a
        textarea fallback and a toast)."""
        script = inline_scripts()
        plan_js = (STATIC / "plan.js").read_text()
        hits = re.findall(r"method\s*:\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", script, re.I)
        self.assertEqual(hits, ["POST"], f"a second writer appeared beside archive/restore: {hits}")
        api_src = (STATIC / "core" / "api.js").read_text()
        post_sites = re.findall(r"method\s*:\s*[\"']POST[\"']", api_src)
        self.assertEqual(len(post_sites), 1, "the POST must live in apiPost alone")
        self.assertIn("X-AgentDash-Action", api_src)
        self.assertEqual(re.findall(r"method\s*:\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", plan_js, re.I), [])
        for name in ("project-home.js", "planned.js", "archive.js"):
            text = (STATIC / "cards" / name).read_text()
            self.assertNotIn("fetch(", text, f"{name} fetches; project views read state only")
            self.assertNotIn("XMLHttpRequest", text, f"{name} fetches; project views read state only")
        home = (STATIC / "cards" / "project-home.js").read_text()
        self.assertIn("navigator.clipboard", home, "no clipboard copy path")
        self.assertIn("execCommand", home, "no textarea copy fallback")
        self.assertIn("showToast", home, "no confirmation toast")
        self.assertIn("ws archive", home, "Archive button names no command")
        arch = (STATIC / "cards" / "archive.js").read_text()
        self.assertIn("ws restore", arch, "Restore button names no command")

    def test_project_slots_and_registry_scopes(self):
        """Chart slots exist by id on the project views, and the registry
        gains the project/planned scopes with the slot contract."""
        home = (STATIC / "cards" / "project-home.js").read_text()
        planned = (STATIC / "cards" / "planned.js").read_text()
        for slot in ("exec-burnup", "exec-health", "exec-depgraph"):
            self.assertIn(slot, home, f"project home lost the {slot} slot")
        self.assertIn("exec-depgraph", planned, "planned view lost the exec-depgraph slot")
        reg = (STATIC / "cards" / "registry.js").read_text()
        self.assertIn('scope: "project"', reg, "no project registry scope")
        self.assertIn('"planned" entries on the Planned view', reg, "no planned registry scope in the contract")
        self.assertIn("registryFor", reg, "no scope-matching registry helper")
        self.assertIn('registryFor("planned")', planned, "planned view never mounts its scope entries")
        for token in ('"needs"', '"feed"', '"roadmap"', '"roadmap-plan"',
                      'slot: "exec-feed"', 'slot: "exec-roadmap"'):
            self.assertNotIn(token, reg, f"registry.js still registers {token}")
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("projectHomeSpecs", bodies)
        self.assertIn("plannedSpecs", bodies)
        self.assertIn("archiveSpecs", bodies)
        self.assertIn("projectSpecs", bodies)
        self.assertIn("projectHomeSpecs(", bodies["projectSpecs"])
        for name in ("project-phases", "project-tiles", "project-current", "project-done", "planned-ready", "archive-shelf"):
            self.assertIn(name, script, f"the {name} section never renders")

    def test_removed_cards_are_gone(self):
        """The dropped What-changed, roadmap, and Needs-you cards leave
        no file, tag, registry line, slot, or heading behind."""
        for name in ("feed.js", "feed.css", "roadmap.js", "roadmap.css", "needs.js"):
            self.assertFalse((STATIC / "cards" / name).exists(), f"static/cards/{name} is back")
        page = (STATIC / "index.html").read_text()
        for tag in ("/static/cards/feed.js", "/static/cards/feed.css",
                    "/static/cards/roadmap.js", "/static/cards/roadmap.css",
                    "/static/cards/needs.js"):
            self.assertNotIn(tag, page, f"index.html still loads {tag}")
        for word in ("What changed", "Project roadmap", "Needs you"):
            self.assertNotIn(word, page, f"index.html still names {word!r}")
        home = (STATIC / "cards" / "project-home.js").read_text()
        planned = (STATIC / "cards" / "planned.js").read_text()
        for token in ("exec-feed", "exec-roadmap", "needsCard", "project-done-needs"):
            self.assertNotIn(token, home, f"project-home.js still names {token}")
            self.assertNotIn(token, planned, f"planned.js still names {token}")

    def test_project_pure_helpers_on_fixtures(self):
        """The project-view data helpers agree with the generated fixtures:
        landing, report basenames, gate staleness, start readiness, the
        gated-but-live scan, and archive ordering."""
        import json as _json

        fx = Path(__file__).resolve().parent.parent / "fixtures"
        project = _json.loads((fx / "project.json").read_text())
        wss = _json.loads((fx / "workstreams.json").read_text())["workstreams"]
        wss_all = _json.loads((fx / "workstreams-all.json").read_text())["workstreams"]
        archive = _json.loads((fx / "archive.json").read_text())
        plan_ws9 = _json.loads((fx / "plan-WS9.json").read_text())
        approvals = _json.loads((fx / "approvals.json").read_text())["approvals"]
        harness = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "globalThis.location={search:'',pathname:'/'};"
            "globalThis.document={getElementById:function(){return null;}};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "eval(fs.readFileSync(process.argv[3],'utf8'));"
            "eval(fs.readFileSync(process.argv[4],'utf8'));"
            "eval(fs.readFileSync(process.argv[5],'utf8'));"
            "eval(fs.readFileSync(process.argv[6],'utf8'));"
            "eval(fs.readFileSync(process.argv[7],'utf8'));"
            "eval(fs.readFileSync(process.argv[8],'utf8'));"
            "const assert=require('assert');"
            "var PROJECT=" + _json.dumps(project) + ";"
            "var WSS=" + _json.dumps(wss) + ";"
            "var ARCHIVE=" + _json.dumps(archive) + ";"
            "var PLAN9=" + _json.dumps(plan_ws9) + ";"
            "var APPROVALS=" + _json.dumps(approvals) + ";"
            "var NOW=1761999960;"
            "assert.strictEqual(projectLanding(PROJECT,WSS),'project');"
            "assert.strictEqual(projectLanding({name:null},WSS),'project');"
            "assert.strictEqual(reportFor('WS11',ARCHIVE),'2025-10-30-closing.md');"
            "assert.strictEqual(reportFor('ws11',ARCHIVE),'2025-10-30-closing.md');"
            "assert.strictEqual(reportFor('WS9',ARCHIVE),null);"
            "var gate9=PLAN9.groups[0].tasks[0];"
            "assert.strictEqual(gate9.kind,'gate');"
            "assert.strictEqual(gateStale(gate9,NOW),true);"
            "assert.strictEqual(gateStale({fresh_hours:24,confirmed_at:'2025-11-01T12:00:00Z'},NOW),false);"
            "assert.strictEqual(gateStale({confirmed_at:'2020-01-01T00:00:00Z'},NOW),false);"
            "assert.strictEqual(gateStale({fresh_hours:0,confirmed_at:'2020-01-01T00:00:00Z'},NOW),false);"
            "var ws9=WSS.filter(function(w){return w.ws==='WS9';})[0];"
            "var states={};"
            + _json.dumps(wss_all) + ".forEach(function(w){states[w.ws]=w.state;});"
            "var r=readinessOf(ws9,PLAN9,APPROVALS,states,NOW);"
            "assert.strictEqual(r.ready,false);"
            "assert.ok(r.reason.indexOf('WS6')>=0,'reason names the blocking workstream: '+r.reason);"
            "assert.strictEqual(r.pending,2);"
            "assert.strictEqual(r.gates,1);"
            "assert.strictEqual(r.staleGates,1);"
            "assert.strictEqual(r.done,0);"
            "assert.ok(r.waitText.indexOf('WS6')>=0);"
            "var okRow={ws:'WSX',depends_on:[],progress:{done:1,total:1}};"
            "var rok=readinessOf(okRow,{groups:[]},[],{WSX:'planned'},NOW);"
            "assert.strictEqual(rok.ready,true);"
            "assert.strictEqual(gatedLive([],{},NOW).length,0);"
            "var held=gatedLive([{ws:'WS6',name:'Cutover',progress:{done:2,total:78,pct:3}}],"
            "{WS6:{groups:[{tasks:[{id:'g',kind:'gate',status:'todo',signal:'owner-signoff gate is approved'}]}]}},NOW);"
            "assert.strictEqual(held.length,1);"
            "assert.strictEqual(held[0].ws,'WS6');"
            "var sorted=sortArchive(ARCHIVE.archive.concat([{id:'WS0',archived_at:null}]));"
            "assert.strictEqual(sorted[0].id,'WS11');"
            "assert.strictEqual(sorted[sorted.length-1].id,'WS0');"
            "assert.strictEqual(durStr(28*86400),'28d');"
            "assert.strictEqual(durStr(90),'1m 30s');"
            "assert.strictEqual(durStr(null),'\u2013');"
        )
        proc = subprocess.run(
            ["node", "-e", harness,
             str(STATIC / "tabs.js"), str(STATIC / "format.js"), str(STATIC / "core" / "el.js"),
             str(STATIC / "core" / "state.js"), str(STATIC / "cards" / "registry.js"),
             str(STATIC / "cards" / "planned.js"), str(STATIC / "cards" / "project-home.js"),
             str(STATIC / "cards" / "archive.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def _keyof_args(self, text):
        """Yield each keyOf(...) argument, honouring nesting and strings."""
        out = []
        for m in re.finditer(r"keyOf\(", text):
            i = m.end()
            depth = 1
            quote = None
            escaped = False
            while i < len(text) and depth:
                ch = text[i]
                if quote:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == quote:
                        quote = None
                elif ch in ("'", '"', "`"):
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
            self.assertEqual(depth, 0, f"keyOf( at offset {m.start()} never closes")
            out.append(text[m.end():i - 1])
        return out

    def test_overview_plan_load_terminates(self):
        """The overview settles on tiny and brand-new dashboards: drawMain
        loads the plan sums once per workstream set (a done-marker keyed by
        the set), and the follow-up redraw happens only when the load
        actually added plan data. An empty planSum (no plans yet) never
        re-arms another load, so the promise chain terminates."""
        page = (STATIC / "index.html").read_text()
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("drawMain", bodies)
        draw = bodies["drawMain"]
        self.assertNotIn(
            "!Object.keys(planSum).length) loadPlanSums",
            page,
            "drawMain re-arms the plan load while planSum is empty: "
            "a plan-less bundle set re-renders forever",
        )
        for marker in ("__adPlanSumsKey", "planBefore", "loadPlanSums"):
            self.assertIn(marker, draw, f"drawMain lost its one-shot plan-load guard: {marker}")
        self.assertIn("delete secCache.program", draw)
        self.assertLess(
            draw.index("planBefore"),
            draw.index("delete secCache.program"),
            "the section-cache delete is not gated on the load adding plan data",
        )
        cards = (STATIC / "cards" / "ws-cards.js").read_text()
        load = dict(functions(cards))["loadPlanSums"]
        self.assertIn(
            "planSumInflight", load, "concurrent plan loads stack fetches instead of sharing one"
        )
        self.assertGreaterEqual(
            load.count("planSumInflight = null"),
            2,
            "an empty/failed plan load never settles its guard",
        )

    def test_plan_load_settles_empty(self):
        """loadPlanSums resolves with an empty planSum (no plans yet) and
        shares one fetch across concurrent calls, so the overview's
        one-shot guard can rely on it settling instead of looping."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
global.state = {wss: []};
global.shown = function (list) { return (list || []).filter(function (w) { return w.plan; }); };
var apiCalls = 0, failNext = false;
global.api = function () {
  apiCalls++;
  if (failNext) return Promise.reject(new Error("404"));
  return Promise.resolve(null);
};
global.renderTabs = function () {};
eval(fs.readFileSync(process.argv[2], "utf8"));
assert.strictEqual(typeof loadPlanSums, "function");
loadPlanSums().then(function () {
  assert.deepStrictEqual(Object.keys(planSum), [], "an empty bundle set must keep planSum empty");
  state.wss = [{ws: "WS1", plan: true}, {ws: "WS2", plan: true}];
  apiCalls = 0;
  return Promise.all([loadPlanSums(), loadPlanSums()]);
}).then(function () {
  assert.strictEqual(apiCalls, 2, "concurrent loads fetched " + apiCalls + " plans, want one shared load of 2");
  assert.strictEqual(Object.keys(planSum).length, 2);
  failNext = true;
  state.wss = [{ws: "WS9", plan: true}];
  delete planSum.WS1; delete planSum.WS2;
  return loadPlanSums();
}).then(function () {
  assert.deepStrictEqual(Object.keys(planSum), [], "failed plan fetches must still settle empty");
  console.log("plan load proof ok");
}).catch(function (e) { console.error("ASSERT: " + ((e && e.stack) || e)); process.exit(1); });
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "plan_load_settles.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "cards" / "ws-cards.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("plan load proof ok", proc.stdout)

    def test_section_keys_have_no_volatile_values(self):
        """Section keys decide rebuilds: a key holding an unbucketed clock
        read, Math.random() or new Date() changes on every render and the
        page never settles. The one allowed clock read is the minute bucket
        behind the live charts."""
        checked = 0
        for name in (
            "cards/registry.js",
            "cards/project-home.js",
            "cards/planned.js",
            "cards/archive.js",
        ):
            text = (STATIC / name).read_text()
            for arg in self._keyof_args(text):
                checked += 1
                self.assertNotIn(
                    "Math.random(",
                    arg,
                    f"{name} keys on randomness; the section rebuilds every render",
                )
                self.assertNotIn(
                    "new Date(",
                    arg,
                    f"{name} keys on a fresh date; the section rebuilds every render",
                )
                for m in re.finditer(r"Date\.now\(\)", arg):
                    before = arg[max(0, m.start() - 11):m.start()]
                    self.assertTrue(
                        before.endswith("Math.floor("),
                        f"{name} keys on an unbucketed clock read; bucket it like the live charts",
                    )
        self.assertGreaterEqual(checked, 10, "the key scan found no keyOf( calls to pin")

    def test_applysections_is_idempotent(self):
        """applySections rebuilds only sections whose key changed: a repeat
        render with identical keys calls no render fn, a changed key rebuilds
        only its section, and reorder/removal moves or drops nodes without
        re-rendering."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
global.state = {ws: "all", groupBy: "model"};
global.ADC = {currentKey: function () { return "k"; }, fresh: function () { return true; }, TTL_MS: 30000};
function mockNode(tag) {
  return {tag: tag, attrs: {}, kids: [], parentNode: null, style: {},
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    appendChild: function (c) { c.parentNode = this; this.kids.push(c); return c; }};
}
global.el = function (tag, attrs, kids) {
  var n = mockNode(tag);
  n.attrs = attrs || {};
  (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
  return n;
};
global.document = {getElementById: function () { return null; }};
eval(fs.readFileSync(process.argv[2], "utf8"));
assert.strictEqual(typeof applySections, "function");
assert.strictEqual(typeof keyOf, "function");
assert.strictEqual(keyOf({a: 1, b: [1, 2]}), keyOf({a: 1, b: [1, 2]}), "keyOf is not stable");
assert.notStrictEqual(keyOf([1, 2]), keyOf([1, 3]), "keyOf misses changes");
function mockMain() {
  var m = {children: [],
    insertBefore: function (node, ref) {
      if (node.parentNode === this) this.children = this.children.filter(function (c) { return c !== node; });
      var at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
      node.parentNode = this; return node;
    },
    replaceChild: function (node, old) {
      var at = this.children.indexOf(old);
      assert.ok(at >= 0, "replaceChild of a missing node");
      this.children[at] = node; node.parentNode = this; old.parentNode = null; return old;
    },
    removeChild: function (node) {
      var at = this.children.indexOf(node);
      assert.ok(at >= 0, "removeChild of a missing node");
      this.children.splice(at, 1); node.parentNode = null; return node;
    }};
  Object.defineProperty(m, "lastChild", {configurable: true, get: function () { return this.children[this.children.length - 1] || null; }});
  return m;
}
var calls = {};
function spec(name, key) {
  return [name, key, function () {
    calls[name] = (calls[name] || 0) + 1;
    return mockNode("inner-" + name);
  }, ""];
}
var m = mockMain();
applySections(m, [spec("a", "k1"), spec("b", "k2")]);
assert.deepStrictEqual(calls, {a: 1, b: 1});
assert.strictEqual(m.children.length, 2);
var aNode = m.children[0], bNode = m.children[1];
applySections(m, [spec("a", "k1"), spec("b", "k2")]);
assert.deepStrictEqual(calls, {a: 1, b: 1}, "identical keys re-rendered");
assert.strictEqual(m.children[0], aNode);
assert.strictEqual(m.children[1], bNode);
applySections(m, [spec("a", "k1"), spec("b", "k3")]);
assert.deepStrictEqual(calls, {a: 1, b: 2}, "a changed key must rebuild only its section");
assert.strictEqual(m.children[0], aNode);
assert.notStrictEqual(m.children[1], bNode);
bNode = m.children[1];
applySections(m, [spec("b", "k3"), spec("a", "k1")]);
assert.deepStrictEqual(calls, {a: 1, b: 2}, "reorder must not re-render");
assert.strictEqual(m.children[0], bNode);
assert.strictEqual(m.children[1], aNode);
applySections(m, [spec("b", "k3")]);
assert.deepStrictEqual(calls, {a: 1, b: 2}, "removal must not re-render");
assert.strictEqual(m.children.length, 1);
assert.strictEqual(m.children[0], bNode);
console.log("applySections proof ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "applysections_idempotent.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "core" / "cache.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("applySections proof ok", proc.stdout)

    def test_render_counter_and_title_guard(self):
        """The settle check reads window.__adRenders (one increment per
        drawMain), and the project title observer converges instead of
        re-applying its own writes."""
        page = (STATIC / "index.html").read_text()
        bodies = dict(functions(inline_scripts()))
        self.assertIn("window.__adRenders", page)
        self.assertIn(
            "__adRenders++", bodies["drawMain"], "drawMain never counts renders; the settle check is blind"
        )
        core = (STATIC / "core" / "project.js").read_text()
        self.assertIn("MutationObserver", core)
        self.assertIn("busy", core, "the title observer lost its re-entrancy guard")
        self.assertIn(
            ".proj-name",
            core,
            "the observer no longer skips a header that already shows the name",
        )

    def _extract_braced(self, text, start_marker):
        """Source of one `function name(...) {...}` starting at start_marker."""
        start = text.index(start_marker)
        brace = text.index("{", start)
        depth = 0
        for pos in range(brace, len(text)):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
                if depth == 0:
                    return text[start : pos + 1]
        raise AssertionError(f"{start_marker} body never closes")

    def test_snapshot_signature_ignores_store_clocks(self):
        """A stream tick that only moves the store clocks (seq, data_seq,
        last_data_ts, now) renders identically: snapSig is equal, so the
        tick skips drawMain and the render counter plateaus instead of
        climbing on every 2 s tick. Any real change (counts, totals,
        usage, run lists) still changes the signature and draws."""
        api_src = (STATIC / "core" / "api.js").read_text()
        clocks_start = api_src.index("var SNAP_CLOCKS =")
        clocks_src = api_src[clocks_start : api_src.index("};", clocks_start) + 2]
        for clock in ("now", "seq", "data_seq", "last_data_ts"):
            self.assertIn(clock, clocks_src, f"snapSig no longer strips the {clock} clock")
        run_start = api_src.index("var RUN_CLOCKS =")
        run_src = api_src[run_start : api_src.index("};", run_start) + 2]
        for tele in ("rss_kb", "elapsed_s", "out_bytes", "err_bytes", "out_growing"):
            self.assertIn(tele, run_src, f"project ticks no longer strip sample telemetry {tele}")
        sig_src = self._extract_braced(api_src, "function snapSig(j, extra)")
        harness = (
            "const assert = require('assert');\n"
            + clocks_src
            + "\n"
            + sig_src
            + "\n"
            "var base = {now: 100, seq: 5, data_seq: 7, last_data_ts: 90,"
            " counts: {finished_in_range: 3}, totals: {tokens: {input: 1, output: 2}, cost_usd: 0.5},"
            " usage: [{input: 1, output: 2}], running: [], recent: []};\n"
            "var clocksOnly = {now: 200, seq: 99, data_seq: 100, last_data_ts: 95,"
            " counts: {finished_in_range: 3}, totals: {tokens: {input: 1, output: 2}, cost_usd: 0.5},"
            " usage: [{input: 1, output: 2}], running: [], recent: []};\n"
            "var changed = {now: 200, seq: 99, data_seq: 100, last_data_ts: 95,"
            " counts: {finished_in_range: 4}, totals: {tokens: {input: 1, output: 2}, cost_usd: 0.5},"
            " usage: [{input: 1, output: 2}], running: [], recent: []};\n"
            "assert.strictEqual(snapSig(clocksOnly), snapSig(base),"
            " 'a tick that only moved the store clocks changed the render signature: ticks redraw forever');\n"
            "assert.notStrictEqual(snapSig(changed), snapSig(base), 'a real change lost its redraw');\n"
            "var telemetry = {now: 200, seq: 99, data_seq: 100, last_data_ts: 95,"
            " counts: {finished_in_range: 3}, totals: {tokens: {input: 1, output: 2}, cost_usd: 0.5},"
            " usage: [{input: 1, output: 2}],"
            " running: [{run_id: 'r1', status: 'running', rss_kb: 371408, elapsed_s: 61,"
            " out_bytes: 100, err_bytes: 0, out_growing: false}], recent: []};\n"
            "var telemetryMoved = {now: 201, seq: 100, data_seq: 100, last_data_ts: 95,"
            " counts: {finished_in_range: 3}, totals: {tokens: {input: 1, output: 2}, cost_usd: 0.5},"
            " usage: [{input: 1, output: 2}],"
            " running: [{run_id: 'r1', status: 'running', rss_kb: 371999, elapsed_s: 63,"
            " out_bytes: 140, err_bytes: 0, out_growing: true}], recent: []};\n"
            "assert.notStrictEqual(snapSig(telemetryMoved), snapSig(telemetry),"
            " 'the exec path lost its live run telemetry');\n"
            "var STRIP = {rss_kb: 1, elapsed_s: 1, out_bytes: 1, err_bytes: 1, out_growing: 1};\n"
            "assert.strictEqual(snapSig(telemetryMoved, STRIP), snapSig(telemetry, STRIP),"
            " 'project ticks redraw on sample telemetry: the home view never idles');\n"
            "assert.strictEqual(snapSig(null), null);\n"
            "assert.strictEqual(snapSig(42), null);\n"
            "console.log('snapSig proof ok');\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "snapsig_clocks.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", "-e", harness_path.read_text(encoding="utf-8")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("snapSig proof ok", proc.stdout)

    def test_since_is_quantized_to_the_server_grid(self):
        """The page's window start moves on the same 30 s grid the server
        keys (and tags) on. A per-second since makes every tick and sweep
        URL novel, so the conditional layer never matches: no If-None-Match
        is ever sent and every request refetches and recomputes."""
        state_src = (STATIC / "core" / "state.js").read_text()
        start = state_src.index("function since()")
        since_src = state_src[start : state_src.index("\n", start)]
        self.assertIn("WINDOW_HOURS", since_src, "since() no longer goes through the 24 h window")
        self.assertIn("30000", since_src, "since() lost its 30 s quantum: every tick URL is novel again")
        harness = (
            "const assert = require('assert');\n"
            "var WINDOW_HOURS = 24;\n" + since_src + "\n"
            "var realNow = Date.now;\n"
            "var t = 1792248001000;\n"
            "Date.now = function () { return t; };\n"
            "var a = since();\n"
            "assert.strictEqual(a % 30, 0, 'since() is not on the 30 s grid: ' + a);\n"
            "t += 15000;\n"
            "var b = since();\n"
            "assert.strictEqual(b, a, 'since() moved inside one 30 s bucket: the URL churns every tick');\n"
            "t += 20000;\n"
            "var c = since();\n"
            "assert.strictEqual(c, a + 30, 'since() did not advance to the next bucket');\n"
            "Date.now = realNow;\n"
            "console.log('since proof ok');\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "since_grid.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", "-e", harness_path.read_text(encoding="utf-8")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("since proof ok", proc.stdout)

    def test_refresh_timers_do_not_stack(self):
        """Poll timers never stack another burst on a running one: ticks
        take one snapshot fetch at a time and yield to a full refresh,
        warmups yield to it at every step, and the slow interval skips
        while one is in flight. Otherwise a slow server queues a fetch
        per tick behind the stuck burst and the stall feeds itself."""
        script = inline_scripts()
        bodies = dict(functions(script))
        for name in ("refreshAll", "refreshProject"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            self.assertIn("refreshActive++", bodies[name], f"{name} never marks its burst in flight")
            self.assertIn("finished", bodies[name], f"{name} never settles its in-flight mark exactly once")
        snap = bodies["refreshSnapshot"]
        self.assertIn("snapInflight", snap, "ticks no longer take one snapshot fetch at a time")
        self.assertIn("refreshBusy()", snap, "a tick still piles onto a running full refresh")
        self.assertIn("refreshBusy()", bodies["stream"], "stream ticks ignore a running refresh")
        self.assertIn("refreshBusy()", bodies["warmTabs"], "warmups still compete with the foreground burst")
        proj = bodies["refreshProject"]
        self.assertIn("projSig", proj, "project ticks lost their skip-when-unchanged gate")
        self.assertIn("quiet", proj, "tick-driven project refreshes no longer pass quiet")
        page = (STATIC / "index.html").read_text()
        self.assertIn("refreshBusy()", page, "the slow interval still stacks on a running refresh")

    def test_conditional_identity_ignores_the_poll_clock(self):
        """Server identity tags (and the snapshot result cache) key on the
        data version, never the poll clock: store.seq advances on every
        poll (process samples, counts) while no served byte changes, so
        tagging it answers 200 to every tick and the conditional layer
        (and the result cache) can never engage on a live server."""
        dash = STATIC.parent / "dash"

        def ident_of(path, method, marker):
            text = path.read_text()
            head = re.search(rf"^(\s*)def {method}\(", text, re.M)
            self.assertIsNotNone(head, f"{method} is gone from {path.name}")
            indent = head.group(1)
            start = head.start()
            try:
                tail = text.index(f"\n{indent}def ", start + 1)
            except ValueError:
                tail = len(text)
            body = text[start:tail]
            hit = body.index(marker)
            depth = 0
            for pos in range(body.index("(", hit), len(body)):
                if body[pos] == "(":
                    depth += 1
                elif body[pos] == ")":
                    depth -= 1
                    if depth == 0:
                        return body[hit : pos + 1]
            raise AssertionError(f"{method} ident never closes")

        server_src = dash / "server.py"
        for method in ("_snapshot", "_runs", "_concurrency"):
            ident = ident_of(server_src, method, "ident = (").replace(
                "store.data_seq", "STORE_DATA_SEQ"
            )
            self.assertNotIn(
                "store.seq",
                ident,
                f"{method} tags the poll clock: every tick 200s and no 304 ever fires",
            )
        key_src = ident_of(
            dash / "store" / "queries.py", "workstreams_key", "return ("
        ).replace("self._data_seq", "SELF_DATA_SEQ")
        self.assertNotIn(
            "self._seq",
            key_src,
            "workstreams_key tags the poll clock: every tick recomputes and no 304 ever fires",
        )
        summarize = (dash / "store" / "summarize.py").read_text()
        self.assertNotIn(
            "hit[1] == self._seq",
            summarize,
            "snapshot_cached gates on the poll clock: the result cache never hits on a live server",
        )
        self.assertIn(
            "hit[0] == self._data_seq",
            summarize,
            "snapshot_cached lost its data-version gate",
        )


    def test_decisions_has_no_doctor_words(self):
        """The decisions card names no doctor output: no health/doctor
        words in the file or stylesheet, no motion in the stylesheet, and
        no raw fetch or non-GET method (it loads through api())."""
        for name in ("decisions.js", "decisions.css"):
            text = (STATIC / "cards" / name).read_text()
            for word in ("doctor", "health"):
                self.assertNotIn(word, text.lower(), f"{name} carries a {word!r} word")
        text = (STATIC / "cards" / "decisions.css").read_text()
        for prop in ("animation", "transition"):
            self.assertNotIn(prop, text, f"decisions.css animates; reduced motion stays still")
        text = (STATIC / "cards" / "decisions.js").read_text()
        self.assertNotIn("fetch(", text, "decisions.js fetches; project cards read api()/state only")
        self.assertNotIn("XMLHttpRequest", text, "decisions.js fetches; project cards read api()/state only")
        hits = re.findall(r"method\s*:\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", text, re.I)
        self.assertEqual(hits, [], f"decisions.js writes with a non-GET method: {hits[:2]}")
        reg = (STATIC / "cards" / "registry.js").read_text()
        self.assertIn('scope: "project"', reg, "no project registry scope")
        self.assertIn('"decisions"', reg, "decisions.js is not registered")
        page = (STATIC / "index.html").read_text()
        for tag in (
            '<script src="/static/cards/decisions.js">',
            '<link rel="stylesheet" href="/static/cards/decisions.css">',
        ):
            self.assertIn(tag, page, f"index.html lost {tag}")
    def test_decisions_card_on_fixtures(self):
        """The decisions card pills, filters, and link rules hold; and the
        decisions spec hides when its source is absent."""
        import json as _json
        fx = Path(__file__).resolve().parent.parent / "fixtures"
        decisions = _json.loads((fx / "decisions.json").read_text())
        harness = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "globalThis.location={search:'',pathname:'/'};"
            "function fakeNode(tag){"
            " var n={tag:tag,attrs:{},kids:[],textContent:'',_on:{},"
            "  setAttribute:function(k,v){this.attrs[k]=v;},"
            "  appendChild:function(c){this.kids.push(c);return c;},"
            "  addEventListener:function(t,f){this._on[t]=f;}};"
            " return n;}"
            "globalThis.document={createElement:fakeNode,getElementById:function(){return null;},querySelector:function(){return null;}};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "eval(fs.readFileSync(process.argv[3],'utf8'));"
            "eval(fs.readFileSync(process.argv[4],'utf8'));"
            "const assert=require('assert');"
            "globalThis.drawMain=function(){};"
            "globalThis.secCache={};"
            "globalThis.keyOf=function(v){return JSON.stringify(v);};"
            "globalThis.api=function(){return {then:function(){return {catch:function(){}};}}};"
            "var state={view:'project',ws:'all',seq:7,"
            " decisions:null,wss:[{ws:'WS9',name:'Relay'}],wssAll:[{ws:'WS9',name:'Relay'},{ws:'WS7',name:'Launch'}]};"
            "function byClass(node,token,out){out=out||[];"
            " (node.kids||[]).forEach(function(k){"
            "  if(k&&k.attrs&&(' '+k.attrs.class+' ').indexOf(' '+token+' ')>=0)out.push(k);"
            "  byClass(k,token,out);});return out;}"
            "function texts(nodes){return nodes.map(function(n){return n.textContent;});}"
            "assert.strictEqual(decisionPill('approved').text,'\\u2713 Approved');"
            "assert.strictEqual(decisionPill('denied').text,'\\u2717 Denied');"
            "assert.strictEqual(decisionPill('pending').text,'\\u25CB Pending');"
            "assert.ok(decisionPill('mystery').text.indexOf('mystery')>=0);"
            "var DEC=" + _json.dumps(decisions) + ";"
            "var sorted=sortDecisions(DEC.decisions);"
            "assert.strictEqual(sorted[0].id,'start-relay');"
            "assert.strictEqual(sorted[sorted.length-1].id,'retire-dup-client');"
            "assert.strictEqual(decisionsSpec(state,{}),null);"
            "state.decisions={decisions:[],counts:{},source:{present:false}};"
            "assert.strictEqual(decisionsSpec(state,{}),null);"
            "state.decisions={decisions:[],counts:{},source:{present:true}};"
            "assert.strictEqual(decisionsSpec(state,{}),null);"
            "state.decisions=" + _json.dumps(decisions) + ";"
            "var dspec=decisionsSpec(state,{});"
            "assert.ok(Array.isArray(dspec)&&typeof dspec[2]==='function');"
            "var dcard=decisionsCard();"
            "assert.strictEqual(byClass(dcard,'dc-row').length,4);"
            "var pills=texts(byClass(dcard,'dc-pill'));"
            "assert.ok(pills.some(function(t){return t.indexOf('\\u2713')===0;}),'approved pill missing glyph');"
            "assert.ok(pills.some(function(t){return t.indexOf('\\u2717')===0;}),'denied pill missing glyph');"
            "assert.ok(pills.some(function(t){return t.indexOf('\\u25CB')===0;}),'pending pill missing glyph');"
            "assert.strictEqual(byClass(dcard,'dc-filter').length,4);"
            "var links=byClass(dcard,'dc-link');"
            "assert.strictEqual(links.length,1);"
            "assert.strictEqual(links[0].tag,'a');"
            "assert.strictEqual(byClass(dcard,'dc-linktext').length,1);"
            "assert.strictEqual(byClass(dcard,'disclose-btn').length,0);"
            "var many=[];for(var i=0;i<13;i++)many.push({id:'d'+i,title:'Row '+i,status:'approved',owner:'o',date:'2026-04-02',workstream:'WS9',area:'a'});"
            "state.decisions={decisions:many,counts:{approved:13},source:{present:true}};"
            "var big=decisionsCard();"
            "assert.strictEqual(byClass(big,'dc-row').length,5);"
            "var dbtn=byClass(big,'disclose-btn');"
            "assert.strictEqual(dbtn.length,1);"
            "assert.strictEqual(dbtn[0].textContent,'Show all 13 decisions');"
            "assert.strictEqual(dbtn[0].attrs['aria-expanded'],'false');"
            "assert.strictEqual(dbtn[0].attrs['aria-controls'],'dec-list');"
        )
        proc = subprocess.run(
            ["node", "-e", harness,
             str(STATIC / "format.js"), str(STATIC / "core" / "el.js"),
             str(STATIC / "core" / "disclosure.js"),
             str(STATIC / "cards" / "decisions.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
    def test_depgraph_layout_has_no_overlaps(self):
        """The dependency graph layout is overlap-free at scale: a
        synthetic 30-node graph (chains, branches, one cycle) lays out
        with disjoint node boxes in wide and stacked-narrow modes, stays
        deterministic, hides archived/dropped unless asked, and traces
        chains with glyph-plus-word marks."""
        script = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const assert=require('assert');"
            "for(const n of ['depLayout','depUpstream','depDownstream','depStateMark','depEdgeMark','depShort','depGraphSpec']){"
            "assert.strictEqual(typeof globalThis[n],'function',n+' is not exported');}"
            "assert.deepStrictEqual(depStateMark('done'),{text:'\\u2713 Done',cls:'done'});"
            "assert.deepStrictEqual(depStateMark('planned'),{text:'\\u25CB Planned',cls:'planned'});"
            "assert.deepStrictEqual(depStateMark('active'),{text:'\\u25CF Active',cls:'active'});"
            "assert.deepStrictEqual(depEdgeMark('met'),{text:'\\u2713 met',cls:'e-met'});"
            "assert.deepStrictEqual(depEdgeMark('waiting'),{text:'\\u25CB waiting',cls:'e-waiting'});"
            "assert.deepStrictEqual(depEdgeMark('stale'),{text:'\\u26A0 stale',cls:'e-stale'});"
            "assert.strictEqual(depShort('A very long workstream title here',10),'A very lo\\u2026');"
            "var nodes=[],edges=[];"
            "for(var i=1;i<=30;i++){"
            "nodes.push({id:'WS'+i,title:'Workstream '+i,state:i%7===0?'archived':(i%11===0?'dropped':(i%3===0?'planned':'active')),depth:0});}"
            "for(var j=1;j<30;j++){edges.push({from:'WS'+j,to:'WS'+(j+1),kind:'dependency',status:'waiting',condition:null});}"
            "edges.push({from:'WS3',to:'WS12',kind:'gate',status:'stale',condition:'WS3 says go'});"
            "edges.push({from:'WS5',to:'WS15',kind:'moved',status:'met',condition:'handed over'});"
            "edges.push({from:'WS30',to:'WS28',kind:'dependency',status:'waiting',condition:null});"
            "var depths={};"
            "nodes.forEach(function(n){"
            "var incoming=edges.filter(function(e){return e.to===n.id;});"
            "depths[n.id]=incoming.length?1+Math.max.apply(null,incoming.map(function(e){return depths[e.from]||0;})):0;"
            "n.depth=depths[n.id];});"
            "var graph={nodes:nodes,edges:edges};"
            "function disjoint(placed){"
            "for(var a=0;a<placed.length;a++)for(var b=a+1;b<placed.length;b++){"
            "var A=placed[a],B=placed[b];"
            "var apart=(A.x+A.w<=B.x)||(B.x+B.w<=A.x)||(A.y+A.h<=B.y)||(B.y+B.h<=A.y);"
            "assert.ok(apart,'overlap: '+A.id+' vs '+B.id);}}"
            "var wide=depLayout(graph,{width:1200});"
            "assert.strictEqual(wide.narrow,false);"
            "disjoint(wide.nodes);"
            "var narrow=depLayout(graph,{width:360,showArch:true});"
            "assert.strictEqual(narrow.narrow,true);"
            "disjoint(narrow.nodes);"
            "assert.ok(narrow.nodes.length>wide.nodes.length,'showArch must reveal hidden nodes');"
            "wide.nodes.forEach(function(n){assert.ok(!/archived|dropped/.test(n.id)&&true);});"
            "var again=depLayout(graph,{width:1200});"
            "assert.deepStrictEqual(again,wide,'layout is not deterministic');"
            "var cols={};wide.nodes.forEach(function(n){cols[n.depth]=(cols[n.depth]||0)+1;});"
            "assert.strictEqual(wide.cols,Object.keys(cols).length);"
            "var up=depUpstream(edges,'WS15');"
            "assert.ok(up['WS5']&&up['WS1'],'upstream misses the chain: '+JSON.stringify(up));"
            "assert.ok(!up['WS15'],'upstream holds itself');"
            "var down=depDownstream(edges,'WS1');"
            "assert.ok(down['WS15'],'downstream misses the chain');"
            "assert.ok(!down['WS1'],'downstream holds itself');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "cards" / "depgraph.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
    def test_depgraph_card_contract(self):
        """The dependency card keeps its integration points: one file plus
        one registry line per scope over the same spec, the exec-depgraph
        slot on both views, a legend, hover/pin handlers with tooltips, a
        "show archived" toggle, compact numbers, and no doctor UI."""
        card = (STATIC / "cards" / "depgraph.js").read_text()
        for marker in (
            "depGraphSpec",
            "depLayout",
            "dep-legend",
            "show archived",
            "aria-pressed",
            "mouseenter",
            "mousemove",
            "mouseleave",
            "showTip",
            "hideTip",
            "depPin",
            "data-diagram",
            "fmtN(",
            "depUpstream",
            "depDownstream",
            "openPlan",
        ):
            self.assertIn(marker, card, f"depgraph.js lost the card branch: {marker}")
        for bad in (".toFixed(", "toLocaleString()", '"dagster"', "'dagster'", "Infinity", "doctor", "health"):
            self.assertNotIn(bad, card, f"depgraph.js must not contain {bad}")
        reg = (STATIC / "cards" / "registry.js").read_text()
        self.assertIn('"depgraph"', reg, "depgraph is not registered")
        self.assertIn('scope: "planned"', reg, "depgraph misses the planned scope")
        self.assertIn('slot: "exec-depgraph"', reg, "depgraph misses its slot")
        page = (STATIC / "index.html").read_text()
        self.assertIn("/static/cards/depgraph.css", page, "index.html never loads the card CSS")
        self.assertIn("/static/cards/depgraph.js", page, "index.html never loads the card script")
        css = (STATIC / "cards" / "depgraph.css").read_text()
        for marker in (
            ".dep-edge.e-met",
            ".dep-edge.e-waiting",
            ".dep-edge.e-stale",
            ".hl-up",
            ".hl-down",
            "@media (max-width:700px)",
            "prefers-reduced-motion",
        ):
            self.assertIn(marker, css, f"depgraph.css lost the style rule: {marker}")

    def test_phases_card_contract(self):
        """The planning-versus-execution card keeps its integration points: one file
        plus one registry line per scope over the phase specs, the
        exec-phases slot on the Project home right after burn-up, the
        workstream and overall variants as stacked bars at the bottom of
        their Execution views, axes and compact formatters only, tooltips
        with exact values, legend glyphs with words, loading/empty/failed
        states, a takeaway naming the main split, and no doctor/health
        words."""
        card = (STATIC / "cards" / "phases.js").read_text()
        for marker in (
            "phaseUsageSpec",
            "phaseUsageCard",
            "phaseUsageAllSpec",
            "phaseUsageWsSpec",
            "phaseUsageWsCard",
            "phaseBar",
            "phaseShares",
            "cumSum",
            "phaseStackDays",
            "phaseStackedBars",
            "Planning versus execution",
            "Kind of work",
            "data-nav",
            "axes(",
            "fmtN(",
            "fmtUsd(",
            "showTip",
            "hideTip",
            "mousemove",
            "mouseleave",
            "aria-pressed",
            "aria-label",
            "cumulative_cost",
            "Per day",
            "Cumulative",
            "vallab",
            "Most of ",
            "could not be split by label",
            "went to ",
            "request failed",
            "Loading planning versus execution",
            "No usage recorded",
            "/api/project/phases",
        ):
            self.assertIn(marker, card, f"phases.js lost the card branch: {marker}")
        self.assertIn('s("rect"', card, "phases.js draws no stacked rects")
        for bad in (".toFixed(", "toLocaleString()", '"dagster"', "'dagster'", "Infinity", "doctor", "health", "fetch("):
            self.assertNotIn(bad, card, f"phases.js must not contain {bad}")
        reg = (STATIC / "cards" / "registry.js").read_text()
        self.assertIn('"phases"', reg, "phases is not registered")
        self.assertIn('"phases-ws"', reg, "the workstream variant is not registered")
        self.assertIn('"phases-all"', reg, "the overall variant is not registered")
        self.assertIn('scope: "project"', reg, "phases misses the project scope")
        self.assertIn('scope: "workstream"', reg, "phases misses the workstream scope")
        self.assertIn('scope: "overview"', reg, "phases misses the overview scope")
        self.assertIn('slot: "exec-phases"', reg, "phases misses its slot")
        home = (STATIC / "cards" / "project-home.js").read_text()
        self.assertIn("exec-phases", home, "the home lost the exec-phases slot")
        self.assertLess(
            home.index('"exec-burnup", "exec-phases"'),
            home.index('"exec-health"'),
            "exec-phases no longer mounts right after burn-up",
        )
        page = (STATIC / "index.html").read_text()
        self.assertIn("/static/cards/phases.css", page, "index.html never loads the card CSS")
        self.assertIn("/static/cards/phases.js", page, "index.html never loads the card script")
        api_src = (STATIC / "core" / "api.js").read_text()
        self.assertIn('"/api/project/phases": "phases"', api_src, "FIXMAP never maps /api/project/phases")
        tokens = (STATIC / "tokens.css").read_text()
        for var in ("--ph-planning", "--ph-execution", "--ph-review", "--ph-coordination", "--ph-unclassified"):
            self.assertIn(var, tokens, f"tokens.css lost the phase colour {var}")
        css = (STATIC / "cards" / "phases.css").read_text()
        for marker in (
            ".ph-bar",
            ".ph-seg",
            ".phase-legend",
            ".ph-takeaway",
            ".mg-phase",
            "@media (max-width:700px)",
            "prefers-reduced-motion",
        ):
            self.assertIn(marker, css, f"phases.css lost the style rule: {marker}")
        for bad in ("doctor", "health"):
            self.assertNotIn(bad, css.lower(), f"phases.css carries a {bad!r} word")

    def test_phases_stacked_bar_math(self):
        """The stacked-bar shares and cumulative series are pure: shares
        sum to one, a zero total shares nothing, cumulatives accumulate,
        and the per-day stacks carry offsets, totals, empty days, and
        unclassified last."""
        script = (
            "const fs=require('fs');"
            "var window={};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const C=window.AdPage.cards;"
            "const assert=require('assert');"
            "const shares=C.phaseShares([{phase:'planning',value:30},{phase:'execution',value:60},{phase:'unclassified',value:10}]);"
            "assert.deepStrictEqual(shares.map(s=>s.share),[0.3,0.6,0.1]);"
            "assert.ok(Math.abs(shares.reduce((a,s)=>a+s.share,0)-1)<1e-9);"
            "const zero=C.phaseShares([{phase:'planning',value:0},{phase:'execution',value:0}]);"
            "assert.deepStrictEqual(zero.map(s=>s.share),[0,0]);"
            "assert.deepStrictEqual(C.cumSum([2,0,5]),[2,2,7]);"
            "assert.deepStrictEqual(C.cumSum(null),[]);"
            "assert.deepStrictEqual(C.cumSum([]),[]);"
            "assert.strictEqual(typeof C.phaseStackDays,'function');"
            "const item={days:[1000,2000,3000],series:{"
            "planning:{cost:[3,0,1],tokens:[30,0,10]},"
            "execution:{cost:[6,0,1],tokens:[60,0,10]},"
            "review:{cost:[0,0,1],tokens:[0,0,10]},"
            "coordination:{cost:[0,0,1],tokens:[0,0,10]},"
            "unclassified:{cost:[1,0,1],tokens:[10,0,10]}}};"
            "const st=C.phaseStackDays(item,'cost');"
            "assert.strictEqual(st.stacks.length,3);"
            "assert.deepStrictEqual(st.stacks.map(s=>s.total),[10,0,5]);"
            "assert.strictEqual(st.max,10);"
            "assert.strictEqual(st.grand,15);"
            "const first=st.stacks[0];"
            "assert.deepStrictEqual(first.segs.map(s=>s.phase),['planning','execution','review','coordination','unclassified']);"
            "assert.deepStrictEqual(first.segs.map(s=>s.y0),[0,3,9,9,9]);"
            "assert.deepStrictEqual(first.segs.map(s=>s.y1),[3,9,9,9,10]);"
            "assert.ok(Math.abs(first.segs.reduce((a,s)=>a+s.share,0)-1)<1e-9);"
            "const empty=st.stacks[1];"
            "assert.strictEqual(empty.total,0);"
            "assert.deepStrictEqual(empty.segs.map(s=>s.value),[0,0,0,0,0]);"
            "assert.deepStrictEqual(empty.segs.map(s=>s.y0),[0,0,0,0,0]);"
            "assert.deepStrictEqual(empty.segs.map(s=>s.share),[0,0,0,0,0]);"
            "const toks=C.phaseStackDays(item,'tokens');"
            "assert.deepStrictEqual(toks.stacks.map(s=>s.total),[100,0,50]);"
            "const ranged=C.phaseStackDays(item,'cost',{since:1500,until:2500});"
            "assert.deepStrictEqual(ranged.days,[2000]);"
            "assert.deepStrictEqual(ranged.stacks.map(s=>s.total),[0]);"
            "assert.deepStrictEqual(C.phaseStackDays(null,'cost').stacks,[]);"
            "assert.deepStrictEqual(C.phaseSortByTotal([{ws:'A',by_phase:{planning:{cost_usd:1},execution:{cost_usd:0},review:{cost_usd:0},coordination:{cost_usd:0},unclassified:{cost_usd:0}}},{ws:'B',by_phase:{planning:{cost_usd:5},execution:{cost_usd:0},review:{cost_usd:0},coordination:{cost_usd:0},unclassified:{cost_usd:0}}}],'cost').map(r=>r.ws),['B','A']);"
            "assert.ok(C.phaseWsTakeaway('Relay (WS9)',{by_phase:{planning:{cost_usd:9},execution:{cost_usd:1},review:{cost_usd:0},coordination:{cost_usd:0},unclassified:{cost_usd:0}}},'cost').indexOf('went to planning')>=0);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "cards" / "phases.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_phases_fixture_shape(self):
        """fixtures/phases.json mirrors the endpoint shape the card reads."""
        import json as _json
        doc = _json.loads(((STATIC.parent / "fixtures") / "phases.json").read_text())
        self.assertEqual(
            set(doc),
            {"scope", "since", "until", "day", "rules_hash", "rules_version",
             "caption", "workstreams", "total", "unclassified_share", "_note"},
        )
        self.assertTrue(doc["workstreams"])
        row = doc["workstreams"][0]
        self.assertEqual(set(row), {"ws", "name", "by_phase", "total", "days", "series"})
        self.assertEqual(
            set(row["by_phase"]), {"planning", "execution", "review", "coordination", "unclassified"}
        )


class ProjectSwitcherTests(unittest.TestCase):
    def test_switcher_wiring(self):
        """The switcher reads /api/projects, needs fixtures + CSS, and is
        keyboard accessible; the API map covers the new endpoint."""
        core = (STATIC / "core" / "project.js").read_text()
        for needle in (
            "/api/projects",
            "../fixtures/projects.json",
            "proj-switcher",
            "proj-menu",
            "proj-item",
            "proj-dot",
            "running",
            "stopped",
            "project launch",
            "copy",
            "aria-haspopup",
            "aria-expanded",
            "menuitem",
            "Escape",
            "ArrowDown",
            "ArrowUp",
            "onkeydown",
            "loadProjects",
            "projectList",
        ):
            self.assertIn(needle, core, f"project.js lost {needle!r}")
        api = (STATIC / "core" / "api.js").read_text()
        self.assertIn('"/api/projects": "projects"', api, "FIXMAP never maps /api/projects")
        fixture = STATIC.parent / "fixtures" / "projects.json"
        self.assertTrue(fixture.is_file(), "fixtures/projects.json is missing")
        doc = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(doc["projects"]), 2, "the switcher needs 2+ fixture projects")
        for row in doc["projects"]:
            self.assertEqual(set(row) - {"_note"}, {"id", "name", "port", "running", "current"}, row)
            self.assertNotIn("data_root", row)
        page = (STATIC / "index.html").read_text()
        for needle in (".proj-switcher", ".proj-menu", ".proj-item", ".proj-dot"):
            self.assertIn(needle, page, f"index.html lost switcher CSS {needle!r}")
    def test_switcher_renders_only_with_two_projects(self):
        """One project keeps the plain name; two build the menu button with
        a running link and a stopped launch command; the button toggles and
        Escape closes. Keyboard-operable, read-only (no writes)."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
const src = fs.readFileSync(process.argv[2], "utf8");
function El(tag) {
  return {tag: tag, attrs: {}, children: [], hidden: false, parentNode: null,
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    appendChild: function (c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild: function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    querySelector: function (sel) {
      var cls = sel[0] === "." ? sel.slice(1) : null;
      var found = null;
      (function walk(n) { n.children.forEach(function (c) {
        if (found) return;
        if (cls && c.attrs && c.attrs.class && c.attrs.class.split(" ").indexOf(cls) >= 0) { found = c; return; }
        if (c.children) walk(c);
      }); })(this);
      return found;
    },
    querySelectorAll: function (sel) {
      var cls = sel[0] === "." ? sel.slice(1) : null;
      var out = [];
      (function walk(n) { n.children.forEach(function (c) {
        if (cls && c.attrs && c.attrs.class && c.attrs.class.split(" ").indexOf(cls) >= 0) out.push(c);
        if (c.children) walk(c);
      }); })(this);
      return out;
    }};
}
function textOf(n) {
  if (!n) return "";
  if (n.text !== undefined) return String(n.text);
  if (typeof n.textContent === "string" && (!n.children || !n.children.length)) return n.textContent;
  return (n.children || []).map(textOf).join(" ");
}
var titleEl = El("h1");
titleEl._text = "Agent dashboard";
Object.defineProperty(titleEl, "textContent", {
  get: function () {
    if (!this.children.length) return this._text;
    return this.children.map(textOf).join(" ");
  },
  set: function (v) { this._text = String(v); this.children = []; },
});
Object.defineProperty(titleEl, "innerHTML", {
  get: function () { return ""; },
  set: function () { this.children = []; },
});
var header = El("header");
header.appendChild(titleEl);
var docListeners = [];
global.window = globalThis;
global.addEventListener = function () {};
global.document = {
  title: "",
  getElementById: function (id) { return id === "title" ? titleEl : null; },
  createElement: function (t) { return El(t); },
  createTextNode: function (t) { return {text: String(t)}; },
  addEventListener: function (t, f) { docListeners.push([t, f]); },
};
global.state = {view: "exec", ws: "all"};
global.FIX = false;
global.applyChrome = function () {};
var mode = "single";
var single = {projects: [{id: "demo", name: "Demo project", port: 8890, running: true, current: true}], current: "demo"};
var multi = {projects: [
  {id: "demo", name: "Demo project", port: 8890, running: true, current: true},
  {id: "beta", name: "Beta project", port: 8891, running: false, current: false}], current: "demo"};
global.fetch = function (url) {
  var body = String(url).indexOf("projects") >= 0 ? (mode === "single" ? single : multi) : {name: "Demo project"};
  return Promise.resolve({ok: true, json: function () { return Promise.resolve(body); }});
};
eval(src);
setTimeout(function () {
  try {
    assert.strictEqual(header.querySelector(".proj-switcher"), null, "one project must keep the plain name");
    assert.ok(titleEl.textContent.indexOf("Demo project") >= 0, "header lost the name: " + titleEl.textContent);
    mode = "multi";
    loadProjects();
    setTimeout(function () {
      try {
        var btn = header.querySelector(".proj-switcher");
        assert.ok(btn, "two projects must build the menu button");
        assert.strictEqual(btn.attrs["aria-haspopup"], "true");
        var menu = header.querySelector(".proj-menu");
        assert.ok(menu, "two projects must build the menu");
        assert.strictEqual(menu.attrs.role, "menu");
        var items = menu.querySelectorAll(".proj-item");
        assert.strictEqual(items.length, 2, "menu lists every project");
        assert.strictEqual(items[0].tag, "a", "a running project navigates");
        assert.ok(String(items[0].attrs.href).indexOf("127.0.0.1:8890") >= 0, "running row links the port: " + items[0].attrs.href);
        assert.ok(textOf(items[0]).indexOf("running") >= 0, "running row says the word: " + textOf(items[0]));
        assert.ok(textOf(items[0]).indexOf("\u25CF") >= 0, "running row carries the dot");
        assert.strictEqual(items[1].tag, "div", "a stopped project is not a link");
        assert.ok(textOf(items[1]).indexOf("stopped") >= 0, "stopped row says the word: " + textOf(items[1]));
        assert.ok(textOf(items[1]).indexOf("python3 agent_dash.py project launch beta") >= 0, "stopped row shows the command: " + textOf(items[1]));
        var copy = items[1].querySelector(".proj-copy");
        assert.ok(copy, "stopped row offers the command to copy");
        assert.strictEqual(typeof btn.onclick, "function", "the button toggles by click");
        btn.onclick();
        assert.strictEqual(menu.hidden, false, "click opens the menu");
        assert.strictEqual(btn.attrs["aria-expanded"], "true");
        var esc = docListeners.filter(function (l) { return l[0] === "keydown"; })[0];
        assert.ok(esc, "Escape closes the menu from the keyboard");
        esc[1]({key: "Escape"});
        assert.strictEqual(menu.hidden, true, "Escape never leaves the menu open");
        assert.ok(items[1].onkeydown, "rows move with the arrow keys");
        console.log("project switcher proof ok");
      } catch (e) { console.error("ASSERT: " + e.message); process.exit(1); }
    }, 100);
  } catch (e) { console.error("ASSERT: " + e.message); process.exit(1); }
}, 100);
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "project_switcher.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "core" / "project.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("project switcher proof ok", proc.stdout)


def _lum(hex6):
    h = hex6.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


class FinalPolishTests(unittest.TestCase):
    """Live-review polish: no page warnings, keyboard-operable cards,
    takeaway chart labels, phone containment, AA muted contrast."""

    def test_footer_carries_no_reconcile_or_warning(self):
        api = (STATIC / "core" / "api.js").read_text()
        self.assertNotIn('api("/api/reconcile"', api)
        self.assertNotIn("Numbers reconcile", api)
        self.assertNotIn("mismatches", api)
        self.assertIn("local, 127.0.0.1", api)

    def test_slot_key_folds_in_the_entry_key(self):
        for name in ("cards/project-home.js", "cards/planned.js"):
            text = (STATIC / name).read_text()
            self.assertIn("innerKey", text, f"{name} slotSection lost its inner key")

    def test_clickable_cards_are_keyboard_buttons(self):
        wsc = (STATIC / "cards" / "ws-cards.js").read_text()
        for needle in ('attrs.role = "button"', 'attrs.tabindex = "0"', 'attrs["aria-label"]', "attrs.onkeydown"):
            self.assertIn(needle, wsc, f"ws-cards.js lost {needle}")
        prog = (STATIC / "cards" / "plan-progress.js").read_text()
        for needle in ('"role", "button"', '"tabindex", "0"', '"aria-label"', '"keydown"', "chipActivate"):
            self.assertIn(needle, prog, f"plan-progress.js lost {needle}")
        tl = (STATIC / "cards" / "activity.js").read_text()
        for needle in ('role: "button"', 'tabindex: "0"', '"aria-label"', '"keydown"'):
            self.assertIn(needle, tl, f"activity.js lost {needle}")

    def test_switcher_row_enter_reaches_copy(self):
        core = (STATIC / "core" / "project.js").read_text()
        self.assertIn("copy.focus()", core)

    def test_reduced_motion_and_visible_focus(self):
        page = (STATIC / "index.html").read_text()
        self.assertIn("prefers-reduced-motion", page)
        self.assertIn(":focus-visible", page)
        core = "\n".join(_core_scripts()) + "\n" + (STATIC / "cards" / "plan-progress.js").read_text()
        self.assertIn("matchMedia", core)
        self.assertIn("RP_REDUCED", core)

    def test_svg_charts_name_the_takeaway(self):
        act = (STATIC / "cards" / "activity.js").read_text()
        self.assertIn('role: "img"', act)
        self.assertIn("in the selected range", act)
        self.assertIn("total", act)
        self.assertIn('"aria-label": actTimelineAria(', act)
        self.assertIn("Agent runs in the selected range", act)
        for word in ('"finished"', '"partial"', '"blocked"', '"died"', '"cancelled"', '"stalled"', '"running"', '"no verdict"'):
            self.assertIn(word, act, f"aria never says {word}")
        self.assertNotIn("died or blocked", act)
        burn = (STATIC / "cards" / "burnup.js").read_text()
        self.assertIn('"aria-label": label', burn)
        self.assertIn("recorded", burn)
        self.assertIn("total, peak", burn)
        dep = (STATIC / "cards" / "depgraph.js").read_text()
        self.assertIn("workstreams, ", dep)

    def test_dep_graph_does_not_upscale_single_node(self):
        dep = (STATIC / "cards" / "depgraph.js").read_text()
        self.assertIn("maxWidth", dep)

    def test_muted_contrast_meets_aa(self):
        css = (STATIC / "tokens.css").read_text()

        def var(block, name):
            m = re.search(block + r"\s*\{([^}]*)\}", css, re.S)
            self.assertIsNotNone(m, f"tokens.css lost {block}")
            m2 = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", m.group(1))
            self.assertIsNotNone(m2, f"tokens.css lost {name} in {block}")
            return m2.group(1)

        dark = _ratio(var(r":root", "--muted"), var(r":root", "--card"))
        light = _ratio(var(r':root\[data-theme="light"\]', "--muted"), var(r':root\[data-theme="light"\]', "--card"))
        self.assertGreaterEqual(dark, 4.5, f"dark muted contrast {dark:.2f}")
        self.assertGreaterEqual(light, 4.5, f"light muted contrast {light:.2f}")

    def test_outcome_marks_meet_graphics_contrast(self):
        """Every outcome mark clears 3:1 on the card in both themes, so
        died red and blocked orange differ by measurement, not by eye —
        and the grey (cancelled, no verdict) and amber (partial, stalled)
        marks stay legible too."""
        css = (STATIC / "tokens.css").read_text()

        def var(block, name):
            m = re.search(block + r"\s*\{([^}]*)\}", css, re.S)
            self.assertIsNotNone(m, f"tokens.css lost {block}")
            m2 = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", m.group(1))
            self.assertIsNotNone(m2, f"tokens.css lost {name} in {block}")
            return m2.group(1)

        for theme, block in (("dark", r":root"), ("light", r':root\[data-theme="light"\]')):
            card = var(block, "--card")
            for name in ("--critical", "--serious", "--warning", "--muted", "--good"):
                ratio = _ratio(var(block, name), card)
                self.assertGreaterEqual(ratio, 3.0, f"{theme} {name} mark contrast {ratio:.2f}")
            self.assertNotEqual(
                var(block, "--critical"), var(block, "--serious"), f"{theme} died/blocked share a mark"
            )

    def test_accent_text_never_sits_on_accent_fill(self):
        css = (STATIC / "cards" / "plan-progress.css").read_text()
        self.assertIn("--accent-ink", css)
        self.assertIn("--accent-fill", css)
        self.assertNotIn("background:var(--s1);color:#fff", css)

    def test_switcher_rows_stay_in_their_lane(self):
        # The tab strip retired into the switcher: rows keep one line
        # with an ellipsis and the hot row scrolls into view.
        css = (STATIC / "cards" / "manage.css").read_text()
        self.assertIn(".ws-row-name", css)
        self.assertIn("text-overflow:ellipsis", css)
        switcher = (STATIC / "core" / "switcher.js").read_text()
        self.assertIn("ws-row-name", switcher)
        self.assertIn("scrollIntoView", switcher)

    def test_phone_hides_the_dock_rail(self):
        css = (STATIC / "tokens.css").read_text()
        self.assertIn("@media (max-width: 700px)", css)
        self.assertIn(".sidenav:not(.is-open)", css)
        self.assertIn(".sidenav.is-open", css)

    def test_phone_tiles_keep_their_words(self):
        css = (STATIC / "cards" / "tiles.css").read_text()
        self.assertIn(".tile .delta .w{white-space:nowrap}", css)
        self.assertIn(".tile .xline{-webkit-line-clamp:3}", css)

    def test_burnup_legend_skips_zero_series(self):
        burn = (STATIC / "cards" / "burnup.js").read_text()
        self.assertIn("if (!v) return;", burn)

    def test_quiet_tabs_state_idleness_plainly(self):
        live = (STATIC / "cards" / "activity.js").read_text()
        self.assertIn("No live agents in this window.", live)
        tiles = (STATIC / "cards" / "tiles.js").read_text()
        self.assertIn("(passN + part + blockedN + diedN + cancN + unkN)", tiles)
        for word in ('" pass"', '" partial"', '" blocked"', '" died"', '" cancelled"', '" no verdict"', '" (24 h)"'):
            self.assertIn(word, tiles, f"tile caption never says {word}")

    def test_planned_cards_state_the_wait_once(self):
        home = (STATIC / "cards" / "project-home.js").read_text()
        self.assertNotIn("pj-wait", home)

    def test_plan_sidenav_has_no_decisions_group(self):
        """The plan tab sidenav names no Decisions group: the plan tab
        renders no decisions section, so agent-friendly falls to Reference."""
        nav = (STATIC / "sidenav.js").read_text()
        self.assertNotIn("Decisions", nav)
        self.assertNotIn("decided", nav)
        self.assertIn('"Reference"', nav)

    def test_untitled_decisions_show_no_dash(self):
        dec = (STATIC / "cards" / "decisions.js").read_text()
        self.assertIn('titleText !== "-"', dec)

    def test_unplanned_lines_separate_name_and_count(self):
        home = (STATIC / "cards" / "project-home.js").read_text()
        self.assertIn('" · " + sub', home)

    def test_plan_mono_wraps(self):
        css = (STATIC / "plan.css").read_text()
        self.assertIn(".plan-view .mono", css)
        self.assertIn("overflow-wrap: anywhere", css)

    def test_pinned_chrome_is_one_slim_row(self):
        """While scrolled only one slim row stays pinned: .topbar at
        --pinned-h (48 px desktop, 44 px phone). The full header
        (.topscroll: live pill, Plan | Execution, Project | Current |
        Planned | Archive, Theme) scrolls away, and no other top-anchored
        sticky may sneak back in. Anchors land below the slim row
        through scroll-padding-top tied to the same token."""
        tokens = (STATIC / "tokens.css").read_text()
        desk = re.search(r"--pinned-h:\s*(\d+)px", tokens)
        self.assertIsNotNone(desk, "tokens.css lost --pinned-h")
        self.assertLessEqual(int(desk.group(1)), 48, "the slim pinned row is taller than 48 px")
        phone = re.search(r"@media\s*\(max-width:\s*700px\)\s*\{[^}]*--pinned-h:\s*(\d+)px", tokens, re.S)
        self.assertIsNotNone(phone, "tokens.css lost the phone --pinned-h")
        self.assertLessEqual(int(phone.group(1)), 44, "the slim pinned row is taller than 44 px on phones")
        page = (STATIC / "index.html").read_text()
        style = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
            if "position:sticky" in body and re.search(r"(?<![-\w])top\s*:\s*0", body):
                self.assertIn(".topbar", sel, f"top-anchored sticky outside the slim row: {sel.strip()}")
        flat = style.replace(" ", "")
        self.assertIn("height:var(--pinned-h)", flat, "the slim row is not sized by --pinned-h")
        topbar = re.search(r'<div class="topbar">(.*?)</div>\s*<div id="exec-view"', page, re.S)
        self.assertIsNotNone(topbar, "the .topbar shell is gone")
        slim = topbar.group(1)
        # The tab strip retired into the switcher: the slim row keeps the
        # title, Overall, the selected chip, the one switcher button, and
        # the pins. Theme stays in the scrolling header, never pinned.
        for keep in ('id="title"', 'id="overall-btn"', 'id="selected-chip"',
                      'id="ws-switcher"', 'id="ws-pins"'):
            self.assertIn(keep, slim, f"the slim row lost {keep}")
        self.assertNotIn('id="tabs"', slim, "the tab strip is back in the slim row")
        for gone in ('id="view-nav"', "view-toggle", 'class="live"', 'id="theme"', "nav-sections"):
            self.assertNotIn(gone, slim, f"a secondary header control stays pinned: {gone}")
        self.assertIn('class="topscroll"', page, "the scrolling full header is gone")
        self.assertIn("scroll-padding-top", style, "no scroll-padding-top for the pinned row")
        self.assertIn("var(--pinned-h)", style, "the scroll offset is not tied to --pinned-h")
        checker = (STATIC.parent / "scripts" / "page_check.mjs").read_text()
        for marker in ("pinned", "Emulation.setDeviceMetricsOverride", "scrollTo", "m.pinned > 48", "m.pinned > 44"):
            self.assertIn(marker, checker, f"page_check.mjs lost the pinned-chrome gate: {marker}")

    def test_project_name_is_never_truncated(self):
        """The project name keeps its natural width: .topbar h1 has no
        max-width and no ellipsis (desktop or phone); the switcher-side
        chips shrink to give way instead (the tab strip is gone)."""
        page = (STATIC / "index.html").read_text()
        style = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        desk = re.search(r"\.topbar h1\{(.*?)\}", style)
        self.assertIsNotNone(desk, ".topbar h1 rule is gone")
        self.assertNotIn("max-width", desk.group(1), "the project name is width-capped")
        self.assertNotIn("text-overflow", desk.group(1), "the project name ellipsizes")
        self.assertNotIn("ellipsis", desk.group(1), "the project name ellipsizes")
        self.assertIn("flex:none", desk.group(1).replace(" ", ""), "the project name does not keep its natural width")
        phone = re.search(r"\.topbar h1#title\{(.*?)\}", style)
        self.assertIsNotNone(phone, "the phone title rule is gone")
        self.assertNotIn("max-width", phone.group(1), "the phone project name is width-capped")
        self.assertNotIn("ellipsis", phone.group(1), "the phone project name ellipsizes")
        chips = (STATIC / "cards" / "manage.css").read_text()
        sel = re.search(r"#selected-chip\{(.*?)\}", chips)
        self.assertIsNotNone(sel, "#selected-chip rule is gone")
        self.assertIn("min-width:0", sel.group(1).replace(" ", ""), "the selected chip no longer shrinks to give way")
        self.assertIn("overflow:hidden", sel.group(1).replace(" ", ""), "the selected chip no longer clips to give way")

    def test_theme_toggle_sits_top_right_alone(self):
        """Exactly one Theme control: a real button with an aria-label in
        the scrolling top row (far right via margin-left:auto), never in
        the pinned tab strip, still wired to the click handler."""
        page = (STATIC / "index.html").read_text()
        self.assertEqual(page.count('id="theme"'), 1, "there is not exactly one Theme control")
        top = re.search(r'<div class="topscroll">(.*?)</div>\s*<div class="topbar">', page, re.S)
        self.assertIsNotNone(top, "the scrolling top header is gone")
        theme = re.search(r'<button[^>]*id="theme"[^>]*>', top.group(1))
        self.assertIsNotNone(theme, "the Theme control is not in the scrolling top row")
        self.assertIn("aria-label", theme.group(0), "the Theme button has no aria-label")
        style = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        right = re.search(r"header\.shell #theme\{(.*?)\}", style)
        self.assertIsNotNone(right, "the Theme control lost its far-right rule")
        self.assertIn("margin-left:auto", right.group(1).replace(" ", ""), "the Theme control is not right-aligned")
        self.assertIn('getElementById("theme")', page, "the Theme click wiring is gone")

    def test_sections_button_is_gone(self):
        """No Sections button anywhere: no markup, styles, handlers,
        shortcut hints, or dead references. The hover rail itself stays."""
        page = (STATIC / "index.html").read_text()
        nav = (STATIC / "sidenav.js").read_text()
        tokens = (STATIC / "tokens.css").read_text()
        for name, src in (("index.html", page), ("sidenav.js", nav), ("tokens.css", tokens)):
            self.assertNotIn("nav-sections", src, f"{name} still references the Sections button")
        for src in (STATIC / "tabs.js", STATIC / "sidenav.js"):
            if src.exists():
                self.assertNotIn("nav-sections", src.read_text(), f"{src.name} still references the Sections button")
        for src in (STATIC / "core").glob("*.js"):
            self.assertNotIn("nav-sections", src.read_text(), f"{src.name} still references the Sections button")
        # The switcher's own "/" shortcut hint stays; only the Sections
        # "[" hint is banned.
        self.assertNotIn('keyshortcuts="["', page, "a Sections shortcut hint is left in the page")
        self.assertNotIn('key !== "["', nav, "the [ shortcut outlived the Sections button")
        self.assertIn("Escape", nav, "the rail lost its Escape-to-close")
        self.assertIn("sidenav-hot", nav, "the hover rail itself is gone")
        self.assertIn("sidenav-hot", tokens, "the hover rail lost its styles")

    def test_fmt_durations_never_print_sixty(self):
        harness = (
            "const fs = require('fs');\n"
            "const assert = require('assert');\n"
            "var window = {};\n"
            "eval(fs.readFileSync(process.argv[2], 'utf8'));\n"
            "assert.strictEqual(window.AD.fmtDur(90), '1m 30s');\n"
            "assert.strictEqual(window.AD.fmtDur(579599), '160h 59m');\n"
            "assert.strictEqual(window.AD.fmtDur(359.6), '5m 59s');\n"
            "console.log('fmtDur proof ok');\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "fmtdur.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "format.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("fmtDur proof ok", proc.stdout)

    def test_archive_confirm_never_uses_window_confirm(self):
        """The archive confirm is an inline popover, never window.confirm:
        no page script may call the blocking dialog."""
        script = inline_scripts()
        plan_js = (STATIC / "plan.js").read_text()
        for name, src in (("index.html", script), ("plan.js", plan_js)):
            self.assertNotIn("window.confirm(", src, f"{name} blocks on window.confirm")
            hits = re.findall(r"(?<![A-Za-z0-9_$])confirm\s*\(", src)
            self.assertEqual(hits, [], f"{name} calls a bare confirm(): {hits[:2]}")

    def test_archive_post_goes_through_the_shared_api_layer(self):
        """Archive/restore POST through apiPost in static/core/api.js with
        the X-AgentDash-Action header; the controls never raw-fetch."""
        api_src = (STATIC / "core" / "api.js").read_text()
        self.assertIn("function apiPost(", api_src, "the shared POST helper is gone")
        self.assertIn("X-AgentDash-Action", api_src, "apiPost lost the action header")
        actions_src = (STATIC / "core" / "actions.js").read_text()
        self.assertIn('apiPost("/api/workstreams/archive"', actions_src)
        self.assertIn('apiPost("/api/workstreams/restore"', actions_src)
        self.assertNotIn("fetch(", actions_src, "the action controls bypass the shared api layer")
        page = (STATIC / "index.html").read_text()
        self.assertIn('<script src="/static/core/actions.js"></script>', page)

    def test_archive_buttons_name_the_workstream(self):
        """Every Archive/Restore button is a real <button> whose
        aria-label names the workstream; requests disable the control
        in flight (no double submit)."""
        actions_src = (STATIC / "core" / "actions.js").read_text()
        for fn in ("archiveControl", "restoreControl"):
            self.assertIn("function %s(" % fn, actions_src, f"{fn} is gone")
        self.assertIn('"aria-label", "Archive " + displayName', actions_src)
        self.assertIn('"aria-label", "Restore " + displayName', actions_src)
        self.assertIn("displayName(id, name)", actions_src)
        self.assertIn("btns[i].disabled", actions_src, "in-flight buttons stay clickable")
        for path, marker in (
            ("cards/ws-cards.js", "archiveControl(w.ws, w.name)"),
            ("cards/project-home.js", "archiveControl(w.ws, w.name)"),
            ("cards/archive.js", "restoreControl(id, card.title)"),
        ):
            src = (STATIC / path).read_text()
            self.assertIn(marker, src, f"{path} lost its real action control")

    def test_archive_confirm_and_undo_pattern(self):
        """The confirm popover names the workstream, offers Archive and
        Cancel, closes on Escape, manages focus, and success toasts an
        Undo kept 10 s; restore stays confirm-free."""
        actions_src = (STATIC / "core" / "actions.js").read_text()
        for marker in (
            '"Archive " + label + "? It moves to the Archive view; you can restore it."',
            'role: "dialog"',
            '"Cancel"',
            '"Escape"',
            ".focus()",
            '"Undo"',
            "10000",
            '"Restored "',
            '"Archived "',
            "Copy command",
        ):
            self.assertIn(marker, actions_src, f"actions.js lost the confirm/undo branch: {marker}")
        archive_src = (STATIC / "cards" / "archive.js").read_text()
        self.assertIn("restoreControl", archive_src)

    def test_undo_expires_with_the_toast(self):
        """The toast hides with opacity, so expiry must detach the Undo
        button with its handler: an invisible Undo must never stay
        keyboard-reachable or re-fire its action after the 10 s window."""
        for path in ("core/manage.js", "core/actions.js"):
            src = (STATIC / path).read_text()
            self.assertIn("if (u.parentNode === t) t.removeChild(u)", src,
                          f"{path} leaves a stale Undo behind when the toast expires")

    def test_archive_falls_back_to_copy_command(self):
        """With writes off (read-only server, fixture preview) every
        control degrades to the copy-command button, so the CLI path
        stays visible."""
        actions_src = (STATIC / "core" / "actions.js").read_text()
        self.assertIn("actionsEnabled()", actions_src)
        self.assertIn("state.actionsArchive", actions_src)
        self.assertIn('copyBtn("python3 agent_dash.py ws archive " + id, "Archive")', actions_src)
        self.assertIn('copyBtn("python3 agent_dash.py ws restore " + id, "Restore")', actions_src)
        api_src = (STATIC / "core" / "api.js").read_text()
        self.assertIn("state.actionsArchive = !!", api_src, "loadNav lost the actions flag")
        self.assertIn("state.actionsArchive = false", api_src)

    def test_exec_section_order_activity_stack(self):
        """One Activity over time card replaces the separate chart
        sections: on both exec scopes the registry order is ... plan,
        activity, finished, the planning-versus-execution stacked bars
        follow, and progress over time is the third strip inside the
        stack (no standalone progress section anywhere). The sidenav
        derives its order from the DOM, so no pin there; the page
        comment and DESIGN.md name the same order."""
        registry = (STATIC / "cards" / "registry.js").read_text()
        entries = re.findall(
            r'\{id:\s*"([^"]+)",\s*order:\s*(\d+),\s*scope:\s*"([^"]+)"', registry
        )
        self.assertGreater(len(entries), 5, "the registry entry pattern no longer matches")
        both = sorted(
            [(int(order), ident) for ident, order, scope in entries if scope == "both"]
        )
        self.assertEqual(
            [ident for _, ident in both],
            ["tiles", "running", "plan", "activity", "finished"],
            "exec order drifted: want the activity stack between plan and finished",
        )
        for gone in ('"tokens"', '"gantt"', '"conc"', '"progress"'):
            self.assertNotIn(
                gone.replace('"', 'id: "'), registry, f"the registry still registers {gone}"
            )
        self.assertNotIn("progressSpec", registry, "the registry still calls the old progress spec")
        by_scope = {}
        for ident, order, scope in entries:
            by_scope.setdefault(scope, []).append((int(order), ident))
        ws = [ident for _, ident in sorted(by_scope.get("workstream", []))]
        self.assertEqual(ws[-1], "phases-ws", "the workstream usage card is not the last workstream section")
        ov = [ident for _, ident in sorted(by_scope.get("overview", []))]
        self.assertEqual(ov[-1], "phases-all", "the overall usage card is not the last section")
        orders = {ident: int(order) for ident, order, _scope in entries}
        self.assertGreater(orders["phases-ws"], orders["finished"], "the workstream usage card sits before Finished")
        stack = (STATIC / "cards" / "activity.js").read_text()
        for token in ("actProgStrip", 'data-strip": "progress"', '"exec-prog"',
                      "actProgKey", "yTicks: 4", "progStep(", "% of tasks"):
            self.assertIn(token, stack, f"activity.js lost the progress strip: {token}")
        self.assertNotIn('"exec-progress"', stack, "the strip reuses the plan map card id")
        self.assertIn("actProgKey(", registry, "the activity section key lost the progress payload")
        page = (STATIC / "index.html").read_text()
        self.assertIn("plan, activity, finished", page)
        self.assertIn("progress-over-time", page)
        self.assertNotIn("charts last on workstream tabs", page)
        self.assertIn('<script src="/static/cards/activity.js"></script>', page)
        self.assertIn('<script src="/static/cards/progress.js"></script>', page)
        self.assertNotIn("/static/cards/progress.css", page, "index.html still loads the old progress CSS")
        self.assertNotIn("token-chart.js", page, "index.html still loads the old token chart")
        self.assertNotIn("live-chart.js", page, "index.html still loads the old live chart")
        design = (STATIC.parent / "DESIGN.md").read_text()
        self.assertIn("Activity over time (the tokens, run-timeline, progress-over-time and live-agents stack)", design)
        self.assertNotIn("Progress over time charts last", design)
        self.assertLess(
            design.index("Activity over time (the tokens, run-timeline, progress-over-time and live-agents stack)"),
            design.index("Finished (24 h)"),
            "DESIGN.md no longer lists the stack before Finished",
        )
        self.assertLess(
            design.index("Finished (24 h)"),
            design.index("usage-by-phase per-day stacked columns below Finished"),
            "DESIGN.md no longer lists the usage bars after Finished",
        )
        sidenav = (STATIC / "sidenav.js").read_text()
        self.assertNotIn('"activity"', sidenav, "the sidenav hard-codes a section order")
        self.assertNotIn('"gantt"', sidenav, "the sidenav hard-codes a section order")
        self.assertNotIn('"conc"', sidenav, "the sidenav hard-codes a section order")
        self.assertNotIn('"phases', sidenav, "the sidenav hard-codes a section order")

    def test_progress_section_id_is_gone(self):
        """The old standalone progress section is fully removed: its
        section id, card and spec are gone from progress.js and the
        registry, while the pure data helpers stay exported."""
        card = (STATIC / "cards" / "progress.js").read_text()
        for gone in ("progress-ws", "progressSpec", "progressCard", "progChartA",
                     "progChartB", "progFetch", "progFrame", "progSvg",
                     "progress.css", ".toFixed(",
                     "toLocaleString(", "doctor", "health", "warn"):
            self.assertNotIn(gone, card, f"progress.js still contains {gone!r}")
        for kept in ("function progLegendTip(", "function progMileRows(",
                     "function progStepRows(", "function progBarRows(",
                     "function progTakeaway(", "function progBest(",
                     "function progStep(", "function progPctFmt("):
            self.assertIn(kept, card, f"progress.js lost the pure helper {kept}")
        self.assertNotIn("axes(", card, "progress.js draws no chart of its own anymore")

    def test_activity_range_math_runs_in_node(self):
        """The stack's range helpers are pure functions on window.AdPage:
        range clamp, preset to range, pixel<->time mapping, brush drag
        math and tick selection, all node-driven with no DOM."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
eval(fs.readFileSync(process.argv[2], "utf8"));
const A = window.AdPage;
["actClampRange", "actPresetRange", "actTimeToX", "actXToTime",
 "actDragHandle", "actDragMove", "actTickStep"].forEach(function (n) {
  assert.strictEqual(typeof A[n], "function", "AdPage." + n + " is not exported");
  assert.strictEqual(typeof window[n], "function", n + " is not a plain global");
});
/* clamp: swapped edges order, the minimum span holds, bounds win. */
assert.deepStrictEqual(A.actClampRange(1000, 1100, 0, 10000), {from: 900, to: 1200});
assert.deepStrictEqual(A.actClampRange(-100, 50000, 0, 3600), {from: 0, to: 3600});
assert.deepStrictEqual(A.actClampRange(10, 5, 0, 10000), {from: 0, to: 300});
/* presets: hour presets follow now (live), All covers the loaded window. */
assert.deepStrictEqual(A.actPresetRange("hour", 100000, 0), {from: 96400, to: 100000, live: true, preset: "hour"});
assert.deepStrictEqual(A.actPresetRange("6h", 100000, 0), {from: 78400, to: 100000, live: true, preset: "6h"});
assert.deepStrictEqual(A.actPresetRange("all", 100000, 20000), {from: 20000, to: 100000, live: false, preset: "all"});
/* pixel<->time: linear, clamped, round-trips. */
assert.strictEqual(A.actTimeToX(50, 0, 100, 150, 800), 550);
assert.strictEqual(A.actTimeToX(500, 0, 100, 150, 800), 950);
assert.strictEqual(A.actXToTime(550, 0, 100, 150, 800), 50);
assert.strictEqual(A.actXToTime(0, 0, 100, 150, 800), 0);
assert.strictEqual(A.actXToTime(A.actTimeToX(37.5, 0, 100, 150, 800), 0, 100, 150, 800), 37.5);
/* brush drag: an edge never crosses the other, the middle stops at the window. */
assert.deepStrictEqual(A.actDragHandle({from: 1000, to: 2000}, "from", 1500, 0, 10000), {from: 1500, to: 2000});
assert.deepStrictEqual(A.actDragHandle({from: 1000, to: 2000}, "from", 1900, 0, 10000), {from: 1700, to: 2000});
assert.deepStrictEqual(A.actDragHandle({from: 1000, to: 2000}, "to", -500, 0, 10000), {from: 1000, to: 1300});
assert.deepStrictEqual(A.actDragMove({from: 1000, to: 2000}, 500, 0, 10000), {from: 1500, to: 2500});
assert.deepStrictEqual(A.actDragMove({from: 9000, to: 9500}, 2000, 0, 10000), {from: 9500, to: 10000});
assert.deepStrictEqual(A.actDragMove({from: 1000, to: 2000}, -5000, 0, 10000), {from: 0, to: 1000});
/* tick selection: the smallest grid step (one minute to one week, the
   same ladder timeTicks uses) fitting the label budget, so small zooms
   read minutes instead of collapsing to one end tick. */
assert.strictEqual(A.actTickStep(86400, 6), 14400);
assert.strictEqual(A.actTickStep(7200, 6), 1800);
assert.strictEqual(A.actTickStep(3600, 6), 900);
assert.strictEqual(A.actTickStep(7 * 86400, 6), 172800);
assert.strictEqual(A.actTickStep(600, 8), 120);
assert.strictEqual(A.actTickStep(1800, 8), 300);
assert.strictEqual(A.actTickStep(7200, 8), 900);
assert.strictEqual(A.actTickStep(21600, 8), 3600);
assert.strictEqual(A.actTickStep(86400, 8), 10800);
assert.strictEqual(A.actTickStep(3 * 86400, 8), 43200);
assert.strictEqual(A.actTickStep(14 * 86400, 8), 172800);
console.log("activity range math ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "act_range_math.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "cards" / "activity.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_progress_strip_scaling_runs_in_node(self):
        """The progress strip's pure math runs node-driven with no DOM:
        percent maps linearly onto the 0-100% y axis (clamped), zoom
        clipping keeps the in-range vertices and carries the last
        earlier one in at the edge (marked, so no dot draws on it), and
        the hidden-when-empty predicate hides missing, zero-total and
        move-less payloads."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
var window = {};
eval(fs.readFileSync(process.argv[2], "utf8"));
const A = window.AdPage;
["actProgY", "actProgClip", "actProgHas", "actProgStrip", "actProgKey"].forEach(function (n) {
  assert.strictEqual(typeof A[n], "function", "AdPage." + n + " is not exported");
  assert.strictEqual(typeof window[n], "function", n + " is not a plain global");
});
/* percent to y: 0% sits on the axis, 100% on top, linear, clamped. */
assert.strictEqual(A.actProgY(0, 8, 144), 152);
assert.strictEqual(A.actProgY(100, 8, 144), 8);
assert.strictEqual(A.actProgY(50, 8, 144), 80);
assert.strictEqual(A.actProgY(25, 8, 144), 116);
assert.strictEqual(A.actProgY(-5, 8, 144), 152);
assert.strictEqual(A.actProgY(150, 8, 144), 8);
/* zoom clipping: in-range vertices pass through untouched. */
var pts = [{t: 10, done: 1, prog: 0}, {t: 20, done: 2, prog: 1}, {t: 30, done: 3, prog: 0}];
assert.deepStrictEqual(A.actProgClip(pts, 0, 100), pts);
assert.deepStrictEqual(A.actProgClip(pts, 10, 20), [{t: 10, done: 1, prog: 0}, {t: 20, done: 2, prog: 1}]);
/* a zoomed window carries the last earlier vertex in at the edge. */
assert.deepStrictEqual(A.actProgClip(pts, 15, 25),
  [{t: 15, done: 1, prog: 0, edge: true}, {t: 20, done: 2, prog: 1}]);
assert.deepStrictEqual(A.actProgClip(pts, 35, 45),
  [{t: 35, done: 3, prog: 0, edge: true}]);
assert.deepStrictEqual(A.actProgClip(pts, 0, 5), []);
assert.deepStrictEqual(A.actProgClip([], 0, 100), []);
assert.deepStrictEqual(A.actProgClip(null, 0, 100), []);
/* hidden when empty: no payload, no total, or no recorded moves. */
assert.strictEqual(A.actProgHas(null), false);
assert.strictEqual(A.actProgHas({}), false);
assert.strictEqual(A.actProgHas({total: 0, points: [{t: 1, done: 0}]}), false);
assert.strictEqual(A.actProgHas({total: 5, points: []}), false);
assert.strictEqual(A.actProgHas({total: 5, points: [{t: 1, done: 1}]}), true);
console.log("progress strip scaling ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "act_prog_scaling.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "cards" / "activity.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_activity_tick_spans_run_in_node(self):
        """The shared timeTicks keep readable x labels on every zoom: at
        spans from 10 minutes to 14 days the zoomed domain yields a
        non-empty list of 3 to 9 ticks, strictly increasing and inside
        the domain, with unique labels formatted for the span (minutes
        under 2 h, hours across 6 to 24 h, dates past a day, plus a date
        line when the range crosses midnight), and a narrow card thins
        to fewer ticks that never overlap."""
        harness = r'''
const fs = require("fs");
const assert = require("assert");
const src = fs.readFileSync(process.argv[2], "utf8").replace("})(window);", "})(global);");
eval(src);
const T = global.AD.timeTicks;
assert.strictEqual(typeof T, "function", "AD.timeTicks is not exported");
var d = new Date(); d.setHours(0, 0, 0, 0);
var t0 = d.getTime() / 1000 + 3780;
function at(span, n, px) {
  var t1 = t0 + span;
  var X = function (t) { return (t - t0) / span * px; };
  return {t0: t0, t1: t1, ticks: T(t0, t1, X, n)};
}
function checkSpan(span, fmt) {
  var r = at(span, 8, 1000), ks = r.ticks;
  assert.ok(ks.length >= 3 && ks.length <= 9, span + "s: " + ks.length + " ticks, want 3-9");
  for (var i = 0; i < ks.length; i++) {
    assert.strictEqual(typeof ks[i].label, "string", span + "s tick " + i + " has no label");
    assert.ok(ks[i].label.length > 0, span + "s tick " + i + " label is empty");
    assert.ok(ks[i].x >= 0 && ks[i].x <= 1000, span + "s tick " + i + " outside the domain");
    if (i) assert.ok(ks[i].x > ks[i - 1].x, span + "s ticks are not increasing");
  }
  var labels = ks.map(function (k) { return k.label; });
  assert.strictEqual(new Set(labels).size, labels.length, span + "s labels repeat: " + labels.join(","));
  fmt(labels, span);
  return ks;
}
/* 10 and 30 minutes read minutes. */
checkSpan(600, function (labels) {
  assert.ok(labels.filter(function (l) { return l.indexOf(":") >= 0; }).length >= 3, "10 min zoom lost its minute ticks");
});
checkSpan(1800, function (labels) {
  assert.ok(labels.filter(function (l) { return l.indexOf(":") >= 0; }).length >= 3, "30 min zoom lost its minute ticks");
});
/* A 2 h zoom reads quarter hours like 02:00, 02:15, 02:30. */
checkSpan(7200, function (labels) {
  assert.ok(labels.filter(function (l) { return l.indexOf(":") >= 0; }).length >= 3, "2 h zoom lost its minute ticks");
});
/* 6 and 24 h read whole hours, and the day crossing midnight keeps a date line
   (the last label is the end tick, which always reads the exact end time). */
checkSpan(21600, function (labels) {
  assert.ok(!labels.slice(0, -1).some(function (l) { return l.indexOf(":") >= 0; }), "6 h zoom mixes minutes into hour ticks");
});
checkSpan(86400, function (labels) {
  assert.ok(!labels.slice(0, -1).some(function (l) { return l.indexOf(":") >= 0; }), "24 h zoom mixes minutes into hour ticks");
  assert.ok(labels.some(function (l) { return /^[A-Z][a-z]{2} \d{1,2}$/.test(l); }), "24 h range crossing midnight keeps no date line");
});
/* Multi-day ranges read dates. */
checkSpan(3 * 86400, function (labels) {
  labels.slice(0, -1).forEach(function (l) { assert.ok(/^[A-Z][a-z]{2} \d{1,2}/.test(l), "3 d tick is not a date: " + l); });
});
checkSpan(14 * 86400, function (labels) {
  labels.slice(0, -1).forEach(function (l) { assert.ok(/^[A-Z][a-z]{2} \d{1,2}/.test(l), "14 d tick is not a date: " + l); });
});
/* A narrow card thins the same 2 h zoom to fewer ticks that never overlap. */
var wide = at(7200, 8, 1000), narrow = at(7200, 3, 300);
assert.ok(narrow.ticks.length < wide.ticks.length, "narrow card never thins: " + narrow.ticks.length);
assert.ok(narrow.ticks.length >= 2, "narrow card thins to nothing");
for (var j = 1; j < narrow.ticks.length; j++) {
  assert.ok(narrow.ticks[j].x - narrow.ticks[j - 1].x >= 40, "narrow ticks overlap");
}
console.log("activity tick spans ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "act_tick_spans.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC / "format.js")],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_activity_zoom_redraws_axis(self):
        """Every zoom path repaints the shared x axis from the zoomed
        domain: each strip builds one shared tick list from its zoomed
        range (actSharedTicks over range.from/range.to), draws its
        gridlines on those ticks, and the bottom strip labels them; the
        tick ladder reaches minute steps so small zooms keep labels."""
        src = (STATIC / "cards" / "activity.js").read_text()
        bodies = dict(functions(inline_scripts()))
        self.assertIn("actSharedTicks", bodies)
        shared = bodies["actSharedTicks"]
        self.assertIn("timeTicks(", shared, "the shared ticks never reach the axis helper")
        self.assertIn("range.from", shared, "the shared ticks ignore the zoomed start")
        self.assertIn("range.to", shared, "the shared ticks ignore the zoomed end")
        for name in ("actTokenStrip", "actTimelineStrip", "actProgStrip", "actConcStrip"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            self.assertIn("actSharedTicks(", bodies[name], f"{name} no longer shares the zoomed ticks")
            self.assertIn("actXGrid(", bodies[name], f"{name} lost its tick gridlines")
        self.assertIn("actXGrid", bodies)
        fmt = (STATIC / "format.js").read_text()
        self.assertIn("120, 300, 900", fmt, "the tick ladder lost its minute steps")

    def test_activity_brush_rules(self):
        """The time filter bar is a keyboard- and pointer-operable range
        control: two focusable role=slider handles with arrow/Shift/Home/
        End/Escape handling, pointer drags plus double-click reset, the
        range persisted per scope in guarded sessionStorage and in the
        URL query, and no fetch fires per pixel of drag."""
        src = (STATIC / "cards" / "activity.js").read_text()
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("actBrush", bodies)
        brush = bodies["actBrush"]
        self.assertIn('role: "slider"', brush, "handles are not role=slider")
        self.assertIn("act-handle-from", brush, "no from handle")
        self.assertIn("act-handle-to", brush, "no to handle")
        self.assertIn('tabindex: "0"', brush, "handles are not focusable")
        for key in ("ArrowLeft", "ArrowRight", "shiftKey", '"Home"', '"End"', '"Escape"'):
            self.assertIn(key, brush, f"handle ignores {key}")
        for ev in ("pointerdown", "pointermove", "pointerup", "setPointerCapture", "dblclick"):
            self.assertIn(ev, brush, f"brush ignores {ev}")
        self.assertIn("aria-valuetext", brush, "handles never announce their time")
        self.assertIn("aria-valuemin", brush)
        self.assertIn("aria-valuenow", brush)
        self.assertNotIn("api(", brush, "a drag pixel fetches")
        self.assertNotIn("actEnsureWide", brush, "a drag pixel tops up the window")
        self.assertNotIn("fetch(", src, "activity.js fetches outside api()")
        for name in ("actReadStored", "actWriteStored"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
            for needle in ("try", "catch", "sessionStorage"):
                self.assertIn(needle, bodies[name], f"{name} touches storage unguarded")
        self.assertIn("agent-dash-activity:", src, "the storage key lost its per-scope prefix")
        for name in ("actReadUrl", "actWriteUrl"):
            self.assertIn(name, bodies, f"{name} is gone from the page")
        self.assertIn("searchParams", src, "the range never reaches the URL")
        self.assertIn('"from"', src, "the URL carries no from")
        self.assertIn('"to"', src, "the URL carries no to")
        self.assertIn("act-live", src, "no live chip for following presets")
        self.assertIn("Reset", src, "no Reset button")

    def test_activity_shares_one_axis(self):
        """One card, one x axis, one filter: exec-activity carries the
        Activity over time nav label with the brush bar on top, the four
        strips stack in tokens/runs/progress/agents order on the same
        domain, only the bottom strip draws x labels, and one crosshair
        spans them."""
        src = (STATIC / "cards" / "activity.js").read_text()
        self.assertIn('"data-nav": "Activity over time"', src)
        self.assertIn('id: "exec-activity"', src.replace("id:", "id:", 1))
        self.assertEqual(src.count('"data-strip": "tokens"'), 1, "tokens strip lost its domain tag")
        self.assertEqual(src.count('"data-strip": "timeline"'), 1, "timeline strip lost its domain tag")
        self.assertEqual(src.count('"data-strip": "progress"'), 1, "progress strip lost its domain tag")
        self.assertIn('"data-strip": "conc"', src, "conc strips lost their domain tag")
        self.assertEqual(src.count('"data-x-from"'), 4, "every strip must tag its shared x domain")
        self.assertIn("conc[conc.length - 1].fam, true", src, "x labels no longer draw once at the bottom")
        self.assertIn("actWireCross", src, "no shared crosshair")
        self.assertEqual(src.count("    actCrossOf(svg"), 4, "a strip lost its crosshair line")
        bodies = dict(functions(inline_scripts()))
        self.assertIn("actPaintStrips", bodies)
        paint = bodies["actPaintStrips"]
        for strip in ("actTokenStrip", "actTimelineStrip", "actProgStrip", "actConcStrips"):
            self.assertIn(strip, paint, f"the stack no longer renders {strip}")
        self.assertLess(paint.index("actTokenStrip"), paint.index("actTimelineStrip"), "tokens must stack first")
        self.assertLess(paint.index("actTimelineStrip"), paint.index("actProgStrip"), "runs must stack before progress")
        self.assertLess(paint.index("actProgStrip"), paint.index("actConcStrips"), "progress must stack before agents")

    def test_phases_card_is_not_before_running(self):
        """The usage card never leads an Execution view: on the
        workstream and overall scopes its registry order sits after the
        running-now table, so it cannot render at the top."""
        registry = (STATIC / "cards" / "registry.js").read_text()
        entries = re.findall(
            r'\{id:\s*"([^"]+)",\s*order:\s*(\d+),\s*scope:\s*"([^"]+)"', registry
        )
        orders = {(ident, scope): int(order) for ident, order, scope in entries}
        running_both = [o for (i, s), o in orders.items() if i == "running" and s == "both"]
        self.assertTrue(running_both, "the running entry is gone from the registry")
        for key in (("phases-ws", "workstream"), ("phases-all", "overview")):
            self.assertIn(key, orders, f"the {key[0]} entry is gone from the registry")
            self.assertGreater(
                orders[key], max(running_both),
                f"{key[0]} renders before running on its exec scope",
            )

    def test_running_now_disclosure(self):
        """Running now shows the top agents with a progressive disclosure
        button: a real <button> with aria-expanded and aria-controls that
        rebuilds the card in place (no drawMain, so ticks never collapse
        it), a cap constant of 5, sessionStorage guarded by try/catch, the
        full count in the header, and no button at 5 or fewer. The data-nav
        label stays "Running now" for the sidenav. Rows group workstream ->
        section -> label with section headers, a Section | None toggle, and
        label-first agent rows."""
        tables = (STATIC / "cards" / "tables.js").read_text()
        self.assertIn("RUNNING_TOP_N = 5", tables, "the top-5 cap constant drifted")
        self.assertIn('el("button"', tables, "the disclosure is not a real <button>")
        for marker in ("aria-expanded", "aria-controls", "exec-running-rows", "runmore"):
            self.assertIn(marker, tables, f"the disclosure lost {marker}")
        self.assertNotIn("exec-running-extra", tables, "the hidden-tbody flip is back")
        self.assertIn('"Show all " + total + " running agents"', tables)
        self.assertIn('"Show top " + RUNNING_TOP_N + " agents"', tables)
        self.assertIn('title + " \\u00B7 " + runs.length', tables, "the header lost its full count")
        self.assertIn('"data-nav": title', tables, "the data-nav label must stay the plain title")
        self.assertIn("runs.length > RUNNING_TOP_N", tables, "the button must hide at 5 or fewer")
        for marker in ("groupRuns", "visibleGroups", "sec-h", "ws-chev", "Group by", "aria-pressed", '"Section"', '"None"'):
            self.assertIn(marker, tables, f"the grouping lost {marker}")
        self.assertIn('var cols = ["Label", "Model"];', tables, "agent rows are no longer label-first")
        self.assertIn('"WS", "Section"', tables, "the Finished table lost its Section column")
        bodies = dict(functions(tables))
        self.assertIn("agents", bodies["runningToggleLabel"], "the toggle label must name agents")
        for name in ("readGroupMode", "writeGroupMode",
                     "readWsCollapsed", "writeWsCollapsed"):
            self.assertIn(name, bodies, f"{name} is gone from tables.js")
            self.assertIn("try", bodies[name], f"{name} touches storage unguarded")
            self.assertIn("catch", bodies[name], f"{name} touches storage unguarded")
            self.assertIn("sessionStorage", bodies[name], f"{name} no longer uses sessionStorage")
        for name in ("readRunningExpanded", "writeRunningExpanded"):
            self.assertIn(name, bodies, f"{name} is gone from tables.js")
        self.assertIn("readDisclosed", bodies["readRunningExpanded"], "expanded reads bypass the shared helper")
        self.assertIn("writeDisclosed", bodies["writeRunningExpanded"], "expanded writes bypass the shared helper")
        self.assertIn("discloseKey", bodies["runningScopeKey"], "the scope key bypasses the shared helper")
        self.assertNotIn('"agent-dash-running:"', tables, "the pre-top-5 key is back: old expanded state would apply")
        self.assertIn("agent-dash-group:", tables, "the group-by key lost its per-scope prefix")

    def test_running_visible_helpers(self):
        """The top-5 slicing and label text are pure functions on
        window.AdPage.cards: newest started_at first (stable on ties),
        top 5 collapsed, all rows expanded, agent-naming labels."""
        script = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "const C=window.AdPage.cards;"
            "const assert=require('assert');"
            "assert.strictEqual(C.RUNNING_TOP_N,5);"
            "const rows=[100,700,300,500,200,600,400].map((t,i)=>({run_id:'r'+i,started_at:t}));"
            "assert.deepStrictEqual(C.runningOrder(rows).map(r=>r.started_at),[700,600,500,400,300,200,100]);"
            "const ties=[{run_id:'a',started_at:9},{run_id:'b',started_at:9}];"
            "assert.deepStrictEqual(C.runningOrder(ties).map(r=>r.run_id),['a','b']);"
            "assert.deepStrictEqual(C.runningOrder(null),[]);"
            "const top=C.runningVisible(rows,false);"
            "assert.strictEqual(top.length,5);"
            "assert.deepStrictEqual(top.map(r=>r.started_at),[700,600,500,400,300]);"
            "assert.strictEqual(C.runningVisible(rows,true).length,7);"
            "assert.strictEqual(C.runningVisible(rows.slice(0,3),false).length,3);"
            "assert.strictEqual(C.runningToggleLabel(12,false),'Show all 12 running agents');"
            "assert.strictEqual(C.runningToggleLabel(12,true),'Show top 5 agents');"
            "assert.ok(C.runningScopeKey('WS1')!==C.runningScopeKey('all'));"
            "assert.ok(C.runningScopeKey('all').indexOf('v2')>=0,'scope key lost its version');"
            "assert.strictEqual(C.readRunningExpanded('all'),false,'no stored state must read collapsed');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "core" / "disclosure.js"), str(STATIC / "cards" / "tables.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_disclosure_helper(self):
        """The one shared top-5 helper (static/core/disclosure.js on
        window.AdPage): discloseRows(rows, key, noun, limit=5) slices to
        the first 5 collapsed and everything expanded, default COLLAPSED
        even with an old-key expanded flag stored, per-list v2 keys in
        sessionStorage behind try/catch, and a render helper returning a
        real Show-all/Show-top-5 button (aria-expanded, aria-controls)
        or null at 5 or fewer."""
        script = (
            "const fs=require('fs');"
            "var store={};"
            "globalThis.window=globalThis;"
            "window.sessionStorage={"
            " getItem:function(k){return Object.prototype.hasOwnProperty.call(store,k)?store[k]:null;},"
            " setItem:function(k,v){store[k]=String(v);},"
            " removeItem:function(k){delete store[k];}};"
            "function mkEl(tag,attrs){var e={tag:tag,attrs:{},kids:[],textContent:'',"
            " setAttribute:function(k,v){this.attrs[k]=v;},"
            " appendChild:function(c){this.kids.push(c);return c;},"
            " addEventListener:function(t,f){this._on=(this._on||{});this._on[t]=f;}};"
            " if(attrs)for(var k in attrs){if(k==='text')e.textContent=attrs[k];else e.setAttribute(k,attrs[k]);}"
            " return e;}"
            "var el=mkEl;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const assert=require('assert');"
            "assert.strictEqual(AdPage.discloseTOP_N,5);"
            "assert.ok(discloseKey('x').indexOf('v2')>=0,'key lost its version');"
            "var rows=[1,2,3,4,5,6,7];"
            "var collapsed=discloseRows(rows,'t5a','agents');"
            "assert.deepStrictEqual(collapsed.rows,[1,2,3,4,5]);"
            "assert.strictEqual(collapsed.total,7);"
            "assert.strictEqual(collapsed.showAll,false);"
            "assert.strictEqual(discloseSlice(rows,false).length,5);"
            "assert.strictEqual(discloseSlice(rows,true).length,7);"
            "assert.strictEqual(discloseSlice([1,2],false).length,2);"
            "assert.strictEqual(discloseLabel(7,false,'agents',5),'Show all 7 agents');"
            "assert.strictEqual(discloseLabel(7,true,'agents',5),'Show top 5 agents');"
            "assert.strictEqual(discloseButton(discloseRows([1,2],'t5b','agents'),'t',function(){}),null);"
            "var btn=discloseButton(collapsed,'rows-id',function(){});"
            "assert.strictEqual(btn.tag,'button');"
            "assert.strictEqual(btn.textContent,'Show all 7 agents');"
            "assert.strictEqual(btn.attrs['aria-expanded'],'false');"
            "assert.strictEqual(btn.attrs['aria-controls'],'rows-id');"
            "btn._on.click();"
            "var expanded=discloseRows(rows,'t5a','agents');"
            "assert.strictEqual(expanded.showAll,true);"
            "assert.strictEqual(expanded.rows.length,7);"
            "var btn2=discloseButton(expanded,'rows-id',function(){});"
            "assert.strictEqual(btn2.textContent,'Show top 5 agents');"
            "assert.strictEqual(btn2.attrs['aria-expanded'],'true');"
            "btn2._on.click();"
            "assert.strictEqual(discloseRows(rows,'t5a','agents').showAll,false);"
            "store['agent-dash-running:all']='all';"
            "assert.strictEqual(readDisclosed('running:all'),false,'the pre-top-5 expanded flag applies');"
            "writeDisclosed('running:all',true);"
            "assert.strictEqual(readDisclosed('running:all'),true);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "core" / "disclosure.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        src = (STATIC / "core" / "disclosure.js").read_text()
        for marker in ("try", "catch", "sessionStorage"):
            self.assertIn(marker, src, f"the helper touches storage without {marker}")
        page = (STATIC / "index.html").read_text()
        self.assertIn('<script src="/static/core/disclosure.js"></script>', page, "index.html never loads the helper")
        self.assertIn('<link rel="stylesheet" href="/static/core/disclosure.css">', page, "index.html never loads the helper CSS")

    def test_outcome_tokens_split_blocked_and_died(self):
        """Blocked and died resolve to different outcome tokens, glyphs
        and words everywhere outcomes are coloured (the one ADM.outcomeOf
        in models.js, mirrored by the server): blocked is serious
        orange with ⊘, died is critical red with ✕, both colour-blind
        safe next to partial amber and pass green. Cancelled is grey,
        stalled amber-ish with its own glyph and word, unknown grey
        hollow with 'no verdict'."""
        for name in ("cards/timeline.js", "cards/plan-progress.js"):
            src = (STATIC / name).read_text()
            self.assertNotIn("died_early", src, f"{name} keeps its own outcome if-chain")
        self.assertIn(
            "ADM.outcomeOf", (STATIC / "cards" / "timeline.js").read_text(),
            "timeline.js no longer reads the one class function",
        )
        script = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "const assert=require('assert');"
            "assert.ok(OUTCOME.blocked[0]!==OUTCOME.died[0],'blocked and died share one colour');"
            "assert.strictEqual(OUTCOME.blocked[0],'var(--serious)');"
            "assert.strictEqual(OUTCOME.died[0],'var(--critical)');"
            "assert.ok(OUTCOME.blocked[1]!==OUTCOME.died[1],'blocked and died share one glyph');"
            "assert.strictEqual(OUTCOME.blocked[1],'\\u2298');"
            "assert.strictEqual(OUTCOME.died[1],'\\u2715');"
            "assert.ok(OUTCOME.blocked[2]!==OUTCOME.died[2],'blocked and died share one word');"
            "assert.strictEqual(OUTCOME.part[0],'var(--warning)');"
            "assert.strictEqual(OUTCOME.ok[0],'var(--good)');"
            "assert.strictEqual(OUTCOME.cancelled[0],'var(--muted)');"
            "assert.strictEqual(OUTCOME.cancelled[1],'\\u23F9');"
            "assert.strictEqual(OUTCOME.unk[1],'\\u25CB');"
            "assert.strictEqual(OUTCOME.unk[2],'no verdict');"
            "assert.strictEqual(OUTCOME.stalled[1],'\\u25D4');"
            "function run(o){return Object.assign({ended_at:1700000100,status:'done',verdict:null},o||{});}"
            "assert.strictEqual(outcomeOf(run({status:'killed'})),'died');"
            "assert.strictEqual(outcomeOf(run({status:'died_early'})),'died');"
            "assert.strictEqual(outcomeOf(run({verdict:'FAILURE'})),'died');"
            "assert.strictEqual(outcomeOf(run({verdict:'FAIL'})),'died');"
            "assert.strictEqual(outcomeOf(run({verdict:'QUOTA'})),'died');"
            "assert.strictEqual(outcomeOf(run({verdict:'CANCELED'})),'cancelled');"
            "assert.strictEqual(outcomeOf(run({status:'killed',verdict:'BLOCKED'})),'died');"
            "assert.strictEqual(outcomeOf(run({verdict:'BLOCKED'})),'blocked');"
            "assert.strictEqual(outcomeOf(run({verdict:'REQUEST_CHANGES'})),'blocked');"
            "assert.strictEqual(outcomeOf(run({verdict:'FIX'})),'blocked');"
            "assert.strictEqual(outcomeOf(run({verdict:'PARTIAL'})),'part');"
            "assert.strictEqual(outcomeOf(run({verdict:'APPROVE_WITH_CHANGES'})),'part');"
            "assert.strictEqual(outcomeOf(run({verdict:'PASS'})),'ok');"
            "assert.strictEqual(outcomeOf({status:'running'}) ,'run');"
            "assert.strictEqual(outcomeOf({status:'running',stalled:3600}) ,'stalled');"
            "assert.strictEqual(outcomeColor(run({verdict:'BLOCKED'})),'var(--serious)');"
            "assert.strictEqual(outcomeColor(run({status:'killed'})),'var(--critical)');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "models.js"), str(STATIC / "cards" / "timeline.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_verdict_pills_split_blocked_and_died(self):
        """The run-row verdict pills never paint blocked like died:
        BLOCKED reads ⊘ blocked and REQUEST_CHANGES ↻ changes asked on
        the serious-orange blocked pill; died stays the run status."""
        script = (
            "const fs=require('fs');"
            "globalThis.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "const assert=require('assert');"
            "assert.deepStrictEqual(verdictPill('BLOCKED'),['blocked','\\u2298 blocked']);"
            "assert.deepStrictEqual(verdictPill('REQUEST_CHANGES'),['blocked','\\u21BB changes asked']);"
            "assert.deepStrictEqual(verdictPill('PARTIAL'),['part','\\u25D0 partial']);"
            "assert.deepStrictEqual(verdictPill('APPROVE_WITH_CHANGES'),['part','\\u25D0 partial']);"
            "assert.deepStrictEqual(verdictPill('PASS'),['pass','\\u2713 pass']);"
            "assert.deepStrictEqual(verdictPill('CANCELED'),['cancelled','\\u23F9 cancelled']);"
            "assert.deepStrictEqual(verdictPill('FAILURE'),['died','\\u2715 failed']);"
            "assert.strictEqual(typeof verdictEl,'function','the drawer pill helper is not exported');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "core" / "disclosure.js"), str(STATIC / "cards" / "tables.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        css = (STATIC / "cards" / "tables.css").read_text()
        self.assertIn(".vpill.blocked", css, "the blocked pill has no style")
        self.assertIn("serious", css, "the blocked pill does not use the serious token")
        self.assertNotIn(".vpill.rev", css, "the dead rev pill style is back")

    def test_top5_caps_across_lists(self):
        """Every capped list renders through the shared disclosure
        helper; the Finished pager stays at 10; the unmapped-running
        strip is gone everywhere."""
        manage = (STATIC / "core" / "manage.js").read_text()
        self.assertIn('discloseRows(tableRows(), "manage:current", "workstreams")', manage)
        self.assertIn('id: "mg-rows"', manage)
        self.assertIn('"data-mg-more"', manage)
        self.assertIn('"Select all " + rows.length + " workstreams in this filter"', manage)
        planned = (STATIC / "cards" / "planned.js").read_text()
        self.assertIn('discloseRows(rows, "planned:readiness", "workstreams")', planned)
        self.assertIn('discloseRows(held, "planned:gated", "workstreams")', planned)
        archive = (STATIC / "cards" / "archive.js").read_text()
        self.assertIn('discloseRows(cards, "archive:shelf", "archived workstreams")', archive)
        home = (STATIC / "cards" / "project-home.js").read_text()
        self.assertIn('discloseRows(rows, "home:planned", "planned workstreams")', home)
        self.assertIn('discloseRows(rows, "home:unplanned", "unplanned workstreams")', home)
        wscards = (STATIC / "cards" / "ws-cards.js").read_text()
        self.assertIn('discloseRows(rows, capKey, "workstreams")', wscards)
        self.assertNotIn("prog-more", wscards, "the old Where-we-are toggle is back")
        decisions = (STATIC / "cards" / "decisions.js").read_text()
        self.assertIn('discloseRows(shown, "decisions", "decisions")', decisions)
        self.assertNotIn("decExpanded", decisions, "the old decisions expander is back")
        phases = (STATIC / "cards" / "phases.js").read_text()
        self.assertIn('discloseRows(items, "phase-usage", "workstreams")', phases)
        for path in ("cards/tables.js", "core/manage.js", "cards/planned.js",
                     "cards/archive.js", "cards/project-home.js",
                     "cards/ws-cards.js", "cards/decisions.js",
                     "cards/phases.js", "core/drawer.js", "plan.js"):
            self.assertNotIn("not mapped to a task", (STATIC / path).read_text(), f"{path} still names the strip")
        for path in ("cards/plan-progress.css", "cards/ws-cards.css",
                     "cards/project-home.css", "cards/decisions.css"):
            self.assertNotIn(".unassigned", (STATIC / path).read_text(), f"{path} keeps dead strip CSS")
            self.assertNotIn("prog-more", (STATIC / path).read_text(), f"{path} keeps dead toggle CSS")

    def test_where_we_are_headline_sums_every_row(self):
        """The Where-we-are hero ("N of M tasks done across W workstreams")
        sums every panel row, never the capped visible slice: collapsing to
        the top 5 must not move the headline."""
        wscards = (STATIC / "cards" / "ws-cards.js").read_text()
        panel = wscards[wscards.index("function programPanel"):]
        panel = panel[:panel.index("window.DashProgram")]
        cap_at = panel.index('discloseRows(rows, capKey, "workstreams")')
        total_at = panel.index("all.done +=")
        self.assertLess(total_at, cap_at, "the hero sums the capped slice, not every row")
        shown_loop = panel[panel.index("info.rows.forEach"):]
        shown_loop = shown_loop[:shown_loop.index("var more = discloseButton")]
        self.assertNotIn("all.done", shown_loop, "the visible-slice loop still feeds the hero")
        self.assertNotIn("all.tot", shown_loop, "the visible-slice loop still feeds the hero")
        self.assertNotIn(".dc-more", (STATIC / "cards" / "decisions.css").read_text(), "dead dc-more CSS is back")
        self.assertNotIn(".vd {", (STATIC / "cards" / "tables.css").read_text(), "the dead drawer verdict style is back")

    def test_finished_lists_share_one_pager(self):
        """Every finished agents/runs list pages through the one shared
        footer (static/core/paginate.js on window.AdPage): the Finished
        (24 h) tables (Execution overall plus each workstream tab) and
        Recently done (Project home). Ten rows per page by default, real
        Prev/Next buttons with list-naming aria-labels, numbered pages with
        ellipsis, a 10/25/50 size selector persisted per list in
        sessionStorage behind try/catch, hidden at 10 or fewer rows, and a
        section key carrying page and size but no volatile values."""
        pager = STATIC / "core" / "paginate.js"
        self.assertTrue(pager.is_file(), "the shared pager module is missing")
        page = (STATIC / "index.html").read_text()
        self.assertIn(
            '<script src="/static/core/paginate.js"></script>',
            page,
            "index.html never loads the shared pager",
        )
        src = pager.read_text()
        tables = (STATIC / "cards" / "tables.js").read_text()
        registry = (STATIC / "cards" / "registry.js").read_text()
        home = (STATIC / "cards" / "project-home.js").read_text()
        css = (STATIC / "cards" / "tables.css").read_text()
        self.assertIn("resolveFinished(snap, scope)", registry, "the Finished table bypasses the pager")
        self.assertIn('renderPager("finished", "Finished agents"', registry)
        self.assertIn('redrawSection("finished")', registry)
        self.assertIn("page.page, page.size, scope", registry, "the finished key lost page/size/scope")
        self.assertIn("function runsTable(title, sub, runs, empty, pager)", tables)
        self.assertIn('pagerState("recently-done", "project")', home, "Recently done bypasses the pager")
        self.assertIn('renderPager("recently-done", "Recently done"', home)
        self.assertIn('redrawSection("project-done")', home)
        self.assertIn("pgSlice(rows", home)
        self.assertIn("shelfDoneCount()", home, "the project-done key lost its count")
        self.assertIn("DEFAULT_PAGE_SIZE = 10", src, "the default page size drifted off 10")
        self.assertIn("PAGE_SIZES = [10, 25, 50]", src)
        self.assertIn("agent-dash-pagesize:", src, "the size key lost its prefix")
        self.assertIn('el("button"', src, "the pager controls are not real <button>s")
        self.assertIn('"Previous page of " + listName', src, "Prev never names its list")
        self.assertIn('"Next page of " + listName', src, "Next never names its list")
        self.assertIn('"Page " + n + " of " + listName', src, "numbered pages never name their list")
        self.assertIn('"Rows per page for " + listName', src, "the size selector never names its list")
        self.assertIn('"Showing " + r.from', src, "the footer lost its Showing label")
        self.assertIn("if (pages <= 1) return null;", src, "the footer no longer hides on one page")
        self.assertIn('setAttribute("disabled"', src, "ends never disable Prev/Next")
        bodies = dict(functions(src))
        for name in ("readPageSize", "writePageSize"):
            self.assertIn(name, bodies, f"{name} is gone from paginate.js")
            for marker in ("try", "catch", "sessionStorage"):
                self.assertIn(marker, bodies[name], f"{name} touches storage unguarded")
        for marker in (".pg-foot", ".pg-label", ".pg-pages", ".pg-size"):
            self.assertIn(marker, css, f"the pager stylesheet lost {marker}")

    def test_pager_pure_helpers(self):
        """Slice, clamp, page windows with ellipsis, label text, totals and
        size persistence are pure functions on window.AdPage.paginate."""
        script = (
            "const fs=require('fs');"
            "var window={};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const P=window.AdPage.paginate;"
            "const assert=require('assert');"
            "assert.strictEqual(P.DEFAULT_PAGE_SIZE,10);"
            "assert.deepStrictEqual(P.PAGE_SIZES,[10,25,50]);"
            "assert.strictEqual(P.pgCount(35,10),4);"
            "assert.strictEqual(P.pgCount(0,10),1);"
            "assert.strictEqual(P.pgCount(10,10),1);"
            "assert.strictEqual(P.pgCount(11,10),2);"
            "assert.strictEqual(P.pgClamp(9,35,10),4);"
            "assert.strictEqual(P.pgClamp(0,35,10),1);"
            "assert.strictEqual(P.pgClamp(2,0,10),1);"
            "const rows=Array.from({length:35},(_,i)=>({run_id:'r'+i}));"
            "assert.deepStrictEqual(P.pgSlice(rows,1,10).map(r=>r.run_id),rows.slice(0,10).map(r=>r.run_id));"
            "assert.strictEqual(P.pgSlice(rows,4,10).length,5);"
            "assert.strictEqual(P.pgSlice(rows,9,10).length,5);"
            "assert.deepStrictEqual(P.pgSlice(null,1,10),[]);"
            "assert.deepStrictEqual(P.pgWindow(1,1),[1]);"
            "assert.deepStrictEqual(P.pgWindow(2,4),[1,2,3,4]);"
            "assert.deepStrictEqual(P.pgWindow(1,7),[1,2,3,4,5,6,7]);"
            "assert.deepStrictEqual(P.pgWindow(1,20),[1,2,'…',20]);"
            "assert.deepStrictEqual(P.pgWindow(2,20),[1,2,3,'…',20]);"
            "assert.deepStrictEqual(P.pgWindow(10,20),[1,'…',9,10,11,'…',20]);"
            "assert.deepStrictEqual(P.pgWindow(19,20),[1,'…',18,19,20]);"
            "assert.deepStrictEqual(P.pgWindow(20,20),[1,'…',19,20]);"
            "assert.deepStrictEqual(P.pgWindow(4,8),[1,'…',3,4,5,'…',8]);"
            "assert.deepStrictEqual(P.pgRange(2,10,35),{from:11,to:20});"
            "assert.deepStrictEqual(P.pgRange(4,10,35),{from:31,to:35});"
            "assert.deepStrictEqual(P.pgRange(1,10,0),{from:0,to:0});"
            "assert.strictEqual(P.pgLabel(1,10,35),'Showing 1-10 of 35');"
            "assert.strictEqual(P.pgLabel(2,10,35),'Showing 11-20 of 35');"
            "assert.strictEqual(P.pgLabel(4,10,35),'Showing 31-35 of 35');"
            "assert.strictEqual(P.finishedTotal({recent:[1,2],recent_total:35,counts:{finished_in_range:35}}),35);"
            "assert.strictEqual(P.finishedTotal({recent:[1,2],counts:{finished_in_range:7}}),7);"
            "assert.strictEqual(P.finishedTotal({recent:[1,2]}),2);"
            "assert.strictEqual(P.pagerState('finished','all').size,10);"
            "assert.strictEqual(P.pagerState('finished','all').page,1);"
            "assert.ok(P.pagerKey('finished','WS1')!==P.pagerKey('finished','all'));"
            "var h={recent:rows,recent_total:35,counts:{finished_in_range:35}};"
            "var r=P.resolveFinished(h,'all');"
            "assert.strictEqual(r.rows.length,10);"
            "assert.strictEqual(r.total,35);"
            "assert.strictEqual(r.pending,false);"
            "var store={};window.sessionStorage={getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}};"
            "P.writePageSize('finished',25);"
            "P.resetPager('finished','all');"
            "assert.strictEqual(P.pagerState('finished','all').size,25);"
            "P.resetPager('finished','WS9');"
            "assert.strictEqual(P.pagerState('finished','WS9').size,25);"
            "window.sessionStorage={getItem(){throw new Error('denied')},setItem(){throw new Error('denied')}};"
            "P.resetPager('finished','all');"
            "assert.strictEqual(P.pagerState('finished','all').size,10);"
            "assert.strictEqual(P.readPageSize('finished'),10);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "core" / "paginate.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_group_runs_ordering(self):
        """The grouping builder is pure and exported
        (window.AdPage.cards.groupRuns): rows group workstream -> section
        -> rows with counts, sections in plan order with Unsectioned last,
        rows newest-first inside a section, mode "none" flat. The collapsed
        slice keeps the first 5 agents most-recently-active-first with the
        freshest section on top; expanded returns the plan order untouched.
        The Group by toggle defaults to Section and survives denied
        storage."""
        script = (
            "const fs=require('fs');"
            "var window={};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const C=window.AdPage.cards;"
            "const assert=require('assert');"
            "assert.strictEqual(typeof C.groupRuns,'function');"
            "assert.strictEqual(typeof C.visibleGroups,'function');"
            "const rows=["
            "{run_id:'r1',workstream:'WS9',label:'ws9-b1-a',section:'Beta',section_index:1,started_at:300},"
            "{run_id:'r2',workstream:'WS9',label:'ws9-a1-a',section:'Alpha',section_index:0,started_at:100},"
            "{run_id:'r3',workstream:'WS9',label:'ws9-a2-a',section:'Alpha',section_index:0,started_at:200},"
            "{run_id:'r4',workstream:'WS9',label:'ws9-zzz',section:null,section_index:null,started_at:400}];"
            "const g=C.groupRuns(rows,['Alpha','Beta'],'section');"
            "assert.strictEqual(g.length,1);"
            "assert.strictEqual(g[0].ws,'WS9');"
            "assert.deepStrictEqual(g[0].sections.map(s=>s.name),['Alpha','Beta',null]);"
            "assert.deepStrictEqual(g[0].sections.map(s=>s.count),[2,1,1]);"
            "assert.deepStrictEqual(g[0].sections[0].rows.map(r=>r.run_id),['r3','r2']);"
            "const g2=C.groupRuns(rows,null,'section');"
            "assert.deepStrictEqual(g2[0].sections.map(s=>s.name),['Alpha','Beta',null]);"
            "const g3=C.groupRuns(rows,{WS9:['Beta','Alpha']},'section');"
            "assert.deepStrictEqual(g3[0].sections.map(s=>s.name),['Beta','Alpha',null]);"
            "const flat=C.groupRuns(rows,['Alpha','Beta'],'none');"
            "assert.strictEqual(flat[0].sections.length,1);"
            "assert.strictEqual(flat[0].sections[0].name,null);"
            "assert.deepStrictEqual(flat[0].sections[0].rows.map(r=>r.run_id),['r4','r1','r3','r2']);"
            "const multi=C.groupRuns(rows.concat([{run_id:'r5',workstream:'WS8',label:'ws8-x',section:'S',section_index:0,started_at:900}]),null,'section');"
            "assert.deepStrictEqual(multi.map(w=>w.ws),['WS8','WS9']);"
            "const vis=C.visibleGroups(g,false,2);"
            "assert.deepStrictEqual(vis[0].sections.map(s=>s.name),[null,'Beta']);"
            "assert.deepStrictEqual(vis[0].sections.map(s=>s.count),[1,1]);"
            "assert.strictEqual(vis[0].sections[0].rows[0].run_id,'r4');"
            "assert.deepStrictEqual(C.visibleGroups(g,true,2),g);"
            "var store={};window.sessionStorage={getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}};"
            "assert.strictEqual(C.readGroupMode('running','all'),'section');"
            "C.writeGroupMode('running','all','none');"
            "assert.strictEqual(C.readGroupMode('running','all'),'none');"
            "C.writeGroupMode('running','all','section');"
            "assert.strictEqual(C.readGroupMode('running','all'),'section');"
            "window.sessionStorage={getItem(){throw new Error('denied')},setItem(){throw new Error('denied')},removeItem(){throw new Error('denied')}};"
            "assert.strictEqual(C.readGroupMode('running','all'),'section');"
            "C.writeGroupMode('running','all','none');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "cards" / "tables.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_plan_sections_term_rule(self):
        """Plan task groupings are SECTIONS on the page: no string in
        static/cards/*.js or static/*.js (excluding phases.js, the
        planning-vs-execution usage card) says "phase"/"phases" for plan
        sections. Data-key identifiers (narrative.phases, phases_line,
        .phases reads, the kept CSS section classes) and the genuinely
        serial project.json phases strip stay, each on an explicit
        allow-list; DESIGN.md states the term."""
        word = re.compile(r"phase", re.IGNORECASE)
        allow = [
            re.compile(r"\.phases\b"),
            re.compile(r"phases_line"),
            re.compile(r"['\"]phases['\"]"),
            re.compile(r"\bphases:"),
            re.compile(r"phases\s*\|\|"),
            re.compile(r"phases\s*\.?(forEach|length)"),
            re.compile(r"phases-(wrap|cap|line)"),
            re.compile(r'"phase"|pj-phase'),
            re.compile(r"phasesSection|project-phases"),
            re.compile(r"exec-phases|phaseUsage|Usage by phase|phases-ws|phases-all"),
            re.compile(r"never phases|phase-sequence|phase sequence|\"phases\" misleads|same groups and phases|"
                       r"phases strip from /api/project|Phases strip from /api/project|mockup order: phases"),
            re.compile(r"From project\.json|No phases listed|its phases|\"Phases\"|\|\| \"Phase\""),
        ]
        files = sorted((STATIC / "cards").glob("*.js")) + sorted(STATIC.glob("*.js"))
        bad = []
        for path in files:
            if path.name == "phases.js":
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if not word.search(line):
                    continue
                if any(rx.search(line) for rx in allow):
                    continue
                bad.append(f"{path.name}:{lineno}: {line.strip()[:160]}")
        self.assertEqual(bad, [], "phase-wording outside the allow-list:\n" + "\n".join(bad))
        for path in files:
            if path.name == "phases.js":
                continue
            self.assertNotIn("Work groups", path.read_text(), f"{path.name} still says Work groups")
        design = (STATIC.parent / "DESIGN.md").read_text()
        for kept in ("SECTIONS", "sections run in parallel", '"Sections" label', "Machine keys"):
            self.assertIn(kept, design, f"DESIGN.md no longer states the sections term ({kept})")


TABLE_FILES = [
    STATIC / "cards" / "tables.js",
    STATIC / "core" / "manage.js",
    STATIC / "cards" / "planned.js",
    STATIC / "cards" / "archive.js",
]
TABLE_CSS = [
    STATIC / "tokens.css",
    STATIC / "cards" / "tables.css",
    STATIC / "cards" / "manage.css",
    STATIC / "cards" / "planned.css",
]
COLUMN_MODIFIERS = ("c-text", "c-label", "c-num", "c-status", "c-time", "c-actions", "c-check", "c-key")


class TableColumnTests(unittest.TestCase):
    def test_tables_share_one_compact_style(self):
        """Every data table uses the shared .dt base class, and tokens.css
        defines the base plus every column modifier once."""
        tokens = (STATIC / "tokens.css").read_text()
        self.assertIn("table.dt {", tokens, "the shared .dt base is gone from tokens.css")
        for mod in COLUMN_MODIFIERS:
            self.assertIn("." + mod, tokens, f"tokens.css never defines .{mod}")
        for path in TABLE_FILES:
            src = path.read_text()
            creations = re.findall(r'el\("table",\s*\{([^}]*)\}', src)
            self.assertGreater(creations, [], f"{path.name} builds no tables")
            for attrs in creations:
                m = re.search(r'class:\s*"([^"]*)"', attrs)
                self.assertIsNotNone(m, f"{path.name} builds a table with no class: {attrs[:80]}")
                self.assertIn("dt", m.group(1).split(),
                              f"{path.name} builds a table outside the shared .dt style: {attrs[:80]}")

    def test_number_cells_use_c_num(self):
        """Number cells carry .c-num (tokCell's legacy td.n class is styled
        through the shared .dt td.n alias, never a bare number style)."""
        for path in TABLE_FILES:
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for attrs in re.findall(r'el\("td",\s*\{([^}]*)\}', line):
                    m = re.search(r'class:\s*"([^"]*)"', attrs)
                    if m and re.search(r"(?:^|\s)n(?:\s|$)", m.group(1)):
                        self.assertIn("c-num", m.group(1).split(),
                                      f"{path.name}:{lineno} numbers a cell without .c-num")
        tokens = (STATIC / "tokens.css").read_text()
        self.assertIn("table.dt td.n", tokens, "the tokCell td.n alias is gone from the shared style")

    def test_label_columns_have_no_fixed_pixel_widths(self):
        """No per-table fixed pixel width on label/text columns: labels wrap
        (2-line clamp) instead of truncating at a pixel width."""
        labelish = re.compile(r"lbl|mg-title|c-label|c-text|dt-kv|c-key")
        for path in TABLE_CSS:
            for chunk in path.read_text().split("}"):
                if "{" not in chunk:
                    continue
                selector, _, body = chunk.rpartition("{")
                if not labelish.search(selector):
                    continue
                hits = re.findall(r"(?:max-|min-)?width:\s*\d+px", body)
                self.assertEqual(hits, [],
                                 f"{path.name} pins a label column to pixels: {selector.strip()[:80]}")

    def test_header_and_cell_alignment_classes_match(self):
        """Every th and td in the table builders carries a .c-* column
        class, so headers align with their column's content; the run-table
        header map names every header the tables render."""
        for path in TABLE_FILES:
            src = path.read_text()
            bare_td = [m for m in re.finditer(r'el\("td",\s*(null|\{text)', src)]
            self.assertEqual([m.group(0) for m in bare_td], [],
                             f"{path.name} renders class-less td cells")
            for m in re.finditer(r'el\("th",\s*\{([^}]*)\}', src):
                attrs = m.group(1)
                cm = re.search(r'class:\s*"([^"]*)"', attrs)
                if cm is None:
                    # The header row takes its class from a map (tables.js
                    # HEAD_CLASS) or an inline [name, class] pair list
                    # (manage.js, planned.js); the pair lists are checked
                    # for .c-* classes below.
                    self.assertTrue("HEAD_CLASS" in attrs or "pair[1]" in attrs,
                                    f"{path.name} renders a class-less th: {attrs[:80]}")
                    continue
                self.assertTrue(any(c in COLUMN_MODIFIERS or c == "hidephone" or c.startswith("mg-c-")
                                    for c in cm.group(1).split()),
                                f"{path.name} th misses its column class: {attrs[:80]}")
        tables = (STATIC / "cards" / "tables.js").read_text()
        for header in ("Label", "Model", "WS", "Section", "Elapsed", "Output", "Tokens", "Status"):
            self.assertIn(f"{header}:", tables.split("var HEAD_CLASS")[1].split("};")[0],
                          f"HEAD_CLASS never maps the {header} header to its column class")
        # The inline [name, class] pair lists name a .c-* class per header.
        for path in TABLE_FILES:
            for m in re.finditer(r'\[\s*"([A-Za-z? ]+)",\s*"(c-[a-z]+)"\s*\]', path.read_text()):
                self.assertIn(m.group(2), COLUMN_MODIFIERS,
                              f"{path.name} pairs a header with a non-column class: {m.group(0)[:80]}")


    def test_linear_link_and_sync_age_follow_page_rules(self):
        """The plan header Linear link is a real external link with an
        accessible name, the sync age is a muted line with the exact time
        in its title, both hide in clip mode, the new CSS uses tokens only,
        and the code names no doctor or health state and formats nothing
        with toFixed or toLocaleString."""
        import json as _json
        src = (STATIC / "cards" / "plan-progress.js").read_text()
        body = dict(functions(src))["linearHeader"]
        for needle in ('el("a"', 'class: "linear-link"', 'target: "_blank"', 'rel: "noopener"', '"aria-label": "Open "', 'class: "linear-sync"'):
            self.assertIn(needle, body, f"linearHeader lost {needle}")
        for word in ("doctor", "health"):
            self.assertNotIn(word, body.lower(), f"linearHeader carries a {word!r} word")
        for bad in (".toFixed(", "toLocaleString"):
            self.assertNotIn(bad, body, f"linearHeader formats with {bad}")
        css = (STATIC / "cards" / "plan-progress.css").read_text()
        clip = [line for line in css.splitlines() if "body.clip #exec-progress .linear-link" in line]
        self.assertTrue(clip, "the Linear link is not hidden in clip mode")
        self.assertIn("body.clip #exec-progress .linear-sync", clip[0])
        self.assertIn("display:none", clip[0])
        tokens = (STATIC / "tokens.css").read_text()
        for name in set(re.findall(r"var\((--[\w-]+)\)", "\n".join(l for l in css.splitlines() if "linear-" in l))):
            self.assertIn(f"{name}:", tokens, f"{name} is not a tokens.css token")
        ws = (STATIC / "cards" / "ws-cards.js").read_text()
        self.assertIn('class: "linear-link"', ws, "the Where-we-are row has no Linear link")
        self.assertIn("planLinear[w.ws]", ws, "the Where-we-are row reads no linear field")
        harness = (
            "const fs=require('fs');const assert=require('assert');"
            "globalThis.window=globalThis;"
            "globalThis.location={search:'',pathname:'/'};"
            "function fakeNode(tag){"
            " var n={tag:tag,attrs:{},kids:[],textContent:'',_on:{},"
            "  setAttribute:function(k,v){this.attrs[k]=v;},"
            "  appendChild:function(c){this.kids.push(c);return c;},"
            "  addEventListener:function(t,f){this._on[t]=f;}};"
            " return n;}"
            "globalThis.document={createElement:fakeNode,getElementById:function(){return null;},querySelector:function(){return null;}};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "var fmtDur=AD.fmtDur,fmtDate=AD.fmtDate;"
            "var state={wss:[{ws:'WS1',name:'Identity'}]};"
            "eval(" + _json.dumps(body) + ");"
            "assert.deepStrictEqual(linearHeader({ws:'WS1'},1761999960),[]);"
            "var a=linearHeader({ws:'WS1',linear:{url:'https://linear.app/example/project/notes-cutover',synced_at:1761999720}},1761999960);"
            "assert.strictEqual(a.length,2);"
            "assert.strictEqual(a[0].tag,'a');"
            "assert.strictEqual(a[0].attrs.target,'_blank');"
            "assert.strictEqual(a[0].attrs.rel,'noopener');"
            "assert.strictEqual(a[0].attrs['aria-label'],'Open Identity in Linear');"
            "assert.strictEqual(a[0].attrs.href,'https://linear.app/example/project/notes-cutover');"
            "assert.strictEqual(a[1].textContent,'synced 4m 0s ago');"
            "assert.strictEqual(a[1].attrs.title,AD.fmtDate(1761999720));"
            "var n=linearHeader({ws:'WS1',linear:{url:'https://linear.app/x',synced_at:null}},1761999960);"
            "assert.strictEqual(n[1].textContent,'not synced yet');"
        )
        proc = subprocess.run(
            ["node", "-e", harness, str(STATIC / "format.js"), str(STATIC / "core" / "el.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
