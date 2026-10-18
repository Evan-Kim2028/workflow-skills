/* Page card: run timeline, one lane per model family.
   Draws every run in the window, never a sample: each lane greedy-packs
   its runs into rows (packRows) and compresses row height as rows grow,
   so overlapping runs stack instead of hiding (bars never shrink below
   2 px tall or 2 px wide, and the lane label keeps the true count). A lane
   past 400 packed rows becomes a density heat strip labelled
   "N runs (dense)". The strip on top is agents at once; running bars
   reach the now-rule. Click a lane to open it (one row per model, each
   bar a run); click a bar for the run drawer. Hover any bar for the exact
   run, model, duration and verdict.
   Reads: state.runs; Date.now for the rolling window.
   Exports (on window.AdPage.cards, also kept as plain globals): gantt,
   expanded, isAgent, runEnd, packRows, outcomeOf, OUTCOME, outcomeColor
   (the replay inside plan-progress.js reads OUTCOME at call time).
   Needs: el, s, CW, axes, timeTicks, showTip, hideTip, openRun, drawMain,
   colorOf, fullName, tierKey, fmtN, fmtDur, fmtT, ADM (plain globals). */
(function (global) {
  "use strict";
  /* timeline: one lane per model family, packed so every run is drawn */
  var expanded = {};
  /* A packed lane past this many rows becomes a density heat strip. */
  var DENSE_ROWS = 400;
  /* A run with no end time is drawn to now only while it is running; an ended run whose end was never seen gets a one-minute stub. */
  /* Lead sessions and shell rows are drawn but not counted as agents. */
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
  function famOfRun(r) { return ADM.famOf(tierKey(r)); }
  function famName(f) { return (ADM.FAM[f] || ADM.FAM.other).name; }
  function famHue(f) { return "var(" + (ADM.FAM[f] || ADM.FAM.other).hue + ")"; }
  /* Row height compresses as packed rows grow; bars never shrink below 2 px. */
  function laneDims(nRows) {
    var rowH = 7, rowGap = 2;
    if (nRows > 10) { rowH = 4; rowGap = 1; }
    if (nRows > 40) { rowH = 3; rowGap = 1; }
    if (nRows > 120) { rowH = 2; rowGap = 1; }
    if (nRows > 250) { rowH = 2; rowGap = 0; }
    return {rowH: rowH, rowGap: rowGap, h: Math.max(nRows * (rowH + rowGap) - rowGap, 12)};
  }
  function durText(r, now) {
    if (r.ended_at) return fmtDur(r.ended_at - r.started_at);
    if (r.status === "running") return fmtDur(now - r.started_at);
    return "end not recorded";
  }
  function drawBar(svg, r, x1, x2, y, h, model, now) {
    var oc = outcomeOf(r), o = OUTCOME[oc];
    var bar = s("rect", {x: x1, y: y, width: Math.max(2, x2 - x1 - 1), height: h, rx: Math.min(2, h / 2), fill: r.clock_source === "mtime" ? "url(#hatch)" : o[0], stroke: r.clock_source === "mtime" ? o[0] : "none", class: "tl-bar"});
    if (r.status === "running") { bar.setAttribute("stroke", "var(--ink)"); bar.setAttribute("stroke-width", "1"); }
    bar.setAttribute("data-run-id", r.run_id || r.label || "");
    bar.setAttribute("data-outcome", oc);
    bar.addEventListener("mousemove", function (ev) { showTip(ev, r.label || r.run_id, [{k: "model", v: model}, {k: "outcome", v: o[1] + " " + (r.verdict || o[2]), c: o[0]}, {k: "duration", v: durText(r, now)}, {k: "workstream", v: r.workstream || "unattributed"}]); });
    bar.addEventListener("mouseleave", hideTip);
    bar.addEventListener("click", function () { openRun(r); });
    svg.appendChild(bar);
    return bar;
  }
  /* One heat cell per time slice, darker where more runs overlap. */
  function drawDense(svg, lane, X, t0, now, y, L, pw) {
    var h = 26, span = Math.max(1, now - t0), n = Math.max(60, Math.floor(pw / 3)), counts = [], i, mx = 0;
    for (i = 0; i < n; i++) counts.push(0);
    lane.runs.forEach(function (r) {
      var a = Math.max(0, Math.floor((r.started_at - t0) / span * n));
      var b = Math.max(a, Math.min(n - 1, Math.floor((runEnd(r, now) - t0) / span * n)));
      for (i = a; i <= b; i++) counts[i]++;
    });
    counts.forEach(function (n2) { mx = Math.max(mx, n2); });
    svg.appendChild(s("rect", {x: L, y: y, width: pw, height: h, rx: 4, fill: "var(--surface-2)"}));
    var hue = famHue(lane.fam), bw = pw / n;
    counts.forEach(function (n2, k) {
      if (!n2) return;
      var cell = s("rect", {x: X(t0 + k * span / n), y: y, width: Math.max(1.5, bw + 0.5), height: h, rx: 1, fill: hue, class: "tl-heat"});
      cell.setAttribute("data-dense-bucket", k);
      cell.setAttribute("data-dense-count", n2);
      cell.setAttribute("opacity", String(Math.round(100 * (0.12 + 0.88 * n2 / Math.max(1, mx))) / 100));
      svg.appendChild(cell);
    });
    var label = famName(lane.fam) + " \u00B7 " + lane.runs.length + " runs (dense)";
    var hit = s("rect", {x: L, y: y, width: pw, height: h, fill: "transparent", class: "tl-dense-hit", "data-lane": lane.fam});
    hit.style.cursor = "pointer";
    hit.addEventListener("mousemove", function (ev) { showTip(ev, label, [{k: "peak slice", v: fmtN(mx) + " overlapping"}, {k: "hint", v: "click the lane to open one row per model"}]); });
    hit.addEventListener("mouseleave", hideTip);
    hit.addEventListener("click", function () { expanded[lane.fam] = !expanded[lane.fam]; drawMain(); });
    svg.appendChild(hit);
  }
  function gantt() {
    var c = el("div", {class: "card tl", id: "exec-timeline", "data-nav": "Run timeline"}, [el("h2", {text: "Run timeline"}), el("p", {class: "sub", text: "Every run in the last 24 h is drawn: lanes group by model family and overlapping runs stack into packed rows that compress instead of hiding runs. The strip is how many worker agents ran at each moment; running bars reach the blue now-rule. Click a lane to open it: one row per model, each bar a run. Hover a bar for the run, click it for details."})]);
    var now = Date.now() / 1000, t0 = now - WINDOW_HOURS * 3600;
    var R = state.runs.filter(function (r) { return r.started_at && runEnd(r, now) >= t0; });
    if (!R.length) { c.appendChild(el("div", {class: "empty", text: "No runs in this window."})); return c; }
    var lg = el("div", {class: "legend"});
    ["run", "ok", "part", "bad", "unk"].forEach(function (k) { var o = OUTCOME[k], sp = el("span"); var sw = el("i", {class: "sw"}); sw.style.background = o[0]; sp.appendChild(sw); sp.appendChild(document.createTextNode(o[1] + " " + o[2])); lg.appendChild(sp); });
    c.appendChild(lg);
    var byFam = {};
    R.forEach(function (r) { var f = famOfRun(r); (byFam[f] = byFam[f] || []).push(r); });
    var order = ADM.FAMS.filter(function (f) { return byFam[f]; });
    var top = order.slice().sort(function (a, b) { return byFam[b].length - byFam[a].length; })[0];
    if (state.ws !== "all" && top && order.every(function (f) { return expanded[f] === undefined; })) expanded[top] = true;
    var W = CW(), L = W <= 600 ? 104 : 150, Rm = 14, pw = W - L - Rm, T = 8, stripH = 30;
    var X = function (t) { return L + pw * (Math.max(t, t0) - t0) / (now - t0); };
    var step = Math.max(60, Math.round((now - t0) / 300));
    /* Lay the lanes out first so the axis and now-rule span the whole plot. */
    var lanes = order.map(function (f) {
      var runs = byFam[f], lane = {fam: f, runs: runs, dense: false, subs: [], rows: null, dims: null, h: 0};
      if (expanded[f]) {
        var byM = {}, y = 0;
        runs.forEach(function (r) { var k = tierKey(r); (byM[k] = byM[k] || []).push(r); });
        ADM.sortKeys(Object.keys(byM)).forEach(function (k) {
          var mrows = packRows(byM[k], 20), dd = laneDims(mrows.length);
          lane.subs.push({name: fullName(k), runs: byM[k], rows: mrows, dims: dd, y: y});
          y += 16 + dd.h + 6;
        });
        lane.h = y;
      } else {
        var rows = packRows(runs, 20);
        if (rows.length > DENSE_ROWS) { lane.dense = true; lane.h = 26; }
        else { lane.rows = rows; lane.dims = laneDims(rows.length); lane.h = lane.dims.h; }
      }
      return lane;
    });
    var stripY = 12, yy = stripY + stripH + 14;
    lanes.forEach(function (lane) { lane.y = yy; yy += 22 + lane.h + 10; });
    var plotB = yy - 10, H = Math.ceil(plotB + 32);
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": "Agent runs over the last 24 hours, one lane per model family"});
    var defs = s("defs", {}); var pat = s("pattern", {id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"}); pat.appendChild(s("rect", {width: 6, height: 6, fill: "var(--surface-2)"})); pat.appendChild(s("line", {x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--muted)", "stroke-width": 2})); defs.appendChild(pat); svg.appendChild(defs);
    var gTicks = timeTicks(t0, now, X, 6); gTicks.forEach(function (k) { svg.appendChild(s("line", {x1: k.x, x2: k.x, y1: T, y2: plotB, class: "gl"})); });
    /* Agents-at-once strip on top. */
    var slab = s("text", {x: 0, y: stripY + stripH / 2, class: "tl-lane-label"}); slab.textContent = "agents at once"; svg.appendChild(slab);
    var scnt = s("text", {x: 0, y: stripY + stripH / 2 + 14, class: "tl-strip-sub"}); scnt.textContent = R.length + " runs"; svg.appendChild(scnt);
    var pts = [], mx = 1;
    for (var t = t0; t <= now; t += step) { var n = 0; R.forEach(function (r) { if (isAgent(r) && r.started_at <= t && runEnd(r, now) >= t) n++; }); pts.push([t, n]); mx = Math.max(mx, n); }
    var Y = function (v) { return stripY + stripH - (stripH - 4) * v / mx; };
    svg.appendChild(s("rect", {x: L, y: stripY, width: pw, height: stripH, rx: 4, fill: "var(--surface-2)"}));
    var d = "M" + X(pts[0][0]) + "," + (stripY + stripH); pts.forEach(function (p) { d += " L" + X(p[0]) + "," + Y(p[1]); }); d += " L" + X(now) + "," + (stripY + stripH) + " Z";
    svg.appendChild(s("path", {d: d, fill: "var(--s1)", "fill-opacity": ".55", stroke: "var(--s1)", "stroke-width": 1.2}));
    [[0, stripY + stripH], [mx, stripY + 4]].forEach(function (p) { svg.appendChild(s("line", {x1: L - 4, x2: L, y1: p[1], y2: p[1], class: "bl"})); var tk = s("text", {x: L - 7, y: p[1] + 3.5, "text-anchor": "end", class: "ax"}); tk.textContent = fmtN(p[0]); svg.appendChild(tk); });
    var shit = s("rect", {x: L, y: stripY, width: pw, height: stripH, fill: "transparent", class: "tl-strip-hit"});
    shit.addEventListener("mousemove", function (ev) {
      var bb = svg.getBoundingClientRect();
      var tt = t0 + (now - t0) * ((ev.clientX - bb.left) * W / bb.width - L) / pw;
      var act = R.filter(function (r) { return isAgent(r) && r.started_at <= tt && runEnd(r, now) >= tt; });
      var byF = {};
      act.forEach(function (r) { var f = famOfRun(r); byF[f] = (byF[f] || 0) + 1; });
      showTip(ev, "agents at " + fmtT(tt), [{k: "running", v: String(act.length)}].concat(order.filter(function (f) { return byF[f]; }).map(function (f) { return {k: famName(f), v: String(byF[f]), c: famHue(f)}; })));
    });
    shit.addEventListener("mouseleave", hideTip);
    svg.appendChild(shit);
    lanes.forEach(function (lane, li) {
      var ly = lane.y, by = ly + 22;
      if (li) svg.appendChild(s("line", {x1: 0, x2: W - Rm, y1: ly - 6, y2: ly - 6, class: "gl"}));
      var lab = s("text", {x: 0, y: ly + 14, class: "tl-lane-label"});
      lab.textContent = (expanded[lane.fam] ? "\u25BE " : "\u25B8 ") + famName(lane.fam) + " \u00B7 " + lane.runs.length + " runs" + (lane.dense && !expanded[lane.fam] ? " (dense)" : "");
      svg.appendChild(lab);
      var hitL = s("rect", {x: 0, y: ly, width: L - 6, height: 20, fill: "transparent", class: "tl-lane-hit", "data-lane": lane.fam});
      hitL.style.cursor = "pointer";
      hitL.addEventListener("click", (function (f) { return function () { expanded[f] = !expanded[f]; drawMain(); }; })(lane.fam));
      svg.appendChild(hitL);
      if (expanded[lane.fam]) {
        lane.subs.forEach(function (sb) {
          var ml = s("text", {x: 10, y: by + sb.y + 11, class: "tl-model-label"});
          ml.textContent = sb.name + " \u00B7 " + sb.runs.length;
          svg.appendChild(ml);
          var ry = by + sb.y + 16;
          sb.rows.forEach(function (row) {
            row.runs.forEach(function (r) {
              drawBar(svg, r, X(Math.max(r.started_at, t0)), X(Math.min(runEnd(r, now), now)), ry, sb.dims.rowH, sb.name, now);
            });
            ry += sb.dims.rowH + sb.dims.rowGap;
          });
        });
      } else if (lane.dense) {
        drawDense(svg, lane, X, t0, now, by, L, pw);
      } else {
        var ry2 = by, dd = lane.dims;
        lane.rows.forEach(function (row) {
          row.runs.forEach(function (r) {
            drawBar(svg, r, X(Math.max(r.started_at, t0)), X(Math.min(runEnd(r, now), now)), ry2, dd.rowH, fullName(tierKey(r)), now);
          });
          ry2 += dd.rowH + dd.rowGap;
        });
      }
    });
    axes(svg, {L: L, T: T, pw: pw, ph: plotB - T, xTicks: gTicks, xTitle: "time", yTitle: null});
    svg.appendChild(s("line", {x1: X(now), x2: X(now), y1: T, y2: plotB, class: "tl-now"}));
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
