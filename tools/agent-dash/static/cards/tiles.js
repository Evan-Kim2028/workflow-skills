/* Page card: headline stat tiles.
   Renders the four tiles (agents live, agents finished, tokens, cost)
   for this view's scope, plus the request-failed state for a dead scope.
   Reads: state.ws and state.snapError; the snap passed in (server
   snapshot totals, never re-summed client buckets); window.ADC/ADM.
   Exports (on window.AdPage.cards, also kept as plain globals): tiles.
   Needs: el, tierKey, modelName, cachedPct, isAgent (plain globals). */
(function (global) {
  "use strict";
  /* components */
  /* Token and cost tiles read the server snapshot totals for this scope and
     the one fixed 24 h window (windowed and reconciled server-side). They
     never re-sum usage buckets on the client, so the tile and the chart
     legend cannot drift. */
  /* A failed scope never reads as zero: when the snapshot fetch failed and
     no cached response exists, the tiles say "request failed" instead of
     "0 in 24 h", so an error is loud instead of silently empty. */
  function tiles(snap) {
    snap = snap || {};
    if (state.snapError && !snap.counts) {
      var failed = el("div", {class: "grid g-tiles", id: "exec-tiles", "data-nav": "Summary", "data-failed-scope": state.ws});
      [["Agents live now", "request failed", state.snapError, null],
       ["Agents finished (24 h)", "request failed", state.snapError, null],
       ["Tokens (24 h)", "request failed", state.snapError, {"data-tile": "tokens", "data-ws": state.ws, "data-hours": "24", "data-failed-scope": state.ws}],
       ["Est. cost (24 h)", "request failed", state.snapError, {"data-tile": "cost", "data-ws": state.ws, "data-hours": "24", "data-failed-scope": state.ws}]
      ].forEach(function (t) {
        var attrs = {class: "card tile"};
        if (t[3]) for (var a in t[3]) attrs[a] = t[3][a];
        failed.appendChild(el("div", attrs, [el("div", {class: "v", text: String(t[1])}), el("div", {class: "l", text: t[0]}), el("div", {class: "x", text: t[2]})]));
      });
      return failed;
    }
    var run = snap.running || [], byTier = {};
    run.forEach(function (r) { var k = tierKey(r); byTier[k] = (byTier[k] || 0) + 1; });
    var tk = ((snap.totals || {}).tokens) || {};
    var allTok = ADC.billed(tk);
    var cost = (snap.totals && snap.totals.cost_usd) || 0;
    var unpTok = (snap.totals && snap.totals.unpriced_tokens) || 0;
    var unpModels = (snap.totals && snap.totals.unpriced_models) || [];
    var at = ((snap.totals_all_time || {}).tokens) || {};
    var allTimeTok = ADC.billed(at);
    var allTimeCost = (snap.totals_all_time && snap.totals_all_time.cost_usd) || 0;
    var agoTxt = ADC.ago(Date.now() / 1000, snap.last_activity);
    var tokVal = allTok ? fmtN(allTok) : "0 in 24 h";
    var tokSub = allTok ? cachedPct(tk) + "% cached \u00B7 out " + fmtN(tk.output || 0)
      : (agoTxt ? "last run " + agoTxt + " \u00B7 " + fmtN(allTimeTok) + " all time" : "no runs yet");
    var costSub = (unpTok ? "excl. " + fmtN(unpTok) + " tokens unpriced (" + unpModels.join(", ") + ") \u00B7 " : "every model priced \u00B7 ") + fmtUsd(allTimeCost) + " all time";
    var g = el("div", {class: "grid g-tiles", id: "exec-tiles", "data-nav": "Summary"});
    var cn = snap.counts || {}, lbt = cn.live_by_tool || {}, fv = cn.finished_by_verdict || {};
    var lbm = cn.live_by_model || {}, byFam = {};
    if (!Object.keys(lbm).length) run.forEach(function (r) { if (!isAgent(r)) return; var k = tierKey(r); lbm[k] = (lbm[k] || 0) + 1; });
    ADM.sortKeys(Object.keys(lbm).filter(function (k) { return lbm[k]; })).forEach(function (k) { var f = ADM.famOf(k); (byFam[f] = byFam[f] || []).push(k); });
    var liveSub = ADM.FAMS.filter(function (f) { return byFam[f]; }).map(function (f) { var ks = byFam[f], n = ks.reduce(function (a, k) { return a + lbm[k]; }, 0); return ADM.FAM[f].name + " " + fmtN(n) + " (" + ks.map(function (k) { return ADM.nameOf(k) + " " + fmtN(lbm[k]); }).join(", ") + ")"; }).join(" \u00B7 ") || "idle";
    if (cn.prod_jobs) { var jobTxt = fmtN(cn.prod_jobs) + " prod jobs"; liveSub = liveSub === "idle" ? jobTxt : liveSub + " \u00B7 " + jobTxt; }
    if (cn.prod_jobs_finished) { var finTxt = fmtN(cn.prod_jobs_finished) + " prod jobs ran"; liveSub = liveSub === "idle" ? finTxt : liveSub + " \u00B7 " + finTxt; }
    var bad = (fv.BLOCKED || 0) + (fv.REQUEST_CHANGES || 0), part = (fv.PARTIAL || 0) + (fv.APPROVE_WITH_CHANGES || 0);
    [["Agents live now", fmtN(cn.live_agents != null ? cn.live_agents : run.filter(isAgent).length), liveSub, null],
     ["Agents finished (24 h)", fmtN(cn.finished_in_range || 0), "\u2713 " + fmtN((fv.PASS || 0) + (fv.APPROVE || 0) + (fv.APPROVE_WITH_NITS || 0)) + " pass \u00B7 \u25D0 " + fmtN(part) + " partial \u00B7 \u2715 " + fmtN(bad) + " blocked (24 h)", null],
     ["Tokens (24 h)", tokVal, tokSub, {"data-tile": "tokens", "data-ws": state.ws, "data-hours": "24", "data-tokens": String(allTok), "data-tokens-all-time": String(allTimeTok)}],
     ["Est. cost (24 h)", fmtUsd(cost), costSub, {"data-tile": "cost", "data-ws": state.ws, "data-hours": "24", "data-cost-usd": String(cost), "data-cost-all-time": String(allTimeCost), "data-unpriced-tokens": String(unpTok)}]
    ].forEach(function (t) {
      var attrs = {class: "card tile"};
      if (t[3]) for (var a in t[3]) attrs[a] = t[3][a];
      g.appendChild(el("div", attrs, [el("div", {class: "v", text: String(t[1])}), el("div", {class: "l", text: t[0]}), el("div", {class: "x", text: t[2]})]));
    });
    return g;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.tiles = tiles;
  global.tiles = tiles;
})(window);
