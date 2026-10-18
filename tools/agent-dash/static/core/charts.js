/* Page core: shared chart and number-cell helpers (not card-specific).
   Exports (on window.AdPage, also kept as plain globals): CW (chart width
   from the main column); tokTotal, cachedPct, tokCell (billed-token sums
   and the table cell, with the live context-window estimate); niceMax
   (1/2/5 axis-ceiling rounding for use with the axes helper).
   Needs: el and fmtN as plain globals; window.AD only through the callers
   (axes and timeTicks stay the shared format.js pair both views call). */
(function (global) {
  "use strict";
  function CW() { var m = document.getElementById("main"); var w = m && m.clientWidth ? m.clientWidth : 1100; return Math.max(560, Math.floor(w - 36)); }
  /* Tokens cell: billed input + output, cached reads in muted ink, and for a live Grok run with no billed usage yet the context-window estimate (never counted in totals). */
  function tokTotal(t) { t = t || {}; return (t.input || 0) + (t.output || 0) + (t.cache_read || 0) + (t.cache_write || 0); }
  function cachedPct(t) { var tot = tokTotal(t); return tot ? Math.round(100 * (t.cache_read || 0) / tot) : 0; }
  function tokCell(r) {
    var t = r.tokens || {}, tot = tokTotal(t);
    var td = el("td", {class: "n", title: "total " + fmtN(tot) + " \u00B7 input " + fmtN(t.input) + " \u00B7 output " + fmtN(t.output) + " \u00B7 cache read " + fmtN(t.cache_read) + " \u00B7 cache write " + fmtN(t.cache_write)});
    if (!tot && r.context_tokens && !r.ended_at) {
      td.appendChild(document.createTextNode("~" + fmtN(r.context_tokens))); td.appendChild(el("span", {class: "cached", text: " live"}));
      td.title = "Estimate: context window now. Billed tokens arrive when the run ends.";
      return td;
    }
    td.appendChild(document.createTextNode(fmtN(tot)));
    if (tot) td.appendChild(el("span", {class: "cached", text: " " + cachedPct(t) + "% cached"}));
    return td;
  }
  function niceMax(v) { if (v <= 0) return 1; var p = Math.pow(10, Math.floor(Math.log10(v))); var m = v / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.CW = CW;
  AdPage.tokTotal = tokTotal;
  AdPage.cachedPct = cachedPct;
  AdPage.tokCell = tokCell;
  AdPage.niceMax = niceMax;
  global.CW = CW;
  global.tokTotal = tokTotal;
  global.cachedPct = cachedPct;
  global.tokCell = tokCell;
  global.niceMax = niceMax;
})(window);
