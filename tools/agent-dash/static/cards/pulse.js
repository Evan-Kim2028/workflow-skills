/* Page card: overview pulse — at-a-glance activity for all workstreams.
   First section on the overview (?ws=all): a KPI strip (workstreams in
   progress, agents running, tokens and cost for the fixed 24 h window,
   finished agents) over the workstream momentum chart (one row per live
   workstream, slowest first). No text about state anywhere except the
   momentum glyphs' own title/aria-label text: no banners, toasts or
   notification rows.
   Reads: state.wss (rows from /api/workstreams, each with by_phase),
   state.snap (counts, totals, prev_24h, running, by_workstream),
   state.pulseUsage (the per-workstream hour buckets fetched below,
   cached per minute), state.pulseProg (per-workstream /api/ws/progress
   payloads, fetched lazily on first expand, cached per minute).
   Exports (on window.AdPage, also kept as plain globals): pulseSpec,
   pulseCard, momentumFor (pure classifier, node-tested), PULSE (the one
   thresholds object), pulseHourly (pure bucket folder, node-tested),
   pulsePhaseColor, pulsePhaseShares, pulseFoldRuns, pulseMileTicks
   (pure drill-down helpers, node-tested), pulseWsKey, pulseEnsure,
   pulseProgEnsure, pulseOpenRows, pulseRowOpen.
   Needs: el, s, api, keyOf, tokTotal, fmtN, fmtUsd, fmtDur, fmtDate,
   ago, niceMax, planSum, progressFor, shown, showTip, hideTip, axes,
   timeTicks, CW, progStep, progPctFmt, progStepRows, progMileRows,
   progTakeaway, progBest, progBestElapsed, openPlan, planLinear
   (plain globals read at call time);
   window.AdPage/ADM/ADC, AdPage.wsColor, AdUnatt (the unattributed
   toggle and note, core/unattributed.js), DashNav, secCache, drawMain,
   state. */
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
     slowing: live agents, not stuck, base > 0, ratio < SLOW_RATIO. A
     workstream with no live agents has stopped, it is not slowing, so
     it never reads slowing.
     moving: not stuck, recent > 0 and ratio >= SLOW_RATIO
     (or base == 0 with recent > 0: a fresh start).
     idle: everything else: no live agents and no recent tokens, or live
     agents with no tokens at all. */
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
    if (live > 0 && base > 0 && ratio < PULSE.SLOW_RATIO) return "slowing";
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

  /* Drill-down band colours: the same --ph-* design tokens the usage
     by-phase card paints (static/cards/phases.js PHASE_COLOR), pinned
     here so the detail strip and that card never drift apart. Pure:
     no DOM, no state. */
  var PULSE_PHASES = ["planning", "execution", "review", "coordination", "unclassified"];
  function pulsePhaseColor(phase) {
    return "var(--ph-" + (PULSE_PHASES.indexOf(phase) >= 0 ? phase : "unclassified") + ")";
  }

  /* Pure shares for the tokens-by-phase stacked bar: [{phase, value,
     share}] in PULSE_PHASES order over the by_phase row's token
     counts ({phase: {tokens}}); zero-total reads every share 0. */
  function pulsePhaseShares(byPhase) {
    var cells = PULSE_PHASES.map(function (ph) {
      var cell = (byPhase || {})[ph] || {};
      var v = +cell.tokens || 0;
      return {phase: ph, value: isFinite(v) && v > 0 ? v : 0, share: 0};
    });
    var total = 0, i;
    for (i = 0; i < cells.length; i++) total += cells[i].value;
    if (total > 0) for (i = 0; i < cells.length; i++) cells[i].share = cells[i].value / total;
    return {cells: cells, total: total};
  }

  /* Pure fold of runs_per_hour rows ([{t, n}]) into per-hour buckets:
     duplicate hours sum, hours sort ascending, {hours, counts, total}.
     The chart draws one faint bar per bucket on its own scale. */
  function pulseFoldRuns(rows) {
    var byH = {}, i;
    (rows || []).forEach(function (r) {
      if (!r) return;
      var t = +r.t, n = +r.n;
      if (!isFinite(t) || !(n > 0)) return;
      var h = Math.floor(t / 3600) * 3600;
      byH[h] = (byH[h] || 0) + n;
    });
    var hours = Object.keys(byH).map(Number).sort(function (a, b) { return a - b; });
    var counts = hours.map(function (h) { return byH[h]; });
    var total = 0;
    for (i = 0; i < counts.length; i++) total += counts[i];
    return {hours: hours, counts: counts, total: total};
  }

  /* Pure milestone tick placement for the detail chart: one tick per
     20/40/60/80/100 % mark on the x axis. Reached marks sit at their
     first-cross time (clamped into [from, to]) and draw filled;
     unreached marks (t null) sit at the axis end and draw hollow, so
     the unreached 100 % always reads as one hollow tick at the right. */
  function pulseMileTicks(milestones, from, to) {
    from = +from; to = +to;
    if (!isFinite(from) || !isFinite(to) || !(to > from)) { from = 0; to = 1; }
    return (milestones || []).map(function (m) {
      m = m || {};
      var reached = !!m.reached && isFinite(+m.t);
      var t = reached ? Math.max(from, Math.min(to, +m.t)) : to;
      return {pct: m.pct, t: t, done: +m.done || 0, elapsed: +m.elapsed || 0, reached: reached, hollow: !reached};
    });
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

  /* Per-workstream hour buckets for the momentum chart, fetched once a
     minute like the activity progress payload (actProgKey): the section
     key below carries the payload stamp, so the arrival redraws the
     section once. A failed fetch marks state.pulseFailed (the card says
     "request failed" instead of drawing zeros) and stamps
     state.pulseFailedMin with the failing minute, so the retry waits for
     the next minute: drawMain calls pulseEnsure again at once. */
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

  /* Drill-down open rows: the set of expanded workstream ids, kept in
     sessionStorage per scope like the disclosure helper (Running now),
     so several rows may stay open at once and expands survive
     re-renders. Pulse renders on the overview only, so the scope is
     always "all". Guarded: private mode falls back to closed. */
  var PULSE_ROWS_KEY = "agent-dash-pulse:v1:rows:all";
  function pulseOpenRows() {
    try {
      var raw = window.sessionStorage.getItem(PULSE_ROWS_KEY);
      if (!raw) return [];
      var list = JSON.parse(raw);
      return Array.isArray(list) ? list.filter(function (w) { return typeof w === "string" && w; }) : [];
    } catch (e) { return []; }
  }
  function pulseRowOpen(ws) { return pulseOpenRows().indexOf(ws) >= 0; }
  function pulseSetRow(ws, open) {
    var rows = pulseOpenRows(), i = rows.indexOf(ws);
    if (open && i < 0) rows.push(ws);
    else if (!open && i >= 0) rows.splice(i, 1);
    try {
      if (rows.length) window.sessionStorage.setItem(PULSE_ROWS_KEY, JSON.stringify(rows));
      else window.sessionStorage.removeItem(PULSE_ROWS_KEY);
    } catch (e) {}
  }
  /* Spec-key half of the open set: sorted ids joined, so an expand or
     collapse rebuilds the section once and stream ticks keep it. */
  function pulseOpenKey() { return pulseOpenRows().slice().sort().join(","); }

  /* Per-workstream progress payloads, fetched lazily on first expand
     and cached per minute (state.pulseProg[ws] = {data|error, at}).
     Arrival fills open panels in place (no focus steal); the spec key
     below carries the stamps so stream ticks keep the panels too. */
  var pulseProgInflight = {};
  function pulseProgGet(ws) {
    try {
      var c = state.pulseProg || {};
      return c[ws] || null;
    } catch (e) { return null; }
  }
  function pulseProgStamps() {
    var c = null;
    try { c = state.pulseProg || {}; } catch (e) { c = {}; }
    return Object.keys(c).sort().map(function (ws) {
      var at = 0;
      try { at = Math.floor(((c[ws] && c[ws].at) || 0) / 60000); } catch (e) { at = 0; }
      return ws + "@" + at;
    }).join(",");
  }
  function pulseProgEnsure(ws) {
    if (!ws || typeof state === "undefined" || typeof api !== "function") return;
    var hit = pulseProgGet(ws);
    /* Settled (payload, no-plan or failed) for this minute: a failure
       retries only in the next minute, never on every redraw. */
    if (hit && (hit.data || hit.nodata || hit.error) && Math.floor((hit.at || 0) / 60000) === pulseMinute()) return;
    if (pulseProgInflight[ws]) return;
    pulseProgInflight[ws] = true;
    /* The shared loader (activity.js) memoises per workstream and minute
       stamp, so this card and any other reader of the same scope share one
       request; a rejection reaches the handler below with the same status. */
    AdPage.loadWsProgress(ws, pulseMinute()).then(function (data) {
      delete pulseProgInflight[ws];
      if (typeof state === "undefined") return;
      state.pulseProg = state.pulseProg || {};
      state.pulseProg[ws] = {data: data || null, at: Date.now()};
      pulseFillDetail(ws);
    }, function (err) {
      delete pulseProgInflight[ws];
      if (typeof state === "undefined") return;
      state.pulseProg = state.pulseProg || {};
      /* A 404 is a workstream with no plan (the server says so), not a
         broken fetch: the panel reads empty, never failed. GET api()
         rejects with a bare Error("404"), so the message counts too. */
      var noPlan = err && (err.status === 404 || err.code === "plan_not_found" ||
        String((err && err.message) || "") === "404");
      var cur = state.pulseProg[ws];
      if (cur && cur.data) cur.at = Date.now();
      else state.pulseProg[ws] = noPlan ? {nodata: true, at: Date.now()} : {error: true, at: Date.now()};
      pulseFillDetail(ws);
    });
  }
  /* In-place fill after the lazy fetch lands: rebuilds only the open
     panel's body, so the row button keeps focus and nothing scrolls. */
  function pulseFillDetail(ws) {
    try {
      var host = document.querySelector('[data-pulse-detail="' + ws + '"]');
      if (!host) return;
      var body = pulseDetailBody(ws);
      host.innerHTML = "";
      if (body) host.appendChild(body);
    } catch (e) {}
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
    ["", "", "", "Workstream", "Plan", "Tokens / h", "Agents", "Active"].forEach(function (t) {
      head.appendChild(el("span", {class: "pulse-cell h", text: t}));
    });
    grid.appendChild(head);
    var openSet = pulseOpenRows();
    rows.forEach(function (r) {
      var item = el("div", {class: "pulse-item", role: "listitem", "data-pulse-item": r.w.ws});
      item.appendChild(pulseRow(r, rowMax, nowS));
      if (openSet.indexOf(r.w.ws) >= 0) {
        var det = el("div", {class: "pulse-detail", role: "region", "aria-label": r.w.ws + " detail", "data-pulse-detail": r.w.ws});
        var body = pulseDetailBody(r.w.ws, snap);
        if (body) det.appendChild(body);
        item.appendChild(det);
        pulseProgEnsure(r.w.ws);
      }
      grid.appendChild(item);
    });
    if (!rows.length) grid.appendChild(el("div", {class: "pulse-empty", text: "No open workstreams in the last 24 h."}));
    scroll.appendChild(grid);
    c.appendChild(scroll);
    return c;
  }

  /* Workstream colour for the drill-down panels: the shared wscolors
     helper (static/core/wscolors.js) with the old pulse blue fallback. */
  function pulseWsColor(ws) {
    try {
      if (global.AdPage && typeof AdPage.wsColor === "function") return AdPage.wsColor(ws, (typeof state !== "undefined") ? state : null);
    } catch (e) {}
    return "var(--s1)";
  }
  function pulseFmtDate(t) {
    try { return fmtDate(t); } catch (e) { return String(t); }
  }
  function pulseFmtDur(s) {
    try { return fmtDur(s); } catch (e) { return String(s); }
  }
  function pulseFmtModel(r) {
    try {
      if (typeof fullName === "function") return fullName(r.model || (typeof tierKey === "function" ? tierKey(r) : ""));
    } catch (e) {}
    return r.model || "";
  }
  function pulseTokOf(r) {
    try {
      if (typeof tokTotal === "function") return tokTotal(r.tokens || {});
    } catch (e) {}
    var t = r.tokens || {};
    return (+t.input || 0) + (+t.output || 0) + (+t.cache_read || 0) + (+t.cache_write || 0);
  }

  /* The workstream row plus its live runs for a detail panel, from the
     page workstream list and the snapshot (override for render, page
     state for the in-place fill). Null when the row is gone. */
  function pulseDetailContext(ws, snap) {
    var w = null, i, list = [];
    try { list = (typeof state !== "undefined" && state.wss) || []; } catch (e) { list = []; }
    for (i = 0; i < list.length; i++) {
      if (list[i] && list[i].ws === ws) { w = list[i]; break; }
    }
    if (!w) return null;
    var runs = [];
    try { runs = ((snap && snap.running) || (state.snap && state.snap.running)) || []; } catch (e) { runs = []; }
    var mine = runs.filter(function (r) { return r && (r.workstream || r.ws) === ws; });
    return {w: w, running: mine};
  }

  /* 1. Actions line: Open workstream (the old row-click target),
     Plan, and the Linear link when the plan carries one (the same
     planLinear lookup the workstream cards use). */
  function pulseDetailActions(w) {
    var acts = el("div", {class: "pulse-actions"});
    var bo = el("button", {type: "button", text: "Open workstream"});
    bo.addEventListener("click", function () {
      if (window.DashNav && typeof DashNav.openExec === "function") DashNav.openExec(w.ws);
    });
    acts.appendChild(bo);
    var bp = el("button", {type: "button", text: "Plan"});
    bp.addEventListener("click", function () {
      try { if (typeof openPlan === "function") openPlan(w.ws); } catch (e) {}
    });
    acts.appendChild(bp);
    var lk = null;
    try { lk = (typeof planLinear !== "undefined" && planLinear && planLinear[w.ws]) || null; } catch (e) { lk = null; }
    if (lk && lk.url) acts.appendChild(el("a", {class: "linear-link", href: lk.url, target: "_blank", rel: "noopener", "aria-label": "Open " + (w.name || w.ws) + " in Linear", text: "Linear \u2197"}));
    return acts;
  }

  /* Percent of tasks to a y pixel on a 0-100% plot (the actProgY rule,
     local so the panel never depends on the activity card loading). */
  function pulsePctY(pct, T, ph) {
    var v = Math.max(0, Math.min(100, +pct || 0));
    return T + ph - v / 100 * ph;
  }
  function pulseR1(x) { return Math.round(x * 10) / 10; }

  /* 2. Progress over time for one workstream: done as a step line in
     the workstream colour, milestones as ticks on the x axis (reached
     filled, unreached hollow at the axis end), runs_per_hour as faint
     bars behind on their own scale. A gap string reads as one muted
     footnote under the chart. Not separable from the burn-up card:
     that card paints project UTC-day buckets from /api/project/burnup
     with its own tabs and legend, while this paints one workstream's
     /api/ws/progress step points — so the smaller chart below reuses
     only the shared drawing pair (axes/timeTicks) and the progress
     step helpers (progStep and friends). */
  function pulseProgChart(ws, p) {
    var host = el("div", {class: "pulse-prog-chart"});
    var total = Math.max(1, (p && p.total) || 0);
    var pts = (p && p.points) || [];
    if (!p || !(p.total > 0) || !pts.length) {
      host.appendChild(el("div", {class: "pulse-muted", text: "No plan progress yet."}));
      return host;
    }
    var nowS = Date.now() / 1000;
    var from = +p.start || pts[0].t;
    var to = Math.max(+p.now || 0, +p.end || 0, nowS, from + 1);
    var W = 640, H = 220, L = 46, R = 8, T = 10, Bm = 52;
    try { if (typeof CW === "function") W = Math.max(560, CW()); } catch (e) {}
    var pw = W - L - R, ph = H - T - Bm;
    var X = function (t) { return L + pw * ((+t - from) / Math.max(1, to - from)); };
    var Y = function (pct) { return pulsePctY(pct, T, ph); };
    var color = pulseWsColor(ws);
    var takeaway = ws + " progress over time";
    try {
      if (typeof progTakeaway === "function") takeaway = progTakeaway(p.done, p.total, p.pct || 0, (typeof progBest === "function" ? progBest(p) : null), (typeof progBestElapsed === "function" ? progBestElapsed(p) : null), p.runs_total || 0);
    } catch (e) {}
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "data-strip": "pulse-prog", "data-ws": ws, "aria-label": takeaway});
    /* Runs bars first (behind): one faint bar per folded hour on its
       own scale, peak named in the Accessible takeaway above. */
    var fold = pulseFoldRuns(p.runs_per_hour);
    var runPeak = 0, i;
    for (i = 0; i < fold.counts.length; i++) { if (fold.counts[i] > runPeak) runPeak = fold.counts[i]; }
    if (runPeak > 0) {
      var bw = 3;
      if (fold.hours.length > 1) bw = Math.max(2, Math.min(28, (X(fold.hours[1]) - X(fold.hours[0])) - 2));
      else bw = Math.max(2, Math.min(28, pw / 24 - 2));
      fold.hours.forEach(function (h, hi) {
        var bh = ph * fold.counts[hi] / runPeak;
        var bar = s("rect", {x: pulseR1(X(h) - bw / 2), y: pulseR1(T + ph - bh), width: pulseR1(bw), height: Math.max(pulseR1(bh), 0.4), fill: "var(--s2)", opacity: ".22", "data-t": String(h), "data-v": String(fold.counts[hi])});
        bar.addEventListener("mousemove", function (ev) {
          try {
            if (typeof showTip === "function" && typeof progBarRows === "function") showTip(ev, pulseFmtDate(h), progBarRows(fold.counts[hi]));
          } catch (e) {}
        });
        bar.addEventListener("mouseleave", function () { try { if (typeof hideTip === "function") hideTip(); } catch (e) {} });
        svg.appendChild(bar);
      });
    }
    var doneOf = function (pt) { return (+pt.done || 0) / total * 100; };
    if (typeof progStep === "function") {
      var line = progStep({L: L}, X, Y, pts, doneOf);
      var first = pts[0], last = pts[pts.length - 1];
      svg.appendChild(s("path", {d: line + " L" + pulseR1(X(last.t)) + "," + pulseR1(T + ph) + " L" + pulseR1(X(first.t)) + "," + pulseR1(T + ph) + " Z", fill: color, opacity: ".12"}));
      svg.appendChild(s("path", {d: line, fill: "none", stroke: color, "stroke-width": "2.5"}));
    }
    pts.forEach(function (pt) {
      var dot = s("circle", {cx: pulseR1(X(pt.t)), cy: pulseR1(Y(doneOf(pt))), r: "2.6", fill: color, "data-t": String(pt.t), "data-v": String(pt.done)});
      dot.addEventListener("mousemove", function (ev) {
        try { if (typeof showTip === "function" && typeof progStepRows === "function") showTip(ev, pulseFmtDate(pt.t), progStepRows(pt.done, total, pt.prog || 0)); } catch (e) {}
      });
      dot.addEventListener("mouseleave", function () { try { if (typeof hideTip === "function") hideTip(); } catch (e) {} });
      svg.appendChild(dot);
    });
    /* Milestone ticks on the x axis: reached ones filled at their
       first-cross time, unreached ones hollow at the axis end. */
    var ticks = pulseMileTicks(p.milestones, from, to);
    var yb = T + ph;
    ticks.forEach(function (m) {
      var x = pulseR1(X(m.t));
      svg.appendChild(s("line", {x1: x, x2: x, y1: yb, y2: yb + 6, stroke: m.reached ? color : "var(--muted)", "stroke-width": "2"}));
      var dot = s("circle", {cx: x, cy: yb + 11, r: "4", fill: m.reached ? color : "var(--surface)", stroke: m.reached ? color : "var(--muted)", "stroke-width": "1.5", "data-milestone": String(m.pct), "data-t": String(Math.round(m.t)), "data-v": String(m.done)});
      if (m.reached) dot.addEventListener("mousemove", function (ev) {
        try { if (typeof showTip === "function" && typeof progMileRows === "function") showTip(ev, pulseFmtDate(m.t) + " — " + m.pct + "% first reached", progMileRows(m.done, total, m.elapsed)); } catch (e) {}
      });
      dot.addEventListener("mouseleave", function () { try { if (typeof hideTip === "function") hideTip(); } catch (e) {} });
      svg.appendChild(dot);
      var lab = s("text", {x: Math.min(x + 7, L + pw - 4), y: yb + 15, class: "plab"});
      lab.textContent = m.pct + "%";
      svg.appendChild(lab);
    });
    try {
      if (typeof axes === "function" && typeof timeTicks === "function") {
        var xt = timeTicks(from, to, X, Math.max(2, Math.floor(pw / 150)));
        var yFmt = (typeof progPctFmt === "function") ? progPctFmt : function (v) { return Math.round(v) + "%"; };
        var axg = axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: 100, yTicks: 4, yFmt: yFmt, xTicks: xt, yTitle: "% of tasks", xTitle: "time"});
        svg.insertBefore(axg, svg.firstChild);
      }
    } catch (e) {}
    host.appendChild(svg);
    if (p.gap) host.appendChild(el("p", {class: "pulse-gap", text: p.gap}));
    return host;
  }

  /* 3. Three-part strip: tokens by phase (one stacked bar with legend),
     task counts as number cells, live agents as a compact table. */
  function pulseDetailStrip(w, running, prog) {
    var strip = el("div", {class: "pulse-strip"});
    /* Tokens by phase from the /api/workstreams row's by_phase. */
    var phw = el("div", {class: "pulse-phase"});
    phw.appendChild(el("h4", {text: "Tokens by phase"}));
    var shares = pulsePhaseShares(w.by_phase);
    if (shares.total > 0) {
      var segs = shares.cells.map(function (c) {
        var word = c.phase === "unclassified" ? "unclassified" : c.phase;
        return [pulsePhaseColor(c.phase), c.value, word + " " + fmtKpi(c.value)];
      });
      phw.appendChild(pulseStack(segs, shares.total, "Lifetime tokens by kind of work: " + segs.map(function (sg) { return sg[2]; }).join(", ")));
      var lg = el("div", {class: "pulse-legend"});
      shares.cells.forEach(function (c) {
        if (!(c.value > 0)) return;
        var sw = el("i", {class: "sw"});
        sw.style.background = pulsePhaseColor(c.phase);
        lg.appendChild(el("span", {class: "lentry", "data-key": c.phase, "data-v": String(c.value), title: c.phase + " " + fmtKpi(c.value) + " tokens"}, [sw, el("span", {class: "lname", text: c.phase}), el("span", {class: "lv", text: fmtKpi(c.value)})]));
      });
      phw.appendChild(lg);
    } else {
      phw.appendChild(el("div", {class: "pulse-muted", text: "no tokens yet"}));
    }
    strip.appendChild(phw);
    /* Task counts from the progress payload (plan statuses), falling
       back to the plan progress the row already carries. */
    var counts = (prog && prog.counts) || null;
    if (!counts) {
      try {
        var pr = (typeof progressFor === "function") ? progressFor(w.ws) : null;
        counts = (pr && pr.counts) || {};
      } catch (e) { counts = {}; }
    }
    var ctw = el("div", {class: "pulse-counts"});
    ctw.appendChild(el("h4", {text: "Tasks"}));
    var cells = el("div", {class: "pulse-count-cells"});
    [["todo", "To do"], ["wip", "Doing"], ["pr", "In review"], ["done", "Done"], ["deferred", "Deferred"]].forEach(function (kv) {
      var n = (counts && +counts[kv[0]]) || 0;
      cells.appendChild(el("div", {class: "pulse-count", "data-count": kv[0], "data-v": String(n)}, [
        el("b", {text: fmtKpi(n)}),
        el("span", {text: kv[1]})
      ]));
    });
    ctw.appendChild(cells);
    strip.appendChild(ctw);
    /* Live agents from the snapshot's running rows for this workstream. */
    var agw = el("div", {class: "pulse-agents"});
    agw.appendChild(el("h4", {text: "Live agents (" + running.length + ")"}));
    if (!running.length) {
      agw.appendChild(el("div", {class: "pulse-muted", text: "no live agents"}));
    } else {
      var table = el("table", {class: "pulse-agent-t", "aria-label": "Live agents on " + w.ws});
      var head = el("tr", {});
      ["", "Label", "Model", "Elapsed", "Tokens"].forEach(function (t) {
        head.appendChild(el("th", {text: t}));
      });
      table.appendChild(head);
      running.forEach(function (r) {
        var tr = (r && r.stalled) ? el("tr", {class: "is-stalled"}) : el("tr", {});
        var dotc = el("td", {class: "c-dot"});
        if (r && r.stalled) dotc.appendChild(el("span", {class: "pulse-stall", title: "stalled"}));
        tr.appendChild(dotc);
        tr.appendChild(el("td", {class: "c-label", text: (r && (r.label || r.run_id)) || ""}));
        tr.appendChild(el("td", {class: "c-model", text: pulseFmtModel(r || {})}));
        tr.appendChild(el("td", {class: "c-num", text: pulseFmtDur((r && r.elapsed_s) || 0)}));
        tr.appendChild(el("td", {class: "c-num", text: fmtKpi(pulseTokOf(r || {}))}));
        table.appendChild(tr);
      });
      agw.appendChild(table);
    }
    strip.appendChild(agw);
    return strip;
  }

  function pulseDetailBody(ws, snap) {
    var ctx = pulseDetailContext(ws, snap);
    if (!ctx) return null;
    var body = el("div", {class: "pulse-detail-body"});
    body.appendChild(pulseDetailActions(ctx.w));
    var hit = pulseProgGet(ws);
    var chartHost = el("div", {class: "pulse-prog"});
    if (hit && hit.data) chartHost.appendChild(pulseProgChart(ws, hit.data));
    else if (hit && hit.nodata) chartHost.appendChild(el("div", {class: "pulse-muted", text: "No plan progress yet."}));
    else if (hit && hit.error) chartHost.appendChild(el("div", {class: "pulse-muted", text: "request failed"}));
    else chartHost.appendChild(el("div", {class: "skel", "aria-hidden": "true"}));
    body.appendChild(chartHost);
    body.appendChild(pulseDetailStrip(ctx.w, ctx.running, hit && hit.data));
    return body;
  }

  function pulseRow(r, rowMax, nowS) {
    var w = r.w, m = MOMENT[r.cls];
    var open = pulseRowOpen(w.ws);
    var row = el("button", {
      type: "button",
      class: "pulse-row m-" + r.cls,
      "aria-label": w.ws + " " + (w.name || "") + ", " + m.word + ", " + r.live + " live agents" + (open ? ", expanded" : ""),
      "aria-expanded": open ? "true" : "false",
      "data-ws": w.ws,
      "data-m": r.cls,
      "data-pulse-row": w.ws
    });
    row.appendChild(el("span", {
      class: "pulse-chev",
      text: open ? "\u25BE" : "\u25B8",
      "aria-hidden": "true",
      title: open ? "Collapse " + w.ws + " detail" : "Expand " + w.ws + " detail"
    }));
    var rail = el("span", {class: "pulse-cell rail"});
    rail.style.background = m.color;
    row.appendChild(rail);
    row.appendChild(el("span", {
      class: "pulse-glyph",
      text: m.glyph,
      title: w.ws + " is " + m.word
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
    /* Row click (Enter/Space fire click on the real button) toggles
       the in-place detail panel; opening the workstream tab moved to
       the panel's own Open workstream action. The open set lives in
       the spec key, so the redraw below (and every stream tick) keeps
       the panel; focus returns to the rebuilt row. */
    row.addEventListener("click", function () {
      pulseSetRow(w.ws, !pulseRowOpen(w.ws));
      try { delete secCache.pulse; } catch (e) {}
      try { if (typeof drawMain === "function") drawMain(); } catch (e2) {}
      try {
        var next = document.querySelector('[data-pulse-row="' + w.ws + '"]');
        if (next && next.focus) next.focus();
      } catch (e3) {}
    });
    return row;
  }

  /* Overview-only spec. The key carries the payload stamp (so the
     usage fetch landing redraws this section once), the open-row set
     (so expands survive stream ticks the way Running now does) and
     the per-workstream progress stamps (so a lazy arrival keeps its
     panel); pulseEnsure (same tick path as the activity progress
     payload) starts the usage fetch. */
  function pulseSpec(st, snap) {
    if (!st || st.ws !== "all") return null;
    if (typeof pulseEnsure === "function") pulseEnsure();
    return ["pulse",
      keyOf([snap.counts, snap.totals, snap.prev_24h, snap.last_activity,
        (state.wss || []).map(function (w) { return [w.ws, w.live_agents, w.tokens, w.last_activity, w.state, w.name, w.by_phase]; }),
        (snap.running || []).map(function (r) { return [r.run_id, r.workstream, r.stalled, r.label, r.elapsed_s, r.tokens, r.model]; }),
        planSum, pulseWsKey(), pulseOpenKey(), pulseProgStamps(), st.snapError,
        typeof AdUnatt !== "undefined" ? AdUnatt.shown("all") : false]),
      function () { return pulseCard(snap); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.PULSE = PULSE;
  AdPage.momentumFor = momentumFor;
  AdPage.pulseHourly = pulseHourly;
  AdPage.pulsePhaseColor = pulsePhaseColor;
  AdPage.pulsePhaseShares = pulsePhaseShares;
  AdPage.pulseFoldRuns = pulseFoldRuns;
  AdPage.pulseMileTicks = pulseMileTicks;
  AdPage.pulseWsKey = pulseWsKey;
  AdPage.pulseEnsure = pulseEnsure;
  AdPage.pulseProgEnsure = pulseProgEnsure;
  AdPage.pulseOpenRows = pulseOpenRows;
  AdPage.pulseRowOpen = pulseRowOpen;
  AdPage.pulseSpec = pulseSpec;
  AdPage.pulseCard = pulseCard;
  global.PULSE = PULSE;
  global.momentumFor = momentumFor;
  global.pulseHourly = pulseHourly;
  global.pulsePhaseColor = pulsePhaseColor;
  global.pulsePhaseShares = pulsePhaseShares;
  global.pulseFoldRuns = pulseFoldRuns;
  global.pulseMileTicks = pulseMileTicks;
  global.pulseWsKey = pulseWsKey;
  global.pulseEnsure = pulseEnsure;
  global.pulseProgEnsure = pulseProgEnsure;
  global.pulseOpenRows = pulseOpenRows;
  global.pulseRowOpen = pulseRowOpen;
  global.pulseSpec = pulseSpec;
  global.pulseCard = pulseCard;
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.pulse = pulseCard;
  AdPage.cards.pulseSpec = pulseSpec;
})(window);
