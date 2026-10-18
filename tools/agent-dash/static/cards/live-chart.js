/* Page card: live agents over time (concurrency strips).
   Renders one strip per model family on its own scale, shades inside a
   strip are the family's models, stacked.
   Reads: state.conc; window.ADM.
   Exports (on window.AdPage.cards, also kept as plain globals): concChart.
   Needs: el, s, CW, axes, timeTicks, modelLegend, showTip, hideTip,
   fmtT (plain globals). */
(function (global) {
  "use strict";
  function concChart(snap) {
    var c = el("div", {class: "card", id: "exec-live", "data-nav": "Live agents over time"}, [el("h2", {text: "Live agents over time"}), el("p", {class: "sub", text: "Agents running at each moment, one strip per model family on its own scale (0 to that family's peak), for this view's scope. Shades inside a strip are the family's models, stacked. Hover for the split."})]);
    var P = state.conc; if (!P.length) { c.appendChild(el("div", {class: "empty", text: "No samples yet."})); return c; }
    var t1 = Date.now() / 1000, t0 = t1 - WINDOW_HOURS * 3600; P = P.filter(function (p) { return p.t >= t0 - 60; }); if (!P.length) { c.appendChild(el("div", {class: "empty", text: "No samples in this window."})); return c; }
    var TOOLFAM = {grok: "cursor", muse: "muse", glm: "glm", claude: "claude"};
    function mods(p) { if (p.models) return p.models; var o = {}; Object.keys(TOOLFAM).forEach(function (t) { if (p[t]) o[TOOLFAM[t] + "-unknown"] = p[t]; }); return o; }
    var seen = {}; P.forEach(function (p) { var m = mods(p); for (var k in m) if (m[k]) seen[k] = 1; });
    var all = ADM.sortKeys(Object.keys(seen));
    if (all.length) c.appendChild(modelLegend(all));
    var fams = ["cursor", "muse", "glm", "claude"];
    function famKeys(f) { return all.filter(function (k) { return ADM.famOf(k) === f; }); }
    function tipRows(p) { var m = mods(p), rows = []; fams.forEach(function (f) { var ks = famKeys(f), tot = ks.reduce(function (a, k) { return a + (m[k] || 0); }, 0); rows.push({k: ADM.FAM[f].name, v: String(tot), c: ADM.famColor(f)}); ks.forEach(function (k) { if (m[k]) rows.push({k: "\u2003" + ADM.nameOf(k), v: String(m[k]), c: ADM.colorOf(k)}); }); }); return rows; }
    fams.forEach(function (f, fi) {
      var ks = famKeys(f), last = fi === fams.length - 1;
      var mx = 1; P.forEach(function (p) { var m = mods(p), sum = 0; ks.forEach(function (k) { sum += m[k] || 0; }); mx = Math.max(mx, sum); }); mx = Math.max(2, Math.ceil(mx / 2) * 2);
      var W = CW(), H = last ? 104 : 66, L = 120, R = 14, T = 10, Bm = last ? 42 : 8, pw = W - L - R, ph = H - T - Bm;
      var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": ADM.FAM[f].name + " live runs over time, by model"});
      var nm = s("text", {x: 0, y: T + ph / 2 + 4, class: "ax"}); nm.textContent = ADM.FAM[f].name; nm.setAttribute("style", "fill:var(--ink-2);font:600 12px var(--font)"); svg.appendChild(nm);
      if (ks.length > 1 || (ks.length === 1 && !/-unknown$/.test(ks[0]))) { var sub = s("text", {x: 0, y: T + ph / 2 + 17, class: "ax"}); sub.textContent = ks.map(function (k) { return ADM.nameOf(k); }).join(", ").slice(0, 22); svg.appendChild(sub); }
      var X = function (t) { return L + pw * (t - t0) / Math.max(1, t1 - t0); }, Y = function (v) { return T + ph - ph * v / mx; };
      var tt6 = timeTicks(t0, t1, X, 6); if (!last) tt6.forEach(function (k) { k.label = ""; });
      axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: mx, yTicks: 1, xTicks: tt6, xTitle: last ? "time" : null});
      var base = P.map(function () { return 0; });
      ks.forEach(function (k) {
        var top = P.map(function (p, i) { return base[i] + (mods(p)[k] || 0); });
        var d = "M" + P.map(function (p, i) { return X(p.t) + "," + Y(top[i]); }).join(" L");
        for (var i = P.length - 1; i >= 0; i--) d += " L" + X(P[i].t) + "," + Y(base[i]);
        svg.appendChild(s("path", {d: d + " Z", fill: ADM.colorOf(k), "fill-opacity": ".75", stroke: "none"}));
        base = top;
      });
      var ln = "M" + P.map(function (p, i) { return X(p.t) + "," + Y(base[i]); }).join(" L");
      svg.appendChild(s("path", {d: ln, fill: "none", stroke: ADM.famColor(f), "stroke-width": 1.6}));
      var hit = s("rect", {x: L, y: T, width: pw, height: ph, fill: "transparent"});
      hit.addEventListener("mousemove", function (ev) { var bb = svg.getBoundingClientRect(); var tt = t0 + (t1 - t0) * ((ev.clientX - bb.left) * W / bb.width - L) / pw; var best = P[0]; P.forEach(function (p) { if (Math.abs(p.t - tt) < Math.abs(best.t - tt)) best = p; }); showTip(ev, fmtT(best.t), tipRows(best)); });
      hit.addEventListener("mouseleave", hideTip); svg.appendChild(hit);
      c.appendChild(svg);
    });
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.concChart = concChart;
  global.concChart = concChart;
})(window);
