"""Static rules for the dashboard pages: every chart has axes, every number is compact."""
import os
import subprocess

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import re
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


def inline_scripts():
    """The executable script of static/index.html (external src files excluded)."""
    text = (STATIC / "index.html").read_text()
    return "\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))


class PageRules(unittest.TestCase):
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
        scope and range. They never re-sum usage buckets on the client."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tiles", bodies)
        body = bodies["tiles"]
        self.assertNotIn("state.usage", body, "tiles() re-sums client buckets; read snap.totals")
        self.assertIn("snap.totals", body)
        self.assertIn("totals_all_time", body)
        self.assertIn("data-tokens", body)

    def test_legend_total_matches_the_tile(self):
        """The chart legend Total is the snapshot total, with data attributes
        for the browser check and a visible cross line when the chart drifts."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tokenChart", bodies)
        body = bodies["tokenChart"]
        self.assertIn("snap.totals", body)
        self.assertIn("data-legend-total", body)
        self.assertIn("data-chart-sum", body)
        self.assertIn("xline", body)
        self.assertIn("--warning", body)

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
        assigns state.snap and state.usage together under the tab+range+group
        guard, and so does every other writer."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("refreshSnapshot", bodies)
        body = bodies["refreshSnapshot"]
        self.assertIn("state.snap =", body)
        self.assertIn("state.usage =", body)
        self.assertIn("ADC.applies", body, "refreshSnapshot lost the tab+range+group guard")
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
        self.assertIn("tokenChart", bodies)
        body = bodies["tokenChart"]
        self.assertIn("state.groupBy =", body)
        self.assertIn("refreshSnapshot", body)

    def test_cache_key_and_stale_guard(self):
        script = (
            "const fs=require('fs');"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const A=globalThis.ADC;"
            "const assert=require('assert');"
            "assert.strictEqual(A.key('WS2',6,'model'),'WS2|6|model');"
            "assert.strictEqual(A.key('all',12,'workstream'),'all|12|workstream');"
            "assert.notStrictEqual(A.key('WS2',6,'model'),A.key('WS2',12,'model'));"
            "assert.notStrictEqual(A.key('WS2',6,'model'),A.key('WS1',6,'model'));"
            "assert.notStrictEqual(A.key('all',12,'model'),A.key('all',12,'workstream'));"
            "assert.strictEqual(A.fresh({at:1000},31000),true);"
            "assert.strictEqual(A.fresh({at:1000},31001),false);"
            "assert.strictEqual(A.fresh(null,1000),false);"
            "assert.strictEqual(A.applies('WS2|6|model',{ws:'WS2',hours:6,groupBy:'model'}),true);"
            "assert.strictEqual(A.applies('WS2|12|model',{ws:'WS2',hours:6,groupBy:'model'}),false);"
            "assert.strictEqual(A.applies('all|6|model',{ws:'all',hours:6,groupBy:'workstream'}),false);"
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


if __name__ == "__main__":
    unittest.main()
