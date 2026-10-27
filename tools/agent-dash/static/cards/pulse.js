/* Page card: overview pulse — at-a-glance activity for all workstreams.
   First section on the overview (?ws=all): a KPI strip (workstreams in
   progress, agents running, tokens and cost for the fixed 24 h window,
   finished agents) over the workstream momentum chart (one row per live
   workstream, slowest first). No text about state anywhere except the
   momentum glyphs' own title/aria-label text: no banners, toasts or
   notification rows.
   Reads: state.wss (rows from /api/workstreams), state.snap (counts,
   totals, prev_24h, running, by_workstream), state.pulseUsage (the
   per-workstream hour buckets fetched below, cached per minute).
   Exports (on window.AdPage, also kept as plain globals): pulseSpec,
   pulseCard, momentumFor (pure classifier, node-tested), PULSE (the one
   thresholds object), pulseHourly (pure bucket folder, node-tested),
   pulseWsKey, pulseEnsure.
   Needs: el, api, keyOf, tokTotal, fmtN, fmtUsd, ago, niceMax, planSum,
   progressFor, shown, showTip, hideTip (plain globals read at call time);
   window.AdPage/ADM/ADC, DashNav, secCache, drawMain, state. */
(function (global) {
  "use strict";

  /* The one thresholds object: tune nothing else. Times are seconds,
     ratios are unitless. */
  var PULSE = {
    WINDOW_H: 24,        /* fixed rolling window, hours */
    RECENT_H: 2,         /* recent = tokens in the last 2 h */
    BASE_H: 6,           /* base = mean hourly tokens over the 6 h before that */
    STUCK_IDLE_S: 20 * 60,  /* silent past this with live agents = stuck ... */
    WARN_IDLE_S: 20 * 60,   /* ... and the age cell turns amber here ... */
    CRIT_IDLE_S: 60 * 60,   /* ... red here (live agents only) */
    SLOW_RATIO: 0.5,     /* recent < half the base rate = slowing */
    CURRENT_MIN_FRAC: 0.25, /* the partial current hour counts at least this */
    MAX_DOTS: 12         /* live-agent dots per row before +N */
  };

  /* Pure classifier: one workstream's momentum class from data already on
     the page. wsRow carries live_agents and last_activity; buckets holds
     WINDOW_H per-hour token numbers for the workstream, oldest first,
     last entry the current partial hour; running holds this workstream's
     live run rows (checked for .stalled); now is epoch seconds.
     recent = last full hour + current hour scaled by elapsed fraction
     (min CURRENT_MIN_FRAC); base = mean over the BASE_H hours before
     those, computed only over hours with any tokens; ratio =
     recent / (RECENT_H * base).
     stuck: live agents and (any running row stalled, or silent past
     STUCK_IDLE_S, or nothing recent against a nonzero base).
     slowing: not stuck, base > 0, ratio < SLOW_RATIO.
     moving: not stuck, recent > 0 and ratio >= SLOW_RATIO
     (or base == 0 with recent > 0: a fresh start).
     idle: everything else (no live agents and no tokens in the window). */
  function momentumFor(wsRow, buckets, running, now) {
    wsRow = wsRow || {};
    buckets = buckets || [];
    running = running || [];
    if (!isFinite(+now)) now = Date.now() / 1000;
    var live = +wsRow.live_agents || 0;
    if (!live) live = running.length;
    var n = buckets.length;
    function numAt(i) {
      var v = +buckets[i];
      return isFinite(v) && v > 0 ? v : 0;
    }
    var frac = (now % 3600) / 3600;
    if (!(frac >= PULSE.CURRENT_MIN_FRAC)) frac = PULSE.CURRENT_MIN_FRAC;
    if (frac > 1) frac = 1;
    var recent, baseVals;
    if (n >= 2) {
      recent = numAt(n - 2) + numAt(n - 1) * frac;
      baseVals = buckets.slice(Math.max(0, n - 2 - PULSE.BASE_H), n - 2);
    } else if (n === 1) {
      recent = numAt(0) * frac;
      baseVals = [];
    } else {
      recent = 0;
      baseVals = [];
    }
    var baseSum = 0, baseN = 0, i;
    for (i = 0; i < baseVals.length; i++) {
      var v = +baseVals[i];
      if (isFinite(v) && v > 0) { baseSum += v; baseN++; }
    }
    var base = baseN ? baseSum / baseN : 0;
    /* No base to compare against: a fresh start with recent tokens
       reads exactly at the bar, so the >= below counts it moving. */
    var ratio = base > 0 ? recent / (PULSE.RECENT_H * base) : (recent > 0 ? PULSE.SLOW_RATIO : 0);
    var last = +wsRow.last_activity || 0;
    /* No recorded activity reads as silent past every threshold. */
    var idleFor = last > 0 ? now - last : 9e15;
    var anyStalled = false;
    for (i = 0; i < running.length; i++) {
      if (running[i] && running[i].stalled) { anyStalled = true; break; }
    }
    if (live > 0 && (anyStalled || idleFor > PULSE.STUCK_IDLE_S || (recent <= 0 && base > 0))) return "stuck";
    if (base > 0 && ratio < PULSE.SLOW_RATIO) return "slowing";
    if (recent > 0 && (base === 0 || ratio >= PULSE.SLOW_RATIO)) return "moving";
    return "idle";
  }

  /* Pure bucket folder: per-hour token numbers for one workstream from
     /api/usage group=workstream cells ({t, key, input, output,
     cache_read, cache_write}), oldest first over the WINDOW_H hours
     ending at the current hour; missing hours read 0. hEnd is the
     current-hour start (epoch seconds). */
  function pulseHourly(cells, ws, hEnd) {
    var out = [];
    var i, h;
    for (i = 0; i < PULSE.WINDOW_H; i++) out.push(0);
    if (!hEnd) return out;
    var byH = {};
    (cells || []).forEach(function (b) {
      if (!b || b.key !== ws) return;
      var t = +b.t;
      if (!isFinite(t)) return;
      var v = (+b.input || 0) + (+b.output || 0) + (+b.cache_read || 0) + (+b.cache_write || 0);
      if (!(v > 0)) return;
      byH[t] = (byH[t] || 0) + v;
    });
    for (i = 0; i < PULSE.WINDOW_H; i++) {
      h = hEnd - (PULSE.WINDOW_H - 1 - i) * 3600;
      if (byH[h]) out[i] = byH[h];
    }
    return out;
  }

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
      if (cur > 0) { cls = "new"; symbol = "\u25B2"; value = "new"; }
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

  /* Per-workstream hour buckets for the momentum chart, fetched once a
     minute like the activity progress payload (actProgKey): the section
     key below carries the payload stamp, so the arrival redraws the
     section once. A failed fetch marks state.pulseFailed and the card
     says "request failed" instead of drawing zeros. */
  var pulseInflight = false;
  function pulseSince(nowS) { return Math.floor(nowS - PULSE.WINDOW_H * 3600); }
  function pulseWsKey() {
    var p = (typeof state !== "undefined" && state.pulseUsage) || null;
    return "pulse|" + (p ? Math.floor(p.since / 60) : 0) + "|" + (p && p.buckets ? "1" : "0") + "|" + Math.floor(Date.now() / 60000);
  }
  function pulseEnsure() {
    if (typeof state === "undefined" || typeof api !== "function") return;
    var nowS = Date.now() / 1000, want = pulseSince(nowS);
    var p = state.pulseUsage;
    if (p && p.buckets && Math.abs(p.since - want) < 120 && (Date.now() - (p.at || 0)) < 60000) return;
    if (pulseInflight) return;
    pulseInflight = true;
    api("/api/usage", {group: "workstream", bucket: "hour", since: want}).then(function (j) {
      pulseInflight = false;
      if (typeof state === "undefined" || (state.view === "exec" && state.ws !== "all")) return;
      state.pulseUsage = {since: want, buckets: (j && j.buckets) || [], at: Date.now()};
      state.pulseFailed = false;
      try { delete secCache.pulse; } catch (e) {}
      if (typeof drawMain === "function") drawMain();
    }, function () {
      pulseInflight = false;
      if (typeof state === "undefined") return;
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
  function wsProgress(id) {
    try {
      if (typeof progressFor === "function") return progressFor(id);
    } catch (e) {}
    return null;
  }

  var MOMENT = {
    stuck: {glyph: "\u25A0", word: "stuck", color: "var(--critical)", order: 0},
    slowing: {glyph: "\u25BC", word: "slowing", color: "var(--warning)", order: 1},
    moving: {glyph: "\u25B2", word: "moving", color: "var(--good)", order: 2},
    idle: {glyph: "\u00B7", word: "idle", color: "var(--muted)", order: 3}
  };

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
    tokExtra.push(hasPayload
      ? pulseBars(hourTot, peak, true, "Hourly tokens across all workstreams, oldest first; the lighter bar is the current partial hour")
      : el("div", {class: "skel", "aria-hidden": "true"}));
    kpis.appendChild(pulseKpi("Tokens (24 h)", allTok ? fmtKpi(allTok) : "0 in 24 h", null, tokExtra,
      {"data-kpi": "tokens", "data-tokens": String(allTok), "data-tokens-all-time": String(allTimeTok), "data-delta": tokChip.cls}));
    var costExtra = [];
    if (costChip.node) costExtra.push(costChip.node);
    kpis.appendChild(pulseKpi("Est. cost (24 h)", fmtMoney(cost), null, costExtra, {"data-kpi": "cost", "data-cost-usd": String(cost), "data-cost-all-time": String(allTimeCost), "data-delta": costChip.cls}));
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

    /* Momentum chart: one row per non-archived workstream, slowest
       first. Done rows stay only with live agents or windowed tokens;
       unassigned stays only with live agents. */
    var momHead = el("div", {class: "pulse-mom-h"}, [el("h3", {text: "Workstream momentum"})]);
    c.appendChild(momHead);
    if (state.pulseFailed && !hasPayload) {
      c.appendChild(el("div", {class: "pulse-failed", text: "request failed"}));
      return c;
    }
    if (!hasPayload) {
      var sk = el("div", {class: "pulse-rows", "aria-busy": "true"});
      for (hi = 0; hi < 4; hi++) sk.appendChild(el("div", {class: "skel"}));
      c.appendChild(sk);
      return c;
    }

    var runsByWs = {};
    (snap.running || []).forEach(function (r) {
      var id = r && (r.workstream || r.ws);
      if (!id) return;
      (runsByWs[id] = runsByWs[id] || []).push(r);
    });
    var rows = [];
    wss.forEach(function (w) {
      if (!w || !w.ws) return;
      var st = wsStateOf(w);
      if (st === "archived" || st === "dropped") return;
      var live = +w.live_agents || 0;
      var toks = +w.tokens || 0;
      if (w.ws === "unassigned") { if (!(live > 0)) return; }
      else if (st === "done" && !(live > 0 || toks > 0)) return;
      var buckets = pulseHourly(cells, w.ws, hEnd);
      var wrunning = runsByWs[w.ws] || [];
      var cls = momentumFor({live_agents: live, last_activity: w.last_activity}, buckets, wrunning, nowS);
      rows.push({w: w, st: st, live: live, toks: toks, buckets: buckets, running: wrunning, cls: cls});
    });
    rows.sort(function (a, b) {
      var d = MOMENT[a.cls].order - MOMENT[b.cls].order;
      if (d) return d;
      d = b.live - a.live;
      if (d) return d;
      return b.toks - a.toks;
    });
    var rowMax = 1;
    rows.forEach(function (r) {
      r.buckets.forEach(function (v) { if (v > rowMax) rowMax = v; });
    });

    var takeaway = rows.length
      ? "Workstream momentum, slowest first: " + rows.slice(0, 3).map(function (r) { return r.w.ws + " " + MOMENT[r.cls].word; }).join(", ") +
        (rows.length > 3 ? ", and " + (rows.length - 3) + " more" : "")
      : "Workstream momentum: no open workstreams";
    var scroll = el("div", {class: "pulse-scroll"});
    var grid = el("div", {class: "pulse-grid", role: "list", "aria-label": takeaway});
    var head = el("div", {class: "pulse-row pulse-head", "aria-hidden": "true"});
    ["", "", "Workstream", "Plan", "Tokens / h", "Agents", "Active"].forEach(function (t) {
      head.appendChild(el("span", {class: "pulse-cell h", text: t}));
    });
    grid.appendChild(head);
    rows.forEach(function (r) {
      grid.appendChild(pulseRow(r, rowMax, nowS));
    });
    if (!rows.length) grid.appendChild(el("div", {class: "pulse-empty", text: "No open workstreams in the last 24 h."}));
    scroll.appendChild(grid);
    c.appendChild(scroll);
    return c;
  }

  function pulseRow(r, rowMax, nowS) {
    var w = r.w, m = MOMENT[r.cls];
    var row = el("button", {
      type: "button",
      class: "pulse-row m-" + r.cls,
      role: "listitem",
      "aria-label": w.ws + " " + (w.name || "") + ", " + m.word + ", " + r.live + " live agents",
      "data-ws": w.ws,
      "data-m": r.cls
    });
    var rail = el("span", {class: "pulse-cell rail"});
    rail.style.background = m.color;
    row.appendChild(rail);
    row.appendChild(el("span", {
      class: "pulse-glyph",
      text: m.glyph,
      title: w.ws + " is " + m.word,
      "aria-label": w.ws + " is " + m.word
    }));
    var idc = el("span", {class: "pulse-cell id"});
    idc.appendChild(el("b", {text: w.ws, title: w.ws + (w.name ? " " + w.name : "")}));
    if (w.name) idc.appendChild(el("span", {class: "nm", text: w.name, title: w.name}));
    var pillCls = r.st === "done" ? "done" : r.st === "paused" ? "paused" : r.st === "planned" ? "todo" : r.st === "active" ? "active" : "none";
    var pillWord = r.st === "done" ? "\u2713 Done" : r.st === "paused" ? "\u23F8 Paused" : r.st === "planned" ? "\u25CB Planned" : "\u25CF In progress";
    idc.appendChild(el("span", {class: "prog-pill " + pillCls, text: pillWord}));
    row.appendChild(idc);

    var pr = wsProgress(w.ws), pc = (pr && pr.counts) || {}, tot = (pr && pr.total) || 0, pct = (pr && pr.pct) || 0, pdone = (pr && pr.done) || 0;
    var progc = el("span", {class: "pulse-cell prog"});
    if (tot) {
      var bar = el("span", {class: "pulse-pbar", role: "img", "aria-label": w.ws + " " + pdone + " of " + tot + " tasks done (" + pct + "%)"});
      [["done", "var(--good)"], ["pr", "var(--warning)"], ["prog", "var(--s5)"], ["wip", "var(--s1)"]].forEach(function (sg) {
        if (!(pc[sg[0]] > 0)) return;
        var sp = el("span", {title: sg[0] + " " + pc[sg[0]] + " of " + tot});
        sp.style.width = (100 * pc[sg[0]] / tot) + "%";
        sp.style.background = sg[1];
        bar.appendChild(sp);
      });
      progc.appendChild(bar);
      progc.appendChild(el("span", {class: "pulse-pct", text: pct + "%"}));
    } else {
      progc.appendChild(el("span", {class: "pulse-noprog", text: "\u2014"}));
    }
    row.appendChild(progc);

    var spark = el("span", {class: "pulse-cell spark"});
    spark.appendChild(pulseBars(r.buckets, rowMax, true, w.ws + " hourly tokens, oldest first; the lighter bar is the current partial hour"));
    row.appendChild(spark);

    var agents = el("span", {class: "pulse-cell agents", "aria-label": r.live + " live agents on " + w.ws});
    var dots = r.running.slice(0, PULSE.MAX_DOTS);
    dots.forEach(function (run) {
      var fam = "other";
      try {
        if (typeof tierKey === "function" && typeof ADM !== "undefined") fam = ADM.famOf(tierKey(run));
      } catch (e) {}
      var d = el("span", {
        class: "pulse-dot" + (run && run.stalled ? " stalled" : ""),
        title: (run && (run.label || run.run_id) ? (run.label || run.run_id) : "agent") + (run && run.stalled ? " (stalled)" : "")
      });
      d.style.background = famColor(fam);
      agents.appendChild(d);
    });
    if (r.running.length > PULSE.MAX_DOTS) agents.appendChild(el("span", {class: "pulse-more", text: "+" + (r.running.length - PULSE.MAX_DOTS)}));
    else if (!r.running.length) agents.appendChild(el("span", {class: "pulse-none", text: "\u00B7", "aria-hidden": "true"}));
    row.appendChild(agents);

    var ageS = 0;
    try { ageS = ago(nowS, w.last_activity); } catch (e) { ageS = ""; }
    var ageCls = "pulse-cell age";
    if (r.live > 0 && w.last_activity) {
      var idleFor = nowS - (+w.last_activity || 0);
      if (idleFor > PULSE.CRIT_IDLE_S) ageCls += " crit";
      else if (idleFor > PULSE.WARN_IDLE_S) ageCls += " warn";
    }
    row.appendChild(el("span", {class: ageCls, text: ageS || "\u2014"}));
    row.addEventListener("click", function () {
      if (window.DashNav && typeof DashNav.openExec === "function") DashNav.openExec(w.ws);
    });
    return row;
  }

  /* Overview-only spec. The key carries the payload stamp, so the usage
     fetch landing redraws this section once; pulseEnsure (same tick path
     as the activity progress payload) starts the fetch. */
  function pulseSpec(st, snap) {
    if (!st || st.ws !== "all") return null;
    if (typeof pulseEnsure === "function") pulseEnsure();
    return ["pulse",
      keyOf([snap.counts, snap.totals, snap.prev_24h, snap.last_activity,
        (state.wss || []).map(function (w) { return [w.ws, w.live_agents, w.tokens, w.last_activity, w.state, w.name]; }),
        (snap.running || []).map(function (r) { return [r.run_id, r.workstream, r.stalled, r.label]; }),
        planSum, pulseWsKey(), st.snapError]),
      function () { return pulseCard(snap); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.PULSE = PULSE;
  AdPage.momentumFor = momentumFor;
  AdPage.pulseHourly = pulseHourly;
  AdPage.pulseWsKey = pulseWsKey;
  AdPage.pulseEnsure = pulseEnsure;
  AdPage.pulseSpec = pulseSpec;
  AdPage.pulseCard = pulseCard;
  global.PULSE = PULSE;
  global.momentumFor = momentumFor;
  global.pulseHourly = pulseHourly;
  global.pulseWsKey = pulseWsKey;
  global.pulseEnsure = pulseEnsure;
  global.pulseSpec = pulseSpec;
  global.pulseCard = pulseCard;
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.pulse = pulseCard;
  AdPage.cards.pulseSpec = pulseSpec;
})(window);
