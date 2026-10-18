/* Page core: the run-details drawer.
   Exports (on window.AdPage, also kept as plain globals): drawer (the
   aside node) and openRun (fills and opens it). The Escape wiring stays in
   the page script next to the replay exit it also triggers. The verdict
   renders through the shared verdictEl pill (colour plus glyph plus word),
   so blocked and died never share a look here either.
   Needs: el, statusEl, verdictEl, fullName, tierKey, fmtDate, fmtN, fmtDur,
   fmtUsd, tokTotal, cachedPct as plain globals. */
(function (global) {
  "use strict";
  /* run drawer */
  var drawer = document.getElementById("drawer");
  function openRun(r) {
    drawer.innerHTML = "";
    drawer.appendChild(el("div", {style: "display:flex;align-items:center;gap:10px"}, [el("h2", {style: "margin:0;font-size:16px", text: r.label || r.run_id}), el("span", {class: "sp"}), el("button", {onclick: function () { drawer.classList.remove("open"); }}, ["Close"])]));
    drawer.appendChild(el("p", {style: "margin:6px 0 14px"}, [statusEl(r), r.verdict ? verdictEl(r.verdict) : null]));
    var tk = r.tokens || {};
    var kv = el("dl", {class: "kv"});
    [["Model", fullName(tierKey(r)) + (r.model ? " (" + r.model + ")" : "")], ["Launched by", r.source], ["Workstream", (r.workstream || "–") + (r.ws_confidence ? " (" + r.ws_confidence + ")" : "")], ["Section", (r.section == null || r.section === "") ? "–" : r.section], ["Session", r.session_id], ["Parent", r.parent_session_id], ["Started", r.started_at ? fmtDate(r.started_at) : "–"], ["Ended", r.ended_at ? fmtDate(r.ended_at) : "running"], ["Elapsed", fmtDur(r.elapsed_s)], ["Clock", r.clock_source], ["Output", fmtN(r.out_bytes) + "B" + (r.out_growing ? " (growing)" : "")], ["Errors", fmtN(r.err_bytes) + "B"], ["PID", r.pid || "–"], ["Memory", r.rss_kb ? fmtN(r.rss_kb * 1024) + "B" : "–"], ["Tokens", fmtN(tokTotal(tk)) + " (" + cachedPct(tk) + "% cached)"], ["Input / output", fmtN(tk.input) + " / " + fmtN(tk.output)], ["Cache read / write", fmtN(tk.cache_read) + " / " + fmtN(tk.cache_write)], ["Est. cost", fmtUsd(r.cost_usd)]].forEach(function (p) { kv.appendChild(el("dt", {text: p[0]})); kv.appendChild(el("dd", {text: p[1] == null ? "–" : String(p[1])})); });
    drawer.appendChild(kv);
    drawer.classList.add("open");
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.drawer = drawer;
  AdPage.openRun = openRun;
  global.drawer = drawer;
  global.openRun = openRun;
})(window);
