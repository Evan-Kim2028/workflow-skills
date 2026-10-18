/* Page card: live agents over time (concurrency strips).
   Renders one strip per model family on its own scale with a gradient
   area fill, a peak dot and a "pk N" annotation; a visible y axis per
   strip and one shared x axis under the last strip.
   Reads: state.conc and state.snapError; window.ADM.
   Exports (on window.AdPage.cards, also kept as plain globals): concChart.
   Needs: el, s, CW, axes, timeTicks, showTip, hideTip,
   fmtT (plain globals). */
(function (global) {
  "use strict";
  function concChart(snap) {
    var c = el("div", {class: "card", id: "exec-live", "data-nav": "Live agents over time"}, [el("h2", {text: "Live agents over time"}), el("p", {class: "sub", text: "Agents running at each moment, one strip per model family on its own scale. Hover a strip for the exact split."})]);
    var P = state.conc || [];
    if (state.snapError && !P.length) { c.appendChild(el("div", {class: "empty err", "data-failed-scope": state.ws, text: "request failed \u2014 " + state.snapError})); return c; }
    if (!P.length) { c.appendChild(el("div", {class: "empty", text: "No samples yet."})); return c; }
    var t1 = Date.now() / 1000, t0 = t1 - WINDOW_HOURS * 3600; P = P.filter(function (p) { return p.t >= t0 - 60; }); if (!P.length) { c.appendChild(el("div", {class: "empty", text: "No samples in this window."})); return c; }
    var TOOLFAM = {grok: "cursor", muse: "muse", glm: "glm", claude: "claude"};
    function mods(p) { if (p.models) return p.models; var o = {}; Object.keys(TOOLFAM).forEach(function (t) { if (p[t]) o[TOOLFAM[t] + "-unknown"] = p[t]; }); return o; }
    var seen = {}; P.forEach(function (p) { var m = mods(p); for (var k in m) if (m[k]) seen[k] = 1; });
    var all = ADM.sortKeys(Object.keys(seen));
    var fams = ["claude", "cursor", "muse", "glm"];
    function famKeys(f) { return all.filter(function (k) { return ADM.famOf(k) === f; }); }
    function tipRows(p) { var m = mods(p), rows = []; fams.forEach(function (f) { var ks = famKeys(f), tot = ks.reduce(function (a, k) { return a + (m[k] || 0); }, 0); rows.push({k: ADM.FAM[f].name, v: String(tot), c: ADM.famColor(f)}); ks.forEach(function (k) { if (m[k]) rows.push({k: "\u2003" + ADM.nameOf(k), v: String(m[k]), c: ADM.colorOf(k)}); }); }); return rows; }
    fams.forEach(function (f, fi) {
      var ks = famKeys(f), last = fi === fams.length - 1;
      var series = P.map(function (p) { var m = mods(p), sum = 0; ks.forEach(function (k) { sum += m[k] || 0; }); return sum; });
      var peak = 0; series.forEach(function (v) { peak = Math.max(peak, v); });
      var mx = Math.max(2, Math.ceil(peak / 2) * 2);
      var wrap = el("div", {class: "conc-strip", "data-fam": f, "data-peak": String(peak)});
      var head = el("div", {class: "conc-head"});
      var dot = el("i", {class: "sw"}); dot.style.background = ADM.famColor(f); head.appendChild(dot);
      head.appendChild(el("span", {class: "conc-fam", text: ADM.FAM[f].name}));
      head.appendChild(el("span", {class: "conc-models", text: ks.length ? ks.map(function (k) { return ADM.nameOf(k); }).join(", ") : "no models seen"}));
      head.appendChild(el("span", {class: "conc-pk", text: "pk " + peak}));
      wrap.appendChild(head);
      var W = CW(), H = last ? 112 : 72, L = 46, R = 48, T = 20, Bm = last ? 36 : 10, pw = W - L - R, ph = H - T - Bm;
      var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": ADM.FAM[f].name + " live runs over time, peak " + peak});
      var X = function (t) { return L + pw * (t - t0) / Math.max(1, t1 - t0); }, Y = function (v) { return T + ph - ph * v / mx; };
      var gid = "ad-conc-" + f;
      var defs = s("defs");
      var grad = s("linearGradient", {id: gid, x1: "0", y1: "0", x2: "0", y2: "1"});
      grad.appendChild(s("stop", {offset: "0", "stop-color": ADM.famColor(f), "stop-opacity": ".45"}));
      grad.appendChild(s("stop", {offset: "1", "stop-color": ADM.famColor(f), "stop-opacity": "0"}));
      defs.appendChild(grad); svg.appendChild(defs);
      var pts = P.map(function (p, i) { return Math.round(X(p.t)) + "," + Math.round(Y(series[i])); }).join(" ");
      svg.appendChild(s("polygon", {points: L + "," + (T + ph) + " " + pts + " " + (L + pw) + "," + (T + ph), fill: "url(#" + gid + ")"}));
      svg.appendChild(s("polyline", {points: pts, fill: "none", stroke: ADM.famColor(f), "stroke-width": 1.8, "stroke-linejoin": "round"}));
      var pi = 0; series.forEach(function (v, i) { if (v === peak) pi = i; });
      var px = X(P[pi].t), py = Y(peak);
      svg.appendChild(s("circle", {cx: Math.round(px), cy: Math.round(py), r: 3, fill: ADM.famColor(f)}));
      var pl = s("text", {y: Math.max(py - 6, T - 8), class: "conc-pk-lab"});
      if (px > L + pw - 46) { pl.setAttribute("x", Math.round(px) - 8); pl.setAttribute("text-anchor", "end"); }
      else { pl.setAttribute("x", Math.round(px) + 8); pl.setAttribute("text-anchor", "start"); }
      pl.textContent = "pk " + peak; svg.appendChild(pl);
      var xt = last ? timeTicks(t0, t1, X, Math.max(2, Math.floor(pw / 90))) : [];
      axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: mx, yTicks: 2, xTicks: xt, yTitle: "agents", xTitle: last ? "time" : null});
      var hit = s("rect", {x: L, y: T, width: pw, height: ph, fill: "transparent"});
      hit.addEventListener("mousemove", function (ev) { var bb = svg.getBoundingClientRect(); var tt = t0 + (t1 - t0) * ((ev.clientX - bb.left) * W / bb.width - L) / pw; var best = P[0]; P.forEach(function (p) { if (Math.abs(p.t - tt) < Math.abs(best.t - tt)) best = p; }); showTip(ev, fmtT(best.t), tipRows(best)); });
      hit.addEventListener("mouseleave", hideTip); svg.appendChild(hit);
      wrap.appendChild(svg);
      c.appendChild(wrap);
    });
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.concChart = concChart;
  global.concChart = concChart;
})(window);
