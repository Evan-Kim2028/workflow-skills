/* Page card: tokens-per-hour stacked bars.
   Renders the hourly token stacks (by model or by workstream) with the
   legend Total read from the snapshot totals, never re-summed.
   Reads: the usage buckets and grouping on state, plus state.snap for the
   legend Total; window.ADC/ADM.
   Exports (on window.AdPage.cards, also kept as plain globals): tokenChart.
   Needs: el, s, CW, axes, timeTicks, modelLegend, colorOf, niceMax,
   refreshSnapshot, since (plain globals). */
(function (global) {
  "use strict";
  /* stacked bars: tokens per hour */
  function tokenChart() {
    var c = el("div", {class: "card", id: "exec-tokens", "data-nav": "Tokens per hour"});
    var hdr = el("div", {style: "display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"}, [el("h2", {text: "Tokens per hour"})]);
    var seg = el("span", {class: "seg-ctl", style: "margin-left:auto"});
    [["model", "By model"], ["workstream", "By workstream"]].forEach(function (g) { seg.appendChild(el("button", {"aria-pressed": String(state.groupBy === g[0]), onclick: function () { state.groupBy = g[0]; refreshSnapshot(); }}, [g[1]])); });
    if (state.ws === "all") hdr.appendChild(seg);
    c.appendChild(hdr);
    c.appendChild(el("p", {class: "sub", text: "Input, output and cache tokens summed per hour, stacked by " + state.groupBy + ". Hover a bar for the split."}));
    var B = state.usage;
    if (!B.length) { c.appendChild(el("div", {class: "empty", text: "No usage in this window."})); return c; }
    var byModel = state.groupBy === "model";
    var hours = {}, keys = {};
    B.forEach(function (b) { var k = state.groupBy === "model" ? modelKey(b.key) : (b.key || "unassigned"); var tot = (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0); hours[b.t] = hours[b.t] || {}; hours[b.t][k] = (hours[b.t][k] || 0) + tot; keys[k] = 1; });
    var ks = byModel ? ADM.sortKeys(Object.keys(keys)) : Object.keys(keys).sort();
    var ts = Object.keys(hours).map(Number).sort(function (a, b) { return a - b; });
    var max = 0; ts.forEach(function (t) { var sum = 0; ks.forEach(function (k) { sum += hours[t][k] || 0; }); max = Math.max(max, sum); });
    var keyTot = {}, grand = 0; ts.forEach(function (t) { ks.forEach(function (k) { var v = hours[t][k] || 0; keyTot[k] = (keyTot[k] || 0) + v; grand += v; }); });
    var keyLbl = {}; ks.forEach(function (k) { keyLbl[k] = fmtN(keyTot[k]); });
    /* The legend Total is the server snapshot total for this scope and the
       fixed 24 h window, the same number the tile shows. The chart draws the
       same response's usage buckets, so the two agree by construction; when
       they ever differ the page still says so instead of hiding it. */
    var snapTok = (state.snap && state.snap.totals) ? ADC.billed(state.snap.totals.tokens) : null;
    var showTotal = snapTok == null ? grand : snapTok;
    var lg; if (byModel) lg = modelLegend(ks, keyLbl); else { lg = el("div", {class: "legend"}); ks.forEach(function (k) { var sp = el("span"); var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k, state.groupBy); sp.appendChild(sw); sp.appendChild(document.createTextNode(k + " " + keyLbl[k])); lg.appendChild(sp); }); }
    var ltotal = el("span", {class: "ltotal", title: "All tokens in this window, input + output + cache read + cache write. The legend values add up to it.", "data-legend-total": String(showTotal), "data-chart-sum": String(grand)}, [el("b", {text: "Total " + fmtN(showTotal)}), document.createTextNode(" · " + fmtN(ts.length ? showTotal / ts.length : 0) + "/h avg · peak " + fmtN(max) + "/h")]);
    lg.insertBefore(ltotal, lg.firstChild);
    c.appendChild(lg);
    if (snapTok != null && grand !== snapTok) {
      var xline = el("p", {class: "xline", "data-warn": "token-total", title: "The tile reads the server total; the chart sums its own buckets. They should agree.", text: "\u26A0 tile " + fmtN(snapTok) + " \u00B7 chart " + fmtN(grand)});
      xline.setAttribute("style", "margin:0 0 8px;font-size:12px;color:var(--warning)");
      c.appendChild(xline);
    }
    var W = CW(), H = 270, L = 64, R = 14, T = 28, Bm = 42, pw = W - L - R, ph = H - T - Bm, h0 = Math.floor(since() / 3600) * 3600, nH = Math.max(1, Math.ceil((Date.now() / 1000 - h0) / 3600)), slot = pw / nH, bw = Math.max(3, Math.min(28, slot - 4));
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": "Tokens per hour stacked by " + state.groupBy});
    var nice = niceMax(max), xt = [];
    ts.forEach(function (t, idx) {
      var x = L + Math.max(0, Math.floor((t - h0) / 3600)) * slot + (slot - bw) / 2, y0 = T + ph;
      ks.forEach(function (k, ki) { var v = hours[t][k] || 0; if (!v) return; var h = ph * v / nice; var top = y0 - h; var r = s("rect", {x: x, y: top + (ki ? 1 : 0), width: bw, height: Math.max(0, h - (ki ? 1 : 0)), rx: 2, fill: colorOf(k, state.groupBy)}); svg.appendChild(r); y0 = top; });
      var hit = s("rect", {x: x - 1, y: T, width: bw + 2, height: ph, fill: "transparent"});
      hit.addEventListener("mousemove", function (ev) { var hourTot = 0; ks.forEach(function (k) { hourTot += hours[t][k] || 0; }); showTip(ev, new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}), [{k: "Total", v: fmtN(hourTot), c: "var(--ink)"}].concat(ks.filter(function (k) { return hours[t][k]; }).map(function (k) { return {k: byModel ? fullName(k) : k, v: fmtN(hours[t][k]), c: colorOf(k, state.groupBy)}; }))); });
      hit.addEventListener("mouseleave", hideTip); svg.appendChild(hit);
      
    });
    xt = timeTicks(h0, Date.now() / 1000, function (t) { return L + Math.floor((t - h0) / 3600) * slot + slot / 2; }, Math.max(2, Math.floor(pw / 90)));
    var axg = axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, xTicks: xt, yTitle: "tokens", xTitle: "hour"}); svg.insertBefore(axg, svg.firstChild); c.appendChild(svg);
    return c;
  }


  /* live agents over time: one strip per model family, its models stacked as shades of the family hue */
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.tokenChart = tokenChart;
  global.tokenChart = tokenChart;
})(window);
