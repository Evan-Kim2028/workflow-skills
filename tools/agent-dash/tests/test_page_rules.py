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
        """The v2 charts carry raw values for agents: the token legend
        entries have data-key/data-v, the bar samples data-t/data-v, the
        peak bar a direct label; every live strip has its own y axis, a
        gradient fill and a pk annotation, and an empty window reads
        "No samples in this window", never a silent zero or a failure."""
        script = inline_scripts()
        bodies = dict(functions(script))
        self.assertIn("tokenChart", bodies)
        tok = bodies["tokenChart"]
        for needle in ("data-key", "data-v", "data-t", "peaklab", "data-legend-total"):
            self.assertIn(needle, tok, f"tokenChart lost {needle!r}")
        self.assertIn("concChart", bodies)
        conc = bodies["concChart"]
        for needle in ("data-fam", "data-peak", "pk ", "linearGradient", "No samples in this window", "data-failed-scope", "request failed"):
            self.assertIn(needle, conc, f"concChart lost {needle!r}")

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
        """The timeline card keeps its integration points: the axis comes
        from the shared helpers, every bar carries its run id and outcome,
        and numbers stay compact."""
        src = (STATIC / "cards" / "timeline.js").read_text()
        for token in ("axes(", "timeTicks(", "data-run-id", "data-outcome"):
            self.assertIn(token, src, f"timeline.js lost {token}")
        for bad in (".toFixed(", "toLocaleString()", '"dagster"', "'dagster'"):
            self.assertNotIn(bad, src, f"timeline.js must not contain {bad}")

    def test_timeline_draws_every_run(self):
        """The run timeline draws every run in the window, never a sample:
        the real gantt() renders one bar per run (data-run-id) of a
        generated 640-run payload, every bar carries a data-outcome, the
        lane labels add up to the payload, and a >400-row lane becomes a
        labelled dense strip that still expands to every bar."""
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
["gantt", "isAgent", "runEnd", "packRows", "outcomeOf", "outcomeColor"].forEach(function (n) {
  assert.strictEqual(typeof global[n], "function", n + " is not exported");
});
assert.strictEqual(typeof global.expanded, "object", "expanded lane state is not exported");
assert.deepStrictEqual(Object.keys(OUTCOME).sort(), ["bad", "ok", "part", "run", "unk"]);
function walk(node, out) {
  out.push(node);
  (node.children || []).forEach(function (c) { if (c && c.children) walk(c, out); });
}
function render() {
  var nodes = [];
  walk(gantt(), nodes);
  return nodes;
}
function barsOf(nodes) {
  return nodes.filter(function (n) { return n.tag === "rect" && n.attrs["data-run-id"] !== undefined; });
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
["run", "ok", "part", "bad", "unk"].forEach(function (o) {
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
["\u25CF running", "\u2713 finished", "\u25D0 partial", "\u2715 died or blocked", "? ended, outcome unknown"].forEach(function (want) {
  assert.ok(legendText.indexOf(want) >= 0, "legend never says " + want);
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
console.log("timeline every-run proof ok: 640 + 505 + 640 bars");
'''
        files = [
            STATIC / "models.js",
            STATIC / "format.js",
            STATIC / "core" / "el.js",
            STATIC / "core" / "charts.js",
            STATIC / "cards" / "timeline.js",
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
            "globalThis.document={createElement:fakeNode,getElementById:function(){return null;}};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "eval(fs.readFileSync(process.argv[2],'utf8'));"
            "eval(fs.readFileSync(process.argv[3],'utf8'));"
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
            "assert.strictEqual(byClass(dcard,'dc-more').length,0);"
            "var many=[];for(var i=0;i<13;i++)many.push({id:'d'+i,title:'Row '+i,status:'approved',owner:'o',date:'2026-04-02',workstream:'WS9',area:'a'});"
            "state.decisions={decisions:many,counts:{approved:13},source:{present:true}};"
            "var big=decisionsCard();"
            "assert.strictEqual(byClass(big,'dc-row').length,12);"
            "assert.strictEqual(byClass(big,'dc-more').length,1);"
        )
        proc = subprocess.run(
            ["node", "-e", harness,
             str(STATIC / "format.js"), str(STATIC / "core" / "el.js"),
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
        tl = (STATIC / "cards" / "timeline.js").read_text()
        for needle in ('role: "button"', 'tabindex: "0"', '"aria-label"', '"keydown"'):
            self.assertIn(needle, tl, f"timeline.js lost {needle}")

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
        tok = (STATIC / "cards" / "token-chart.js").read_text()
        self.assertIn('role: "img"', tok)
        self.assertIn("total, peak", tok)
        burn = (STATIC / "cards" / "burnup.js").read_text()
        self.assertIn('"aria-label": label', burn)
        self.assertIn("recorded", burn)
        self.assertIn("total, peak", burn)
        tl = (STATIC / "cards" / "timeline.js").read_text()
        self.assertIn('"aria-label": "Agent runs over the last 24 hours', tl)
        self.assertIn("died or blocked, ", tl)
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
        live = (STATIC / "cards" / "live-chart.js").read_text()
        self.assertIn("No live agents in this window.", live)
        tiles = (STATIC / "cards" / "tiles.js").read_text()
        self.assertIn("(passN + part + bad)", tiles)

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

    def test_exec_section_order_tokens_then_timeline(self):
        """The run timeline sits directly below Tokens per hour: on both
        exec scopes the registry order is ... tokens, gantt, conc,
        finished, with every other section unchanged relative to the
        rest. The sidenav derives its order from the DOM, so no pin
        there; the page comment and DESIGN.md name the same order."""
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
            ["tiles", "running", "plan", "tokens", "gantt", "conc", "finished"],
            "exec order drifted: want tokens, then gantt, then conc, then finished",
        )
        page = (STATIC / "index.html").read_text()
        self.assertIn("tokens, gantt, conc, finished", page)
        design = (STATIC.parent / "DESIGN.md").read_text()
        self.assertIn("run timeline directly below it", design)
        self.assertLess(
            design.index("run timeline directly below it"),
            design.index("Finished (24 h)"),
            "DESIGN.md no longer lists the timeline before Finished",
        )
        sidenav = (STATIC / "sidenav.js").read_text()
        self.assertNotIn('"gantt"', sidenav, "the sidenav hard-codes a section order")
        self.assertNotIn('"conc"', sidenav, "the sidenav hard-codes a section order")

    def test_running_now_disclosure(self):
        """Running now shows the top agents with a progressive disclosure
        button: a real <button> with aria-expanded and aria-controls over
        the extra rows, a cap constant of 5, sessionStorage guarded by
        try/catch, the full count in the header, and no button at 5 or
        fewer. The data-nav label stays "Running now" for the sidenav."""
        tables = (STATIC / "cards" / "tables.js").read_text()
        self.assertIn("RUNNING_TOP_N = 5", tables, "the top-5 cap constant drifted")
        self.assertIn('el("button"', tables, "the disclosure is not a real <button>")
        for marker in ("aria-expanded", "aria-controls", "exec-running-extra"):
            self.assertIn(marker, tables, f"the disclosure lost {marker}")
        self.assertIn('"Show all " + total + " running agents"', tables)
        self.assertIn('"Show top " + RUNNING_TOP_N + " agents"', tables)
        self.assertIn('title + " \\u00B7 " + runs.length', tables, "the header lost its full count")
        self.assertIn('"data-nav": title', tables, "the data-nav label must stay the plain title")
        self.assertIn("rows.length > RUNNING_TOP_N", tables, "the button must hide at 5 or fewer")
        self.assertIn('setAttribute("hidden"', tables)
        self.assertIn('removeAttribute("hidden"', tables)
        bodies = dict(functions(tables))
        self.assertIn("agents", bodies["runningToggleLabel"], "the toggle label must name agents")
        for name in ("readRunningExpanded", "writeRunningExpanded"):
            self.assertIn(name, bodies, f"{name} is gone from tables.js")
            self.assertIn("try", bodies[name], f"{name} touches storage unguarded")
            self.assertIn("catch", bodies[name], f"{name} touches storage unguarded")
            self.assertIn("sessionStorage", bodies[name], f"{name} no longer uses sessionStorage")
        self.assertIn("agent-dash-running:", tables, "the storage key lost its per-scope prefix")

    def test_running_visible_helpers(self):
        """The top-5 slicing and label text are pure functions on
        window.AdPage.cards: newest started_at first (stable on ties),
        top 5 collapsed, all rows expanded, agent-naming labels."""
        script = (
            "const fs=require('fs');"
            "var window={};"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
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
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "cards" / "tables.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)



if __name__ == "__main__":
    unittest.main()
