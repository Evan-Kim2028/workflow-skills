/* Page card: running and finished run tables.
   Renders the Running now and Finished (24 h) tables plus the row-key
   and truncation-subtitle helpers drawMain's registry entries use.
   Reads: nothing at load; rows are passed in.
   Exports (on window.AdPage.cards, also kept as plain globals):
   runsTable, statusEl (also read by static/core/drawer.js), runRow,
   finishedSub.
   Needs: el, tierKey, modelName, colorOf, tokCell, openRun, fmtN, fmtDur
   (plain globals). */
(function (global) {
  "use strict";
  function statusEl(r) { var st = r.status || "unknown"; return el("span", {class: "st " + st}, [el("i"), st === "died_early" ? "died early" : st]); }

  function runsTable(title, sub, runs, empty) {
    var attrs = {class: "card", "data-nav": title};
    if (title === "Running now") attrs.id = "exec-running";
    else if (title === "Finished (24 h)") { attrs.id = "exec-finished"; attrs["data-nav"] = "Finished (24 h)"; }
    var c = el("div", attrs, [el("h2", {text: title}), el("p", {class: "sub", text: sub})]);
    if (!runs.length) { c.appendChild(el("div", {class: "empty", text: empty})); return c; }
    var tb = el("tbody");
    runs.forEach(function (r) {
      var k = tierKey(r);
      var tr = el("tr", {class: "clk", onclick: function () { openRun(r); }});
      var tier = el("span", {class: "tier", title: r.model || ""}); var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k); tier.appendChild(sw); tier.appendChild(el("span", {class: "mfam-n", text: ADM.FAM[ADM.famOf(k)].name})); tier.appendChild(document.createTextNode(" " + modelName(k)));
      tr.appendChild(el("td", null, [tier]));
      tr.appendChild(el("td", null, [el("span", {class: "lbl", text: r.label || r.run_id})]));
      tr.appendChild(el("td", null, [el("span", {class: "ws", text: r.workstream || "–"})]));
      tr.appendChild(el("td", {class: "n", text: fmtDur(r.elapsed_s)}));
      var ob = el("td", {class: "n", text: fmtN(r.out_bytes) + "B"}); if (r.out_growing) ob.appendChild(el("span", {class: "grow", text: "▲"})); tr.appendChild(ob);
      tr.appendChild(tokCell(r));
      tr.appendChild(el("td", null, [statusEl(r), r.verdict ? el("span", {class: "vd", text: r.verdict}) : null]));
      tb.appendChild(tr);
    });
    var t = el("table", null, [el("thead", null, [el("tr", null, ["Model", "Label", "WS", "Elapsed", "Output", "Tokens", "Status"].map(function (h, i) { return el("th", {style: i >= 3 && i <= 5 ? "text-align:right" : ""}, [h]); }))]), tb]);
    c.appendChild(el("div", {style: "overflow-x:auto"}, [t]));
    return c;
  }
  var runRow = function (r) { return [r.run_id, r.status, r.verdict, r.out_bytes, r.tokens, r.context_tokens, Math.floor((r.elapsed_s || 0) / 15)]; };
  /* Finished subtitle names the truncation: "Newest 50 of 1,452" when the
     window holds more finished runs than the table shows, so the hidden gap
     is visible instead of silently omitted. */
  function finishedSub(snap) {
    var shown = (snap.recent || []).length;
    var total = (snap.counts && snap.counts.finished_in_range != null)
      ? snap.counts.finished_in_range
      : (snap.recent_total != null ? snap.recent_total : shown);
    if (total > shown) return "Newest " + shown + " of " + total + " \u00B7 Newest first, ended in the last 24 h.";
    return "Newest first, ended in the last 24 h.";
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.runsTable = runsTable;
  AdPage.cards.finishedSub = finishedSub;
  global.runsTable = runsTable;
  global.statusEl = statusEl;
  global.runRow = runRow;
  global.finishedSub = finishedSub;
})(window);
