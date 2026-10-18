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


def _extract_taskof(path, name):
    """The page's taskOf source with its name swapped, so node can run it
    against the same samples as dash/taskmatch.py. taskOf lives in a card
    file now, so fall back to the card scripts in script-tag order."""
    text = Path(path).read_text()
    if "function taskOf(" not in text:
        for cand in _card_scripts():
            if "function taskOf(" in cand:
                text = cand
                break
    start = text.index("function taskOf(")
    brace = text.index("{", start)
    depth = 0
    for pos in range(brace, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[start:pos + 1].replace("function taskOf(", f"function {name}(", 1)
    raise AssertionError(f"taskOf body never closes in {path}")


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
