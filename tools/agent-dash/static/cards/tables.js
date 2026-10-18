/* Page card: running and finished run tables.
   Renders the Running now and Finished (24 h) tables plus the row-key
   and truncation-subtitle helpers drawMain's registry entries use. Status
   is colour plus symbol plus word, verdicts are glyph-plus-word pills, the
   model cell carries the family swatch, numbers are tabular, rows hover,
   focus by keyboard, tooltip exact values, and open the drawer on
   click or Enter.
   Reads: nothing at load; rows are passed in.
   Exports (on window.AdPage.cards, also kept as plain globals):
   runsTable, statusEl (also read by static/core/drawer.js), verdictPill,
   runRow, finishedSub.
   Needs: el, tierKey, modelName, fullName, colorOf, tokCell, tokTotal,
   openRun, showTip, hideTip, fmtN, fmtDur (plain globals). */
(function (global) {
  "use strict";
  /* Status is colour plus symbol plus word, never colour alone: the dot
     carries the colour, the glyph carries the shape, the word names the
     state. Glyphs match the Where-we-are pills and the timeline key. */
  function statusSym(st) {
    if (st === "running") return "\u25CF";
    if (st === "done" || st === "finished") return "\u2713";
    if (st === "killed" || st === "died_early") return "\u2715";
    return "\u25CB";
  }
  function statusEl(r) {
    var st = r.status || "unknown";
    var word = st === "died_early" ? "died early" : st;
    return el("span", {class: "st " + st, title: "status: " + word}, [el("i"), statusSym(st) + " " + word]);
  }

  /* Verdict pills: colour plus glyph plus word. The raw verdict stays in
     the title so nothing exact is lost. */
  function verdictPill(v) {
    if (v === "PASS" || v === "APPROVE" || v === "APPROVE_WITH_NITS") return ["pass", "\u2713 pass"];
    if (v === "PARTIAL") return ["part", "\u25D0 partial"];
    if (v === "REQUEST_CHANGES") return ["rev", "\u2715 changes asked"];
    if (v === "BLOCKED") return ["rev", "\u2715 blocked"];
    return ["none", "\u2014 no verdict"];
  }
  function verdictEl(v) {
    var p = verdictPill(v);
    return el("span", {class: "vpill " + p[0], title: v == null ? "no verdict recorded" : String(v), text: p[1]});
  }

  /* Exact-value tooltip rows for a run row: full token digits plus the
     compact cell text's inputs. */
  function tipRows(r) {
    var t = r.tokens || {};
    return [
      {k: "Model", v: fullName(tierKey(r))},
      {k: "Elapsed", v: fmtDur(r.elapsed_s)},
      {k: "Tokens", v: String(tokTotal(t))},
      {k: "Status", v: (r.status || "unknown") + (r.verdict ? " \u00B7 " + r.verdict : "")}
    ];
  }

  function runsTable(title, sub, runs, empty) {
    var attrs = {class: "card", "data-nav": title};
    if (title === "Running now") attrs.id = "exec-running";
    else if (title === "Finished (24 h)") { attrs.id = "exec-finished"; attrs["data-nav"] = "Finished (24 h)"; }
    var c = el("div", attrs, [el("h2", {text: title}), el("p", {class: "sub", text: sub})]);
    if (!runs.length) { c.appendChild(el("div", {class: "empty", text: empty})); return c; }
    var tb = el("tbody");
    runs.forEach(function (r) {
      var k = tierKey(r);
      var tr = el("tr", {class: "clk", tabindex: "0",
        onclick: function () { openRun(r); },
        onkeydown: function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openRun(r); } },
        onmousemove: function (ev) { showTip(ev, r.label || r.run_id, tipRows(r)); },
        onmouseleave: function () { hideTip(); }});
      var tier = el("span", {class: "tier", title: r.model || ""}); var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k); tier.appendChild(sw); tier.appendChild(el("span", {class: "mfam-n", text: ADM.FAM[ADM.famOf(k)].name})); tier.appendChild(el("span", {class: "mmod-n", text: modelName(k)}));
      tr.appendChild(el("td", null, [tier]));
      tr.appendChild(el("td", null, [el("span", {class: "lbl", text: r.label || r.run_id})]));
      tr.appendChild(el("td", null, [el("span", {class: "ws", text: r.workstream || "–"})]));
      tr.appendChild(el("td", {class: "n hidephone", text: fmtDur(r.elapsed_s)}));
      var ob = el("td", {class: "n hidephone", text: fmtN(r.out_bytes) + "B"}); if (r.out_growing) ob.appendChild(el("span", {class: "grow", text: "▲"})); tr.appendChild(ob);
      tr.appendChild(tokCell(r));
      tr.appendChild(el("td", null, [statusEl(r), r.verdict ? verdictEl(r.verdict) : null]));
      tb.appendChild(tr);
    });
    var t = el("table", {"aria-label": title}, [el("thead", null, [el("tr", null, ["Model", "Label", "WS", "Elapsed", "Output", "Tokens", "Status"].map(function (h, i) { return el("th", {scope: "col", class: (i === 3 || i === 4) ? "hidephone" : "", style: i >= 3 && i <= 5 ? "text-align:right" : ""}, [h]); }))]), tb]);
    c.appendChild(el("div", {class: "twrap"}, [t]));
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
  global.verdictPill = verdictPill;
  global.runRow = runRow;
  global.finishedSub = finishedSub;
})(window);
