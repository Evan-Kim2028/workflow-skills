/* Page card: overview pulse — at-a-glance activity for all workstreams.
   First section on the overview (?ws=all): a KPI strip (workstreams in
   progress, agents running, tokens and cost for the fixed 24 h window,
   finished agents) with hourly token bars. No banners, toasts or
   notification rows.
   Reads: state.wss (rows from /api/workstreams), state.snap (counts,
   totals, prev_24h, running, by_workstream), state.pulseUsage (the
   per-workstream hour buckets fetched below, cached per minute; the
   KPI tokens cell folds them into 24 hourly bars).
   Exports (on window.AdPage, also kept as plain globals): pulseSpec,
   pulseCard, PULSE (the one thresholds object), pulseWsKey, pulseEnsure.
   Needs: el, api, keyOf, tokTotal, fmtN, fmtUsd, ago, planSum, shown
   (plain globals read at call time);
   window.AdPage/ADM/ADC, AdUnatt (the unattributed toggle and note,
   core/unattributed.js), secCache, drawMain, state. */
(function (global) {
  "use strict";

  /* The one thresholds object: fixed rolling window, hours. */
  var PULSE = {
    WINDOW_H: 24         /* fixed rolling window, hours */
  };

  /* Tool (live_by_tool key) to entity family, the same mapping the tiles
     use so the KPI bar and the agent dots agree. */
  var TOOL_FAM = {grok: "cursor", muse: "muse", glm: "glm", claude: "claude", deepseek: "deepseek", beam: "beam", codex: "codex", commandcode: "commandcode", other: "other"};
  function famColor(fam) {
    try {
      if (global.ADM && typeof ADM.famColor === "function") return ADM.famColor(fam);
    } catch (e) {}
    return "var(--other)";
  }
  function verdictColor(cls) {
    try {
      if (global.ADM && ADM.OUTCOME && ADM.OUTCOME[cls]) return ADM.OUTCOME[cls][0];
    } catch (e) {}
    return "var(--other)";
  }
  function toolFam(tool) { return TOOL_FAM[tool] || "other"; }

  /* Percent chip math without toFixed (the page rule bans it): one
     decimal below 100, whole numbers at and past it. */
  function pulsePct1(pc) {
    var r = Math.abs(pc) >= 100 ? Math.round(pc) : Math.round(pc * 10) / 10;
    return (pc > 0 ? "+" : "") + String(r) + "%";
  }
  function pulseChip(cur, prev, word, title) {
    var cls, symbol, value;
    if (!(prev > 0)) {
      /* A zero prior window is genuinely new; a missing one (NaN) reads flat. */
      if (cur > 0 && prev === 0) { cls = "new"; symbol = "\u25B2"; value = "new"; }
      else { cls = "flat"; symbol = "\u25C6"; value = "flat"; }
    } else {
      var pc = 100 * (cur - prev) / prev;
      if (Math.abs(pc) < 0.5) { cls = "flat"; symbol = "\u25C6"; value = "flat"; }
      else { cls = pc > 0 ? "up" : "down"; symbol = pc > 0 ? "\u25B2" : "\u25BC"; value = pulsePct1(pc); }
    }
    var c = el("span", {class: "delta " + cls, title: title});
    c.appendChild(document.createTextNode(symbol + " " + value));
    c.appendChild(el("span", {class: "w", text: word}));
    return {node: c, cls: cls};
  }
  function fmtKpi(n) {
    try { return fmtN(n); } catch (e) { return String(n); }
  }
  function fmtMoney(n) {
    try { return fmtUsd(n); } catch (e) { return String(n); }
  }

  /* Per-workstream hour buckets for the KPI tokens hourly bars,
     fetched once a minute like the activity progress payload
     (actProgKey): the section key below carries the payload stamp, so
     the arrival redraws the section once. A failed fetch marks
     state.pulseFailed (the card says "request failed" instead of
     drawing zeros) and stamps state.pulseFailedMin with the failing
     minute, so the retry waits for the next minute: drawMain calls
     pulseEnsure again at once. */
  var pulseInflight = false;
  function pulseSince(nowS) { return Math.floor(nowS - PULSE.WINDOW_H * 3600); }
  function pulseMinute() { return Math.floor(Date.now() / 60000); }
  function pulseWsKey() {
    var p = (typeof state !== "undefined" && state.pulseUsage) || null;
    return "pulse|" + (p ? Math.floor(p.since / 60) : 0) + "|" + (p && p.buckets ? "1" : "0") + "|" + Math.floor(Date.now() / 60000);
  }
  function pulseEnsure() {
    if (typeof state === "undefined" || typeof api !== "function") return;
    var nowS = Date.now() / 1000, want = pulseSince(nowS);
    var p = state.pulseUsage;
    if (p && p.buckets && Math.abs(p.since - want) < 120 && (Date.now() - (p.at || 0)) < 60000) return;
    if (state.pulseFailedMin === pulseMinute()) return;
    if (pulseInflight) return;
    pulseInflight = true;
    api("/api/usage", {group: "workstream", bucket: "hour", since: want}).then(function (j) {
      pulseInflight = false;
      if (typeof state === "undefined" || (state.view === "exec" && state.ws !== "all")) return;
      state.pulseUsage = {since: want, buckets: (j && j.buckets) || [], at: Date.now()};
      state.pulseFailed = false;
      state.pulseFailedMin = null;
      try { delete secCache.pulse; } catch (e) {}
      if (typeof drawMain === "function") drawMain();
    }, function () {
      pulseInflight = false;
      if (typeof state === "undefined") return;
      state.pulseFailedMin = pulseMinute();
      if (!state.pulseUsage) state.pulseFailed = true;
      try { delete secCache.pulse; } catch (e) {}
      if (typeof drawMain === "function") drawMain();
    });
  }

  function wsStateOf(w) {
    try {
      if (typeof ADT !== "undefined" && ADT.wsStateOf) return ADT.wsStateOf(w);
    } catch (e) {}
    var s = w && w.state;
    return s === "planned" || s === "active" || s === "paused" || s === "done" || s === "archived" || s === "dropped" ? s : "active";
  }

  /* One stacked bar: segments [[color, count, title]] over total, 2 px
     gaps and rounded ends from the shared card CSS. Zero tools hide. */
  function pulseStack(segs, total, label) {
    var bar = el("div", {class: "pulse-stack", role: "img", "aria-label": label});
    segs.forEach(function (sg) {
      if (!(sg[1] > 0) || !(total > 0)) return;
      var sp = el("span", {title: sg[2]});
      sp.style.width = (100 * sg[1] / total) + "%";
      sp.style.background = sg[0];
      bar.appendChild(sp);
    });
    return bar;
  }

  /* 24 hourly bars as plain divs (decorative: the headline text carries
     every value, the same reason the tile sparklines stay aria-hidden).
     lastHatched marks the current partial hour. */
  function pulseBars(vals, max, lastHatched, title) {
    var wrap = el("div", {class: "pulse-bars", "aria-hidden": "true", title: title});
    var peak = max > 0 ? max : 1;
    vals.forEach(function (v, i) {
      var b = el("span", {class: "pulse-bar" + (lastHatched && i === vals.length - 1 ? " cur" : "")});
      b.style.height = Math.max(4, Math.round(100 * v / peak)) + "%";
      wrap.appendChild(b);
    });
    return wrap;
  }

  function pulseKpi(label, valueText, chip, extra, dataAttrs) {
    var attrs = {class: "pulse-kpi"};
    if (dataAttrs) for (var a in dataAttrs) attrs[a] = dataAttrs[a];
    var card = el("div", attrs, [el("div", {class: "l", text: label}), el("div", {class: "v", text: valueText})]);
    if (chip) card.appendChild(chip);
    (extra || []).forEach(function (x) { if (x) card.appendChild(x); });
    return card;
  }

  function pulseCard(snap) {
    snap = snap || {};
    var nowS = Date.now() / 1000;
    var cn = snap.counts || {};
    var tk = ((snap.totals || {}).tokens) || {};
    var allTok = 0;
    try { allTok = tokTotal(tk); } catch (e) { allTok = (+tk.input || 0) + (+tk.output || 0) + (+tk.cache_read || 0) + (+tk.cache_write || 0); }
    var cost = (+(snap.totals || {}).cost_usd) || 0;
    var allTime = (snap.totals_all_time || {});
    var allTimeTok = 0;
    try { allTimeTok = tokTotal(allTime.tokens || {}); } catch (e) { allTimeTok = 0; }
    var allTimeCost = (+allTime.cost_usd) || 0;
    var p24 = (snap.prev_24h && typeof snap.prev_24h === "object") ? snap.prev_24h : null;
    var wss = [];
    try { wss = (typeof shown === "function" ? shown(state.wss || []) : (state.wss || [])).slice(); } catch (e) { wss = []; }

    var c = el("div", {class: "pulse", "data-nav": "Pulse"});
    c.appendChild(el("div", {class: "pulse-h"}, [
      el("h2", {text: "Pulse"}),
      el("span", {class: "sub", text: "At-a-glance activity \u00B7 last 24 h"})
    ]));

    if (state.snapError && !snap.counts) {
      var failed = el("div", {class: "pulse-kpis"});
      ["Workstreams in progress", "Agents running", "Tokens (24 h)", "Est. cost (24 h)", "Finished (24 h)"].forEach(function (t) {
        failed.appendChild(el("div", {class: "pulse-kpi", "data-failed-scope": "all"}, [el("div", {class: "l", text: t}), el("div", {class: "v", text: "request failed"})]));
      });
      c.appendChild(failed);
      return c;
    }

    /* KPI 1: workstreams in progress. Planned counts only with live
       agents or windowed tokens; unassigned is not a workstream. */
    var active = 0, planned = 0, done = 0;
    wss.forEach(function (w) {
      if (!w || w.ws === "unassigned") return;
      var st = wsStateOf(w);
      if (st === "active") active++;
      else if (st === "planned") { if ((+w.live_agents || 0) > 0 || (+w.tokens || 0) > 0) planned++; }
      else if (st === "done") done++;
    });
    var inProg = active + planned;

    /* KPI 2: agents running, stacked by tool. */
    var lbt = cn.live_by_tool || {};
    var liveNow = cn.live_agents != null ? cn.live_agents : (snap.running || []).filter(function (r) { return !r.ended_at; }).length;
    var toolSegs = [], toolLegend = [], liveTot = 0;
    Object.keys(lbt).forEach(function (t) { liveTot += (+lbt[t] || 0); });
    if (!liveTot) liveTot = liveNow;
    var byFam = {};
    Object.keys(lbt).forEach(function (t) {
      var n = +lbt[t] || 0;
      if (!(n > 0)) return;
      var fam = toolFam(t);
      byFam[fam] = (byFam[fam] || 0) + n;
      toolSegs.push([famColor(fam), n, t + " " + fmtKpi(n)]);
    });
    Object.keys(byFam).forEach(function (f) {
      var name = "other";
      try { name = (ADM && ADM.FAM && ADM.FAM[f]) ? ADM.FAM[f].name : f; } catch (e) { name = f; }
      toolLegend.push(name + " " + fmtKpi(byFam[f]));
    });
    var liveBar = toolSegs.length ? pulseStack(toolSegs, liveTot, "Live agents by tool: " + toolLegend.join(", ")) : null;

    /* KPI 3+4: tokens and cost with the prior-24 h delta. */
    var tokChip = pulseChip(allTok, p24 ? +p24.tokens : NaN, "vs prior 24 h", "Change vs the previous rolling 24 h (exact API totals): this window " + fmtKpi(allTok) + ", prior " + fmtKpi(p24 ? p24.tokens : 0));
    var costChip = pulseChip(cost, p24 ? +p24.cost_usd : NaN, "vs prior 24 h", "Change vs the previous rolling 24 h (exact API totals): this window " + fmtMoney(cost) + ", prior " + fmtMoney(p24 ? p24.cost_usd : 0));

    /* KPI 5: finished in range, stacked by verdict with the Finished
       table's colours (ADM.OUTCOME, the one derivation). */
    var fv = cn.finished_by_verdict || {};
    var finNow = cn.finished_in_range || 0;
    var finSegs = [["ok", "\u2713 pass"], ["part", "\u25D0 partial"], ["blocked", "\u2298 blocked"], ["died", "\u2715 died"]].map(function (kv) {
      var n = +fv[kv[0]] || 0;
      return [verdictColor(kv[0]), n, kv[1] + " " + fmtKpi(n)];
    });
    var finExtra = (+fv.cancelled || 0) + (+fv.unk || 0);
    if (finExtra > 0) finSegs.push(["var(--muted)", finExtra, "cancelled / no verdict " + fmtKpi(finExtra)]);
    var finTot = finSegs.reduce(function (a, s) { return a + s[1]; }, 0) || finNow;
    var finBar = finSegs.length && finTot > 0 ? pulseStack(finSegs, finTot, "Finished by verdict: " + finSegs.map(function (s) { return s[2]; }).join(", ")) : null;

    /* Hourly totals across all workstreams for the token sparkline. */
    var cells = (state.pulseUsage && state.pulseUsage.buckets) || null;
    var hEnd = Math.floor(nowS / 3600) * 3600;
    var hours = [];
    var hi;
    for (hi = 0; hi < PULSE.WINDOW_H; hi++) hours.push(hEnd - (PULSE.WINDOW_H - 1 - hi) * 3600);
    var hourTot = hours.map(function () { return 0; });
    var hasPayload = !!cells;
    if (cells) {
      cells.forEach(function (b) {
        if (!b) return;
        var t = +b.t;
        if (!isFinite(t)) return;
        var idx = Math.round((t - (hEnd - (PULSE.WINDOW_H - 1) * 3600)) / 3600);
        if (idx < 0 || idx >= PULSE.WINDOW_H) return;
        hourTot[idx] += (+b.input || 0) + (+b.output || 0) + (+b.cache_read || 0) + (+b.cache_write || 0);
      });
    }
    var peak = hourTot.reduce(function (a, b) { return Math.max(a, b); }, 0);

    var kpis = el("div", {class: "pulse-kpis"});
    kpis.appendChild(pulseKpi("Workstreams in progress", String(inProg),
      null,
      [el("div", {class: "x", text: active + " active \u00B7 " + planned + " planned \u00B7 " + done + " done"})],
      {"data-kpi": "in-progress", "data-active": String(active), "data-planned": String(planned), "data-done": String(done)}));
    var agentExtra = [];
    if (liveBar) agentExtra.push(liveBar);
    agentExtra.push(el("div", {class: "x", text: toolLegend.length ? toolLegend.join(" \u00B7 ") : "idle"}));
    kpis.appendChild(pulseKpi("Agents running", fmtKpi(liveNow), null, agentExtra, {"data-kpi": "live", "data-live": String(liveNow)}));
    var tokExtra = [];
    if (tokChip.node) tokExtra.push(tokChip.node);
    /* A zero window still names the last run and the all-time total
       (the tiles' zero contract, on the pulse cell). */
    if (!allTok) {
      var lastTxt = "";
      try { if (snap.last_activity) lastTxt = "last run " + ago(nowS, snap.last_activity) + " \u00B7 "; } catch (e) { lastTxt = ""; }
      tokExtra.push(el("div", {class: "x", text: lastTxt + fmtKpi(allTimeTok) + " all time"}));
    }
    tokExtra.push(hasPayload
      ? pulseBars(hourTot, peak, true, "Hourly tokens across all workstreams, oldest first; the lighter bar is the current partial hour")
      : el("div", {class: "skel", "aria-hidden": "true"}));
    kpis.appendChild(pulseKpi("Tokens (24 h)", allTok ? fmtKpi(allTok) : "0 in 24 h", null, tokExtra,
      {"data-kpi": "tokens", "data-ws": "all", "data-hours": String(PULSE.WINDOW_H), "data-tokens": String(allTok), "data-tokens-all-time": String(allTimeTok), "data-delta": tokChip.cls}));
    var costExtra = [];
    if (costChip.node) costExtra.push(costChip.node);
    kpis.appendChild(pulseKpi("Est. cost (24 h)", fmtMoney(cost), null, costExtra, {"data-kpi": "cost", "data-ws": "all", "data-hours": String(PULSE.WINDOW_H), "data-cost-usd": String(cost), "data-cost-all-time": String(allTimeCost), "data-delta": costChip.cls}));
    var finExtraEls = [];
    if (finBar) finExtraEls.push(finBar);
    var finBits = [];
    [["ok", "\u2713"], ["part", "\u25D0"], ["blocked", "\u2298"], ["died", "\u2715"]].forEach(function (kv) {
      var n = +fv[kv[0]] || 0;
      if (n > 0 || finNow === 0) finBits.push(kv[1] + " " + fmtKpi(n) + " " + kv[0]);
    });
    if ((+fv.cancelled || 0) > 0) finBits.push("\u23F9 " + fmtKpi(fv.cancelled) + " cancelled");
    if ((+fv.unk || 0) > 0) finBits.push("\u25CB " + fmtKpi(fv.unk) + " no verdict");
    finExtraEls.push(el("div", {class: "x", text: finBits.length ? finBits.join(" \u00B7 ") + " (24 h)" : "none in 24 h"}));
    kpis.appendChild(pulseKpi("Finished (24 h)", fmtKpi(finNow), null, finExtraEls, {"data-kpi": "finished", "data-finished": String(finNow)}));
    c.appendChild(kpis);
    /* The unattributed control the tiles carry on workstream tabs: the
       same button (AdUnatt.toggleButton, shared flag per scope) and the
       same muted note, here on the overview's pulse strip. */
    if (typeof AdUnatt !== "undefined") {
      var unattN = cn.unattributed || null;
      var hidRuns = unattN && unattN.runs != null ? (+unattN.runs || 0) : 0;
      var hidTok = unattN && unattN.tokens != null ? (+unattN.tokens || 0) : null;
      var showU = AdUnatt.shown("all");
      if (showU || hidRuns > 0 || (hidTok || 0) > 0) {
        var urow = el("div", {class: "pulse-unatt"});
        if (!showU && (hidRuns > 0 || (hidTok || 0) > 0)) {
          var tnote = AdUnatt.noteText(hidRuns, hidTok, fmtKpi);
          urow.appendChild(el("span", {class: "unatt-note", style: "color:var(--muted);font-size:12px", text: tnote, title: tnote, "data-unatt-runs": String(hidRuns), "data-unatt-tokens": hidTok == null ? "n/a" : String(hidTok)}));
        }
        urow.appendChild(AdUnatt.toggleButton("all"));
        c.appendChild(urow);
      }
    }

    if (state.pulseFailed && !hasPayload) {
      c.appendChild(el("div", {class: "pulse-failed", text: "request failed"}));
      return c;
    }
    return c;
  }

  /* Overview-only spec. The key carries the payload stamp (so the
     usage fetch landing redraws this section once); pulseEnsure
     (same tick path as the activity progress payload) starts the
     usage fetch. */
  function pulseSpec(st, snap) {
    if (!st || st.ws !== "all") return null;
    if (typeof pulseEnsure === "function") pulseEnsure();
    return ["pulse",
      keyOf([snap.counts, snap.totals, snap.prev_24h, snap.last_activity,
        (state.wss || []).map(function (w) { return [w.ws, w.live_agents, w.tokens, w.state]; }),
        (snap.running || []).map(function (r) { return [r.run_id, r.ended_at]; }),
        planSum, pulseWsKey(), st.snapError,
        typeof AdUnatt !== "undefined" ? AdUnatt.shown("all") : false]),
      function () { return pulseCard(snap); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.PULSE = PULSE;
  AdPage.pulseWsKey = pulseWsKey;
  AdPage.pulseEnsure = pulseEnsure;
  AdPage.pulseSpec = pulseSpec;
  AdPage.pulseCard = pulseCard;
  global.PULSE = PULSE;
  global.pulseWsKey = pulseWsKey;
  global.pulseEnsure = pulseEnsure;
  global.pulseSpec = pulseSpec;
  global.pulseCard = pulseCard;
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.pulse = pulseCard;
  AdPage.cards.pulseSpec = pulseSpec;
})(window);
