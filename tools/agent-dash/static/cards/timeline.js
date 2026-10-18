/* Page card: run timeline gantt.
   Renders one activity strip per workstream, expandable into packed
   lanes of run bars coloured by outcome. Owns the shared run-classify
   helpers and the lane-expanded state.
   Reads: state.runs; Date.now for the rolling window.
   Exports (on window.AdPage.cards, also kept as plain globals): gantt,
   expanded, isAgent, runEnd, packRows, outcomeOf, OUTCOME, outcomeColor
   (the replay inside plan-progress.js reads OUTCOME at call time).
   Needs: el, s, CW, axes, timeTicks, showTip, hideTip, tokCell, statusEl,
   openRun, drawMain, colorOf, fmtN, fmtDur (plain globals). */
(function (global) {
  "use strict";
  /* timeline: one activity strip per workstream, expandable into packed lanes */
  var expanded = {};
  /* A run with no end time is drawn to now only while it is running; an ended run whose end was never seen gets a one-minute stub. */
  /* Lead sessions and shell rows are drawn but not counted as agents. The server
     never sends production (dagster) jobs in these lists; the page has no dagster branch. */
  function isAgent(r) { return r.source !== "claude-main" && r.source !== "shell" && !r.lane_shadow; }
  function runEnd(r, now) { return r.ended_at || (r.status === "running" ? now : (r.started_at || now) + 60); }
  function packRows(runs, gapS) {
    var rows = [];
    runs.slice().sort(function (x, y) { return x.started_at - y.started_at; }).forEach(function (r) {
      var end = runEnd(r, Date.now() / 1000), placed = false;
      for (var i = 0; i < rows.length; i++) { if (rows[i].end + gapS <= r.started_at) { rows[i].runs.push(r); rows[i].end = end; placed = true; break; } }
      if (!placed) rows.push({end: end, runs: [r]});
    });
    return rows;
  }
  function outcomeOf(r) {
    if (!r.ended_at) return r.status === "running" ? "run" : "unk";
    if (r.status === "died_early" || r.status === "killed" || r.verdict === "BLOCKED" || r.verdict === "REQUEST_CHANGES" || r.verdict === "FAILURE" || r.verdict === "CANCELED") return "bad";
    if (r.verdict === "PARTIAL" || r.verdict === "APPROVE_WITH_CHANGES") return "part";
    if (r.status === "done" || r.verdict === "SUCCESS") return "ok";
    return "unk";
  }
  var OUTCOME = {run: ["var(--s1)", "\u25CF", "running"], ok: ["var(--good)", "\u2713", "finished"], part: ["var(--warning)", "\u25D0", "partial"], bad: ["var(--critical)", "\u2715", "died or blocked"], unk: ["var(--muted)", "?", "ended, outcome unknown"]};
  function outcomeColor(r) { var o = outcomeOf(r); return o === "run" || o === "unk" ? null : OUTCOME[o][0]; }
  function gantt() {
    var c = el("div", {class: "card", id: "exec-timeline", "data-nav": "Run timeline"}, [el("h2", {text: "Run timeline"}), el("p", {class: "sub", text: "One lane per workstream; the strip is how many worker agents ran at each moment (lead sessions are not counted). Click a lane to open it: one row per model, each bar a run, coloured by how it ended. Hover a bar for the run, click it for details."})]);
    var now = Date.now() / 1000, t0 = now - WINDOW_HOURS * 3600;
    var R = state.runs.filter(function (r) { return r.started_at && runEnd(r, now) >= t0; });
    if (!R.length) { c.appendChild(el("div", {class: "empty", text: "No runs in this window."})); return c; }
    var lg = el("div", {class: "legend"}); ["run", "ok", "part", "bad"].forEach(function (k) { var o = OUTCOME[k], sp = el("span"); var sw = el("i", {class: "sw"}); sw.style.background = o[0]; sp.appendChild(sw); sp.appendChild(document.createTextNode(o[1] + " " + o[2])); lg.appendChild(sp); });
    c.appendChild(lg);
    var groups = {}; R.forEach(function (r) { var g = r.workstream || "other"; (groups[g] = groups[g] || []).push(r); });
    var order = Object.keys(groups).sort(function (x, y) { var a = /(\d+)/.exec(x), b = /(\d+)/.exec(y); return (a ? +a[1] : 999) - (b ? +b[1] : 999) || x.localeCompare(y); });
    if (state.ws !== "all" && expanded[state.ws] === undefined) expanded[state.ws] = true;
    var W = CW(), L = 150, Rm = 14, pw = W - L - Rm, stripH = 30, rowH = 7, rowGap = 2;
    var X = function (t) { return L + pw * (Math.max(t, t0) - t0) / (now - t0); };
    var blocks = [], H = 12;
    order.forEach(function (g) {
      var subs = [], hh = 0;
      if (expanded[g]) { var byM = {}; groups[g].forEach(function (r) { var k = tierKey(r); (byM[k] = byM[k] || []).push(r); });
        ADM.sortKeys(Object.keys(byM)).forEach(function (k) { var rows = packRows(byM[k], 20); var h = Math.max(rows.length * (rowH + rowGap), 14) + 6; subs.push({k: k, name: fullName(k), runs: byM[k], rows: rows, dy: hh, h: h}); hh += h; }); }
      blocks.push({g: g, runs: groups[g], subs: subs, y: H}); H += stripH + 24 + (subs.length ? hh + 10 : 0); });
    var plotB = H; H += 34;
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": "Run activity per workstream over time"});
    var gTicks = timeTicks(t0, now, X, 6); gTicks.forEach(function (k) { svg.appendChild(s("line", {x1: k.x, x2: k.x, y1: 4, y2: plotB, class: "gl"})); });
    var step = Math.max(60, Math.round((now - t0) / 300));
    blocks.forEach(function (bk) {
      var col = colorOf(bk.g, "workstream"), y0 = bk.y;
      var lab = s("text", {x: 0, y: y0 + stripH / 2 + 4}); lab.textContent = (expanded[bk.g] ? "▾ " : "▸ ") + bk.g; lab.setAttribute("style", "fill:var(--ink);font:700 12px var(--mono);cursor:pointer");
      var cnt = s("text", {x: 0, y: y0 + stripH / 2 + 17, class: "ax"}); cnt.textContent = bk.runs.length + " runs";
      var hitL = s("rect", {x: 0, y: y0, width: L - 6, height: stripH, fill: "transparent"}); hitL.style.cursor = "pointer";
      hitL.addEventListener("click", function () { expanded[bk.g] = !expanded[bk.g]; drawMain(); });
      svg.appendChild(lab); svg.appendChild(cnt); svg.appendChild(hitL);
      var pts = [], mx = 1;
      for (var t = t0; t <= now; t += step) { var n = 0; bk.runs.forEach(function (r) { if (isAgent(r) && r.started_at <= t && runEnd(r, now) >= t) n++; }); pts.push([t, n]); mx = Math.max(mx, n); }
      var Y = function (v) { return y0 + stripH - (stripH - 4) * v / mx; };
      svg.appendChild(s("rect", {x: L, y: y0, width: pw, height: stripH, rx: 4, fill: "var(--surface-2)"}));
      var d = "M" + X(pts[0][0]) + "," + (y0 + stripH); pts.forEach(function (p) { d += " L" + X(p[0]) + "," + Y(p[1]); }); d += " L" + X(now) + "," + (y0 + stripH) + " Z";
      svg.appendChild(s("path", {d: d, fill: col, "fill-opacity": ".55", stroke: col, "stroke-width": 1.2}));
      [[0, y0 + stripH], [mx, y0 + 4]].forEach(function (p) { svg.appendChild(s("line", {x1: L - 4, x2: L, y1: p[1], y2: p[1], class: "bl"})); var tk = s("text", {x: L - 7, y: p[1] + 3.5, "text-anchor": "end", class: "ax"}); tk.textContent = fmtN(p[0]); svg.appendChild(tk); });
      bk.runs.forEach(function (r) {
        if (!r.ended_at || r.ended_at < t0) return; var oc = outcomeColor(r); if (!oc) return;
        var bad = oc === "var(--critical)";
        var m = bad ? s("text", {x: X(r.ended_at), y: y0 + stripH + 14, "text-anchor": "middle"}) : s("circle", {cx: X(r.ended_at), cy: y0 + stripH + 10, r: 2.6, fill: oc});
        if (bad) { m.textContent = "✕"; m.setAttribute("style", "fill:var(--critical);font:700 10px var(--font)"); }
        m.style.cursor = "pointer"; m.addEventListener("click", function () { openRun(r); });
        m.addEventListener("mousemove", function (ev) { showTip(ev, r.label || r.run_id, [{k: "outcome", v: r.verdict || r.status}, {k: "model", v: fullName(tierKey(r))}, {k: "ended", v: fmtT(r.ended_at)}]); }); m.addEventListener("mouseleave", hideTip);
        svg.appendChild(m);
      });
      var hit = s("rect", {x: L, y: y0, width: pw, height: stripH, fill: "transparent"});
      hit.addEventListener("mousemove", function (ev) { var bb = svg.getBoundingClientRect(); var tt = t0 + (now - t0) * ((ev.clientX - bb.left) * W / bb.width - L) / pw; var act = bk.runs.filter(function (r) { return isAgent(r) && r.started_at <= tt && runEnd(r, now) >= tt; }); var byT = {}; act.forEach(function (r) { var k = tierKey(r); byT[k] = (byT[k] || 0) + 1; }); showTip(ev, bk.g + " at " + fmtT(tt), [{k: "running", v: String(act.length)}].concat(ADM.sortKeys(Object.keys(byT)).map(function (k) { return {k: fullName(k), v: String(byT[k]), c: colorOf(k)}; }))); });
      hit.addEventListener("mouseleave", hideTip); hit.addEventListener("click", function () { expanded[bk.g] = !expanded[bk.g]; drawMain(); }); hit.style.cursor = "pointer";
      svg.appendChild(hit);
      var base = y0 + stripH + 26;
      bk.subs.forEach(function (sb, si) {
        var sy = base + sb.dy;
        if (si) svg.appendChild(s("line", {x1: 0, x2: W - Rm, y1: sy - 5, y2: sy - 5, class: "gl"}));
        var ml = s("text", {x: 10, y: sy + 9}); ml.textContent = sb.name + " \u00B7 " + sb.runs.length; ml.setAttribute("style", "fill:var(--ink-2);font:600 11.5px var(--font)"); svg.appendChild(ml);
        var ry = sy;
        sb.rows.forEach(function (row) {
          row.runs.forEach(function (r) {
            var x1 = X(r.started_at), x2 = X(runEnd(r, now)), o = OUTCOME[outcomeOf(r)];
            var bar = s("rect", {x: x1, y: ry, width: Math.max(2.5, x2 - x1 - 1), height: rowH, rx: 2, fill: r.clock_source === "mtime" ? "url(#hatch)" : o[0], stroke: r.clock_source === "mtime" ? o[0] : "none"});
            if (r.status === "running") { bar.setAttribute("stroke", "var(--ink)"); bar.setAttribute("stroke-width", "1"); }
            bar.style.cursor = "pointer";
            bar.addEventListener("mousemove", function (ev) { showTip(ev, r.label || r.run_id, [{k: "model", v: sb.name}, {k: "outcome", v: o[1] + " " + (r.verdict || o[2]), c: o[0]}, {k: "elapsed", v: r.ended_at || r.status === "running" ? fmtDur((r.ended_at || now) - r.started_at) : "end not recorded"}, {k: "workstream", v: r.workstream || "unattributed"}]); });
            bar.addEventListener("mouseleave", hideTip); bar.addEventListener("click", function () { openRun(r); });
            svg.appendChild(bar);
          });
          ry += rowH + rowGap;
        });
      });
    });
    var defs = s("defs", {}); var pat = s("pattern", {id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"}); pat.appendChild(s("rect", {width: 6, height: 6, fill: "var(--surface-2)"})); pat.appendChild(s("line", {x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--muted)", "stroke-width": 2})); defs.appendChild(pat); svg.insertBefore(defs, svg.firstChild);
    axes(svg, {L: L, T: 4, pw: pw, ph: plotB - 4, xTicks: gTicks, xTitle: "time", yTitle: null});
    svg.appendChild(s("line", {x1: X(now), x2: X(now), y1: 4, y2: plotB, stroke: "var(--s1)", "stroke-width": 1.5}));
    c.appendChild(svg);
    return c;
  }

  /* workstream cards */
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.gantt = gantt;
  global.gantt = gantt;
  global.expanded = expanded;
  global.isAgent = isAgent;
  global.runEnd = runEnd;
  global.packRows = packRows;
  global.outcomeOf = outcomeOf;
  global.OUTCOME = OUTCOME;
  global.outcomeColor = outcomeColor;
})(window);
