/* Page card: Planning versus execution (?view=project, slot exec-phases;
   the all-workstreams variant on the overall Execution view; the
   workstream variant as the last section of each workstream Execution
   tab, below Finished).
   The project and overall cards paint GET /api/project/phases
   (../fixtures/phases.json under ?fixtures): stacked horizontal bars per
   workstream with kind-of-work colours, a tokens/cost toggle, scope tabs,
   totals, and a plain-words takeaway, sorted by total descending.
   Clicking a workstream opens its tab. The workstream card paints the
   same payload for the open tab: per-day stacked columns by kind of
   work (planning, execution, review, coordination, unclassified grey)
   with a Cost | Tokens toggle and a Per day | Cumulative toggle
   (per-day stacked bars by default, the cumulative lines behind the
   toggle), so the planning spend reads as a line that flattens when
   planning ends. Unclassified is its own grey bucket, never merged.
   Reads: state.phaseUsage (per scope), state.phaseUsageError,
   state.phaseUsageAt, state.view, state.ws, state.wss/wssAll.
   Exports (on window.AdPage.cards, also kept as plain globals):
   phaseUsageSpec, phaseUsageCard, phaseUsageAllSpec, phaseUsageWsSpec,
   phaseUsageWsCard, phaseBar, phaseShares, cumSum, phaseStackDays.
   Needs: el, s, CW, axes, niceMax, fmtN, fmtUsd, showTip, hideTip,
   keyOf, drawMain, api, go, secCache, state (plain globals). */
(function (global) {
  "use strict";
  var PHASES = ["planning", "execution", "review", "coordination", "unclassified"];
  var PHASE_WORD = {planning: "Planning", execution: "Execution", review: "Review", coordination: "Coordination", unclassified: "Unclassified"};
  var PHASE_GLYPH = {planning: "\u25C6", execution: "\u25A0", review: "\u25B2", coordination: "\u25CF", unclassified: "\u25CB"};
  var PHASE_COLOR = {planning: "var(--ph-planning)", execution: "var(--ph-execution)", review: "var(--ph-review)", coordination: "var(--ph-coordination)", unclassified: "var(--ph-unclassified)"};
  var SCOPES = ["24h", "7d", "all"];
  var SCOPE_WORD = {"24h": "24 h", "7d": "7 days", "all": "All time"};
  var PHASE_TTL_MS = 120000;
  var phaseScope = "24h";
  var phaseMetric = "cost";

  /* Pure: the cell value under a metric. Endpoint cells carry
     billed_tokens; row cells (by_phase on workstream rows) carry tokens. */
  function phaseValue(cell, metric) {
    if (!cell) return 0;
    if (metric === "cost") return +cell.cost_usd || 0;
    if (cell.billed_tokens != null) return +cell.billed_tokens || 0;
    return +cell.tokens || 0;
  }

  /* Pure: shares of a [{phase, value}] list; shares sum to 1, and a
     zero total shares nothing instead of dividing by zero. */
  function phaseShares(cells) {
    var total = 0;
    (cells || []).forEach(function (c) { total += (+c.value || 0); });
    return (cells || []).map(function (c) {
      return {phase: c.phase, value: +c.value || 0, share: total > 0 ? (+c.value || 0) / total : 0};
    });
  }

  /* Pure: running totals of a per-day series. */
  function cumSum(values) {
    var out = [], running = 0, i;
    for (i = 0; i < (values || []).length; i++) { running += (+values[i] || 0); out.push(running); }
    return out;
  }

  /* Pure: per-day stacks for one workstream item. Each day becomes one
     column with one segment per kind of work in PHASES order
     (unclassified last): {phase, value, y0, y1, share}. Empty days keep
     zero totals and zero offsets instead of dividing by zero. The
     optional range ({since, until} in unix seconds) filters days, so a
     shared time filter can pass through without this file depending on
     its module. */
  function phaseStackDays(item, metric, range) {
    var days = (item && item.days) || [];
    var series = (item && item.series) || {};
    var key = metric === "cost" ? "cost" : "tokens";
    var out = [], max = 0, grand = 0, i, p;
    for (i = 0; i < days.length; i++) {
      if (range && range.since != null && +days[i] < +range.since) continue;
      if (range && range.until != null && +days[i] > +range.until) continue;
      var total = 0, vals = [];
      for (p = 0; p < PHASES.length; p++) {
        var arr = ((series[PHASES[p]] || {})[key]) || [];
        var v = +arr[i] || 0;
        vals.push(v);
        total += v;
      }
      var segs = [], y0 = 0;
      for (p = 0; p < PHASES.length; p++) {
        var value = vals[p];
        segs.push({phase: PHASES[p], value: value, y0: y0, y1: y0 + value,
          share: total > 0 ? value / total : 0});
        y0 += value;
      }
      out.push({day: days[i], total: total, segs: segs});
      if (total > max) max = total;
      grand += total;
    }
    return {days: out.map(function (d) { return d.day; }), stacks: out, max: max, grand: grand};
  }

  /* Pure: workstream rows sorted by their total under a metric,
     descending. Zero-total rows sink to the end in stable order. */
  function phaseSortByTotal(items, metric) {
    var scored = (items || []).map(function (item, idx) {
      var sum = 0;
      PHASES.forEach(function (ph) { sum += phaseValue((item.by_phase || {})[ph], metric); });
      return {item: item, total: sum, idx: idx};
    });
    scored.sort(function (a, b) { return (b.total - a.total) || (a.idx - b.idx); });
    return scored.map(function (s) { return s.item; });
  }

  var phaseWsView = "bars";

  /* Exact integers with thousand separators and exact dollars, without
     locale formatting (page rule: compact fmt helpers for axes, exact
     text only inside tooltips and titles). */
  function phaseExact(n) {
    var s = String(Math.round(+n || 0)), out = "", i;
    var neg = s.charAt(0) === "-";
    if (neg) s = s.slice(1);
    for (i = 0; i < s.length; i++) {
      if (i && (s.length - i) % 3 === 0) out += ",";
      out += s.charAt(i);
    }
    return (neg ? "-" : "") + out;
  }

  function phaseExactUsd(n) {
    var cents = Math.round((+n || 0) * 100), neg = cents < 0;
    if (neg) cents = -cents;
    var body = phaseExact(Math.floor(cents / 100)) + "." + (cents % 100 < 10 ? "0" : "") + String(cents % 100);
    return (neg ? "-$" : "$") + body;
  }

  /* Every painted number routes through the shared compact formatters. */
  function phaseFmtN(n) { return fmtN(n); }
  function phaseFmtUsd(n) { return fmtUsd(n); }
  function phaseFmt(metric, n) { return metric === "cost" ? phaseFmtUsd(n) : phaseFmtN(n); }

  function phaseExactFmt(metric, n) { return metric === "cost" ? phaseExactUsd(n) : phaseExact(n) + " tokens"; }

  function wsLabel(ws, name) {
    if (!name || String(name).toUpperCase() === String(ws).toUpperCase()) return String(name || ws);
    return name + " (" + ws + ")";
  }

  function phasePayload() {
    return phasePayloadFor(phaseScope);
  }

  function phasePayloadFor(scope) {
    var all = state.phaseUsage || {};
    return all[scope] || null;
  }

  /* The home slot caches by state.seq, which a phases arrival does not
     move: drop the slot section (the burn-up pattern) so drawMain
     repaints this card instead of keeping "Loading". */
  function phaseRedraw() {
    try { delete secCache["exec-phases"]; } catch (e) {}
    try { delete secCache["phases-ws"]; } catch (e) {}
    try { delete secCache["phases"]; } catch (e) {}
    try { delete secCache["phases-all"]; } catch (e) {}
    drawMain();
  }

  function ensurePhaseUsage() {
    ensurePhaseUsageFor(phaseScope);
  }

  /* The workstream card always reads the lifetime scope, so its legend
     and cumulative chart agree with its lifetime bar. */
  function ensurePhaseUsageFor(scope) {
    var all = state.phaseUsage || {};
    var stale = !state.phaseUsageAt || !state.phaseUsageAt[scope] || (Date.now() - state.phaseUsageAt[scope] > PHASE_TTL_MS);
    if (all[scope] || state._phaseLoading || !stale) return;
    state._phaseLoading = true;
    api("/api/project/phases", {scope: scope}).then(function (j) {
      state._phaseLoading = false;
      state.phaseUsage = state.phaseUsage || {};
      state.phaseUsage[scope] = j || null;
      state.phaseUsageError = null;
      state.phaseUsageAt = state.phaseUsageAt || {};
      state.phaseUsageAt[scope] = Date.now();
      phaseRedraw();
    }, function (e) {
      state._phaseLoading = false;
      state.phaseUsageError = String((e && (e.hint || e.error || e.message)) || e || "fetch failed");
      phaseRedraw();
    });
  }

  function phaseSegTabs(host) {
    var seg = el("div", {class: "seg-ctl phase-tabs", role: "group", "aria-label": "Usage window"});
    SCOPES.forEach(function (sc) {
      seg.appendChild(el("button", {type: "button", "aria-pressed": String(phaseScope === sc), onclick: function () { phaseScope = sc; phaseRedraw(); }}, [SCOPE_WORD[sc]]));
    });
    host.appendChild(seg);
    var met = el("div", {class: "seg-ctl phase-tabs", role: "group", "aria-label": "Usage measure"});
    [["tokens", "Tokens"], ["cost", "Cost"]].forEach(function (t) {
      met.appendChild(el("button", {type: "button", "aria-pressed": String(phaseMetric === t[0]), onclick: function () { phaseMetric = t[0]; phaseRedraw(); }}, [t[1]]));
    });
    host.appendChild(met);
  }

  /* Legend: every kind of work is a glyph plus a word plus its swatch,
     never colour alone; each entry carries raw values for agents. */
  function phaseLegend(host, cells, metric) {
    var lg = el("div", {class: "legend phase-legend"});
    var grand = 0;
    cells.forEach(function (c) { grand += (+c.value || 0); });
    lg.appendChild(el("span", {class: "lhead", text: "Kind of work"}));
    lg.appendChild(el("span", {class: "ltotal", "data-legend-total": String(grand), title: "All kinds of work in this view"}, [el("b", {text: "Total " + phaseFmt(metric, grand)})]));
    phaseShares(cells).forEach(function (c) {
      if (!c.value) return;
      var sp = el("span", {class: "lentry", "data-key": c.phase, "data-v": String(c.value),
        title: PHASE_WORD[c.phase] + " " + phaseExactFmt(metric, c.value)});
      var sw = el("i", {class: "sw"});
      sw.style.background = PHASE_COLOR[c.phase];
      sp.appendChild(sw);
      sp.appendChild(el("span", {class: "lname", text: PHASE_GLYPH[c.phase] + " " + PHASE_WORD[c.phase]}));
      sp.appendChild(el("span", {class: "lv", text: phaseFmt(metric, c.value)}));
      lg.appendChild(sp);
    });
    host.appendChild(lg);
  }

  /* One stacked horizontal bar row: the workstream name opens its tab,
     every segment tooltips its exact value. */
  function phaseRow(host, item, metric) {
    var cells = PHASES.map(function (ph) {
      return {phase: ph, value: phaseValue((item.by_phase || {})[ph], metric)};
    });
    var total = 0;
    cells.forEach(function (c) { total += c.value; });
    if (!(total > 0)) return false;
    var row = el("div", {class: "ph-row", "data-ws": item.ws, "data-v": String(total)});
    var label = wsLabel(item.ws, item.name);
    var open = el("button", {type: "button", class: "ph-name", text: label, title: label});
    open.setAttribute("aria-label", "Open the " + label + " tab");
    open.addEventListener("click", function () { go(item.ws); });
    row.appendChild(open);
    var bar = el("div", {class: "ph-bar", role: "img",
      "aria-label": label + ": " + phaseShares(cells).filter(function (c) { return c.value > 0; }).map(function (c) {
        return PHASE_WORD[c.phase] + " " + phaseFmt(metric, c.value);
      }).join(", ")});
    phaseShares(cells).forEach(function (c) {
      if (!(c.value > 0) || !(c.share > 0)) return;
      var seg = el("div", {class: "ph-seg", "data-key": c.phase, "data-v": String(c.value),
        title: label + " \u00B7 " + PHASE_WORD[c.phase] + " " + phaseExactFmt(metric, c.value)});
      seg.style.width = (c.share * 100) + "%";
      seg.style.background = PHASE_COLOR[c.phase];
      seg.addEventListener("mousemove", function (ev) {
        showTip(ev, label, [{k: PHASE_GLYPH[c.phase] + " " + PHASE_WORD[c.phase], v: phaseExactFmt(metric, c.value), c: PHASE_COLOR[c.phase]},
          {k: "Total", v: phaseExactFmt(metric, total), c: "var(--ink)"}]);
      });
      seg.addEventListener("mouseleave", hideTip);
      bar.appendChild(seg);
    });
    row.appendChild(bar);
    row.appendChild(el("span", {class: "ph-total", "data-v": String(total), text: phaseFmt(metric, total)}));
    host.appendChild(row);
    return true;
  }

  /* The plain-words takeaway: which workstream dominates this view and
     which kind of work took most of it. Names first, never bare ids. */
  function phaseTakeaway(items, metric) {
    var scope = SCOPE_WORD[phaseScope] === "All time" ? "so far" : "in the last " + SCOPE_WORD[phaseScope];
    var top = null, topValue = 0;
    (items || []).forEach(function (item) {
      var sum = 0;
      PHASES.forEach(function (ph) { sum += phaseValue((item.by_phase || {})[ph], metric); });
      if (sum > topValue) { topValue = sum; top = item; }
    });
    if (!top || !(topValue > 0)) return null;
    var cells = PHASES.map(function (ph) { return {phase: ph, value: phaseValue((top.by_phase || {})[ph], metric)}; });
    var lead = {phase: "unclassified", value: 0};
    cells.forEach(function (c) { if (c.value > lead.value) lead = c; });
    var label = wsLabel(top.ws, top.name);
    var unit = metric === "cost" ? "cost" : "tokens";
    if (lead.phase === "unclassified")
      return "Most of " + label + "\u2019s " + unit + " " + scope + " could not be split by label \u2014 it reads Unclassified.";
    return "Most of " + label + "\u2019s " + unit + " " + scope + " was " + PHASE_WORD[lead.phase].toLowerCase() + ".";
  }

  function phaseUsageCard() {
    var c = el("div", {class: "card phase-usage", "data-nav": "Planning versus execution"});
    c.appendChild(el("h2", {text: "Planning versus execution"}));
    var data = phasePayload();
    c.appendChild(el("p", {class: "sub", text: (data && data.caption) || "Planning versus execution per workstream."}));
    phaseSegTabs(c);
    if (state.phaseUsageError && !data) {
      c.appendChild(el("div", {class: "empty err", "data-failed-scope": "project", text: "request failed \u2014 " + state.phaseUsageError}));
      return c;
    }
    if (!data) {
      ensurePhaseUsage();
      c.appendChild(el("div", {class: "empty", text: "Loading planning versus execution\u2026"}));
      return c;
    }
    var items = phaseSortByTotal((data.workstreams || []).filter(function (w) { return w.ws !== "Other"; }), phaseMetric);
    var other = (data.workstreams || []).filter(function (w) { return w.ws === "Other"; })[0];
    var grandCells = PHASES.map(function (ph) {
      var sum = 0;
      (data.workstreams || []).forEach(function (w) { sum += phaseValue((w.by_phase || {})[ph], phaseMetric); });
      return {phase: ph, value: sum};
    });
    phaseLegend(c, grandCells, phaseMetric);
    var list = el("div", {class: "ph-list"});
    var shown = 0;
    items.forEach(function (item) { if (phaseRow(list, item, phaseMetric)) shown++; });
    if (!shown) {
      c.appendChild(el("div", {class: "empty", text: "No usage recorded in this window."}));
      return c;
    }
    c.appendChild(list);
    var grand = 0;
    grandCells.forEach(function (cell) { grand += cell.value; });
    var foot = el("p", {class: "ph-foot"});
    foot.appendChild(el("span", {text: "Project total " + phaseFmt(phaseMetric, grand)}));
    if (other) {
      var oSum = 0;
      PHASES.forEach(function (ph) { oSum += phaseValue((other.by_phase || {})[ph], phaseMetric); });
      if (oSum > 0) foot.appendChild(el("span", {class: "ph-other", text: " \u00B7 hidden lanes read Other: " + phaseFmt(phaseMetric, oSum)}));
    }
    var unc = (data.workstreams || []).reduce(function (acc, w) {
      return acc + phaseValue(((w.by_phase || {}).unclassified), phaseMetric);
    }, 0);
    if (grand > 0) {
      var pct = Math.round(unc / grand * 1000) / 10;
      foot.appendChild(el("span", {class: "ph-other", text: " \u00B7 " + pct + "% reads Unclassified"}));
    }
    c.appendChild(foot);
    var take = phaseTakeaway(data.workstreams, phaseMetric);
    if (take) c.appendChild(el("p", {class: "ph-takeaway", text: take}));
    return c;
  }

  /* Registry spec for the Project home slot exec-phases. */
  function phaseUsageSpec(passedState, snap) {
    return ["phases", keyOf([state.phaseUsage, state.phaseUsageError, state.seq, phaseScope, phaseMetric]), function () { return phaseUsageCard(); }, ""];
  }

  /* Registry spec for the overall Execution view (all workstreams): the
     same stacked bars as the Project home card, as its own bottom
     section below Finished. */
  function phaseUsageAllSpec(passedState, snap) {
    return ["phases-all", keyOf([state.phaseUsage, state.phaseUsageError, state.seq, phaseScope, phaseMetric]), function () { return phaseUsageCard(); }, "margin-top:14px"];
  }

  /* Thin two-segment bar for a management table row: planning versus
     execution share of lifetime tokens, the rest of the kinds of work
     named in the tooltip. Returns null when the row carries no split. */
  function phaseBar(byPhase) {
    if (!byPhase) return null;
    var cells = PHASES.map(function (ph) { return {phase: ph, value: phaseValue(byPhase[ph], "tokens")}; });
    var total = 0;
    cells.forEach(function (c) { total += c.value; });
    if (!(total > 0)) return null;
    var shares = phaseShares(cells);
    var bar = el("div", {class: "mg-phase", role: "img"});
    var tip = shares.filter(function (c) { return c.value > 0; }).map(function (c) {
      return PHASE_WORD[c.phase] + " " + phaseExact(c.value);
    }).join(" \u00B7 ");
    bar.setAttribute("aria-label", "Lifetime tokens by kind of work: " + tip);
    bar.title = "Lifetime tokens by kind of work \u2014 " + tip + " \u00B7 " + phaseExact(total) + " total";
    ["planning", "execution"].forEach(function (ph) {
      var cell = null;
      shares.forEach(function (c) { if (c.phase === ph) cell = c; });
      if (!cell || !(cell.share > 0)) return;
      var seg = el("div", {class: "mg-phase-seg", "data-key": ph, "data-v": String(cell.value)});
      seg.style.width = (cell.share * 100) + "%";
      seg.style.background = PHASE_COLOR[ph];
      bar.appendChild(seg);
    });
    return bar;
  }

  function wsRow() {
    var id = state.ws ? String(state.ws).toUpperCase() : "";
    var rows = (state.wssAll && state.wssAll.length ? state.wssAll : state.wss) || [];
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].ws).toUpperCase() === id) return rows[i];
    }
    return null;
  }

  /* UTC day label from date parts (never a locale string, so shots agree). */
  function phaseDay(t) {
    var d = new Date(+t * 1000);
    var m = d.getUTCMonth() + 1, day = d.getUTCDate();
    return d.getUTCFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (day < 10 ? "0" + day : day);
  }

  /* Cumulative lines by kind of work over time for one workstream, behind
     the Cumulative toggle: one line per kind with spend (cost or tokens
     via metric), planning first so the planning line flattening reads
     as planning ending. */
  function phaseCumulative(host, item, metric) {
    var m = metric === "tokens" ? "tokens" : "cost";
    var cumKey = m === "cost" ? "cumulative_cost" : "cumulative_tokens";
    var days = item.days || [];
    if (!days.length) return false;
    var order = PHASES.filter(function (ph) {
      var cum = ((item.series || {})[ph] || {})[cumKey] || [];
      return cum.length && cum[cum.length - 1] > 0;
    });
    if (!order.length) return false;
    var peak = 0;
    order.forEach(function (ph) {
      ((item.series || {})[ph][cumKey] || []).forEach(function (v) { if (v > peak) peak = v; });
    });
    var nice = niceMax(peak);
    var yFmt = m === "cost" ? fmtUsd : fmtN;
    var exact = m === "cost" ? phaseExactUsd : function (v) { return phaseExact(v) + " tokens"; };
    var W = CW(), H = 240, L = 64, R = 14, T = 24, Bm = 44;
    var pw = W - L - R, ph = H - T - Bm;
    var n = Math.max(1, days.length);
    var X = function (t) { return L + ((t - days[0]) / 86400) * (pw / n) + (pw / n) / 2; };
    var Y = function (v) { return T + ph - v / nice * ph; };
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img",
      "aria-label": "Cumulative " + m + " by kind of work over time for " + wsLabel(item.ws, item.name)});
    order.forEach(function (phase) {
      var cum = item.series[phase][cumKey] || [];
      var line = cum.map(function (v, i) { return X(days[i]) + "," + Y(v); }).join(" ");
      svg.appendChild(s("polyline", {points: line, fill: "none", stroke: PHASE_COLOR[phase], "stroke-width": "2.2", "data-key": phase, "data-v": String(cum.length ? cum[cum.length - 1] : 0)}));
      var last = cum.length ? cum[cum.length - 1] : 0;
      var dot = s("circle", {cx: X(days[days.length - 1]), cy: Y(last), r: "3.5", fill: PHASE_COLOR[phase], "data-key": phase, "data-v": String(last)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, PHASE_GLYPH[phase] + " " + PHASE_WORD[phase], [{k: "Cumulative", v: exact(last), c: PHASE_COLOR[phase]}]);
      });
      dot.addEventListener("mouseleave", hideTip);
      svg.appendChild(dot);
    });
    var maxTicks = Math.max(2, Math.floor(pw / 70));
    var step = Math.max(1, Math.ceil(days.length / maxTicks));
    var xt = [];
    for (var i = 0; i < days.length; i += step) {
      var lastTick = i + step >= days.length;
      xt.push({x: X(days[i]) + (i === 0 ? 4 : lastTick ? -4 : 0),
        label: phaseDay(days[i]), anchor: i === 0 ? "start" : lastTick ? "end" : "middle"});
    }
    var axg = axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, yTicks: 4, yFmt: yFmt, xTicks: xt, yTitle: m === "cost" ? "USD cumulative" : "tokens cumulative", xTitle: "UTC day"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
    return true;
  }

  /* Per-day stacked columns for one workstream: one column per UTC day,
     one rect segment per kind of work from the by-day series. The shared
     axes() helper draws both axes; compact formatters label them while
     tooltips carry exact values. Totals label the last few bars. */
  function phaseStackedBars(host, item, metric, range) {
    var m = metric === "tokens" ? "tokens" : "cost";
    var stacked = phaseStackDays(item, m, range);
    if (!stacked.stacks.length || !(stacked.grand > 0)) return false;
    var label = wsLabel(item.ws, item.name);
    var yFmt = m === "cost" ? fmtUsd : fmtN;
    var nice = niceMax(stacked.max);
    var W = CW(), H = 280, L = 64, R = 14, T = 30, Bm = 44;
    var pw = W - L - R, ph = H - T - Bm;
    var n = stacked.stacks.length;
    var slot = pw / Math.max(1, n), bw = Math.max(3, Math.min(28, slot - 4));
    var GAP = 2;
    var X = function (i) { return L + i * slot + (slot - bw) / 2; };
    var Y = function (v) { return T + ph - v / nice * ph; };
    var unit = m === "cost" ? "cost" : "tokens";
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img",
      "aria-label": "Per-day " + unit + " stacked by kind of work for " + label + ": " + phaseFmt(m, stacked.grand) + " total"});
    stacked.stacks.forEach(function (col, i) {
      var x = X(i), y0 = T + ph;
      var order = [];
      col.segs.forEach(function (sg, ki) { if (sg.value > 0) order.push(ki); });
      col.segs.forEach(function (sg, ki) {
        if (!(sg.value > 0)) return;
        var h = ph * sg.value / nice, top = Y(sg.y1);
        var gap = ki !== order[0] ? GAP : 0;
        var y = top + gap, hh = h - gap;
        if (hh < 0) { hh = 0; y = y0; }
        var r = s("rect", {x: x, y: y, width: bw, height: Math.max(hh, 0.4), fill: PHASE_COLOR[sg.phase],
          "data-key": sg.phase, "data-v": String(sg.value), "data-t": String(col.day)});
        svg.appendChild(r); y0 = top;
      });
      var pct = function (sg) { return col.total > 0 ? Math.round(sg.value / col.total * 1000) / 10 : 0; };
      var tipRows = col.segs.filter(function (sg) { return sg.value > 0; }).map(function (sg) {
        return {k: PHASE_GLYPH[sg.phase] + " " + PHASE_WORD[sg.phase] + " " + pct(sg) + "%",
          v: phaseExactFmt(m, sg.value), c: PHASE_COLOR[sg.phase]};
      });
      tipRows.push({k: "Total", v: phaseExactFmt(m, col.total), c: "var(--ink)"});
      var hit = s("rect", {x: x - 1, y: T, width: bw + 2, height: ph, fill: "transparent",
        "data-t": String(col.day), "data-v": String(col.total)});
      hit.addEventListener("mousemove", function (ev) {
        showTip(ev, label + " \u00B7 " + phaseDay(col.day), tipRows);
      });
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    /* Visible totals above the last few non-empty bars. */
    var labelled = 0;
    for (var li = stacked.stacks.length - 1; li >= 0 && labelled < 3; li--) {
      var colL = stacked.stacks[li];
      if (!(colL.total > 0)) continue;
      var topY = Y(colL.total), cx = X(li) + bw / 2;
      var t = s("text", {x: cx, y: Math.max(topY - 5, 10), "text-anchor": "middle", class: "vallab"});
      t.textContent = phaseFmt(m, colL.total);
      svg.appendChild(t);
      labelled++;
    }
    var maxTicks = Math.max(2, Math.floor(pw / 70));
    var step = Math.max(1, Math.ceil(n / maxTicks));
    var xt = [];
    for (var xi = 0; xi < n; xi += step) {
      var isLast = xi + step >= n;
      xt.push({x: X(xi) + bw / 2 + (xi === 0 ? 4 : isLast ? -4 : 0),
        label: phaseDay(stacked.stacks[xi].day), anchor: xi === 0 ? "start" : isLast ? "end" : "middle"});
    }
    var axg = axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, yTicks: 4, yFmt: yFmt,
      xTicks: xt, yTitle: m === "cost" ? "USD per day" : "tokens per day", xTitle: "UTC day"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
    return true;
  }

  /* Plain-words takeaway for one workstream: which kind of work took
     most of its cost or tokens. Names first, never bare ids. */
  function phaseWsTakeaway(label, item, metric) {
    var cells = PHASES.map(function (ph) { return {phase: ph, value: phaseValue((item.by_phase || {})[ph], metric)}; });
    var lead = {phase: "unclassified", value: 0};
    cells.forEach(function (c) { if (c.value > lead.value) lead = c; });
    if (!(lead.value > 0)) return null;
    var unit = metric === "cost" ? "cost" : "tokens";
    if (lead.phase === "unclassified")
      return "Most of " + label + "\u2019s " + unit + " could not be split by label \u2014 it reads Unclassified.";
    return "Most of " + label + "\u2019s " + unit + " went to " + PHASE_WORD[lead.phase].toLowerCase() + ".";
  }

  /* Metric toggle shared by the workstream card (Cost | Tokens). */
  function phaseWsMetricTabs(host) {
    var met = el("div", {class: "seg-ctl phase-tabs", role: "group", "aria-label": "Usage measure"});
    [["tokens", "Tokens"], ["cost", "Cost"]].forEach(function (t) {
      met.appendChild(el("button", {type: "button", "aria-pressed": String(phaseMetric === t[0]), onclick: function () { phaseMetric = t[0]; phaseRedraw(); }}, [t[1]]));
    });
    host.appendChild(met);
    var view = el("div", {class: "seg-ctl phase-tabs", role: "group", "aria-label": "Usage view"});
    [["bars", "Per day"], ["lines", "Cumulative"]].forEach(function (t) {
      view.appendChild(el("button", {type: "button", "aria-pressed": String(phaseWsView === t[0]), onclick: function () { phaseWsView = t[0]; phaseRedraw(); }}, [t[1]]));
    });
    host.appendChild(view);
  }

  /* Last section of the workstream Execution tab: per-day stacked columns
     by kind of work (default) with the cumulative lines behind a small
     toggle. The optional range ({since, until}) lets a shared time
     filter narrow the days without this file depending on its module. */
  function phaseUsageWsCard(range) {
    var row = wsRow();
    var c = el("div", {class: "card phase-usage phase-ws", "data-nav": "Planning versus execution"});
    c.appendChild(el("h2", {text: "Planning versus execution"}));
    c.appendChild(el("p", {class: "sub", text: "Per-day split by kind of work for this workstream."}));
    phaseWsMetricTabs(c);
    if (!row) {
      c.appendChild(el("div", {class: "empty", text: "No workstream selected."}));
      return c;
    }
    var label = wsLabel(row.ws, row.name);
    var data = phasePayloadFor("all");
    if (state.phaseUsageError && !data) {
      c.appendChild(el("div", {class: "empty err", "data-failed-scope": "workstream", text: "request failed \u2014 " + state.phaseUsageError}));
      return c;
    }
    if (!data) {
      ensurePhaseUsageFor("all");
      c.appendChild(el("div", {class: "empty", text: "Loading planning versus execution\u2026"}));
      return c;
    }
    var item = null;
    (data.workstreams || []).forEach(function (w) {
      if (String(w.ws).toUpperCase() === String(row.ws).toUpperCase()) item = w;
    });
    if (!item) {
      c.appendChild(el("div", {class: "empty", text: "No usage recorded for " + label + " yet."}));
      return c;
    }
    var cells = PHASES.map(function (ph) { return {phase: ph, value: phaseValue((item.by_phase || {})[ph], phaseMetric)}; });
    phaseLegend(c, cells, phaseMetric);
    if (phaseWsView === "lines") {
      if (!phaseCumulative(c, item, phaseMetric)) {
        c.appendChild(el("div", {class: "empty", text: "No cumulative " + (phaseMetric === "cost" ? "cost" : "tokens") + " yet for " + label + "."}));
      } else {
        c.appendChild(el("p", {class: "ph-foot", text: "Cumulative " + (phaseMetric === "cost" ? "cost" : "tokens") + " by kind of work \u2014 a flattening line means that kind stopped spending."}));
      }
    } else {
      if (!phaseStackedBars(c, item, phaseMetric, range)) {
        c.appendChild(el("div", {class: "empty", text: "No usage recorded for " + label + " yet."}));
      } else {
        c.appendChild(el("p", {class: "ph-foot", text: "Stacked columns per day \u2014 each segment is one kind of work."}));
      }
    }
    var take = phaseWsTakeaway(label, item, phaseMetric);
    if (take) c.appendChild(el("p", {class: "ph-takeaway", text: take}));
    return c;
  }

  /* Registry spec for the workstream tab, as the last section below
     Finished (and its pager). */
  function phaseUsageWsSpec(passedState, snap) {
    var row = wsRow();
    return ["phases-ws", keyOf([state.ws, row && row.by_phase, state.phaseUsage, state.phaseUsageError, state.seq, phaseMetric, phaseWsView]), function () { return phaseUsageWsCard(); }, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.phaseUsage = phaseUsageCard;
  AdPage.cards.phaseUsageWs = phaseUsageWsCard;
  AdPage.cards.phaseShares = phaseShares;
  AdPage.cards.cumSum = cumSum;
  AdPage.cards.phaseStackDays = phaseStackDays;
  AdPage.cards.phaseSortByTotal = phaseSortByTotal;
  AdPage.cards.phaseWsTakeaway = phaseWsTakeaway;
  global.phaseUsageSpec = phaseUsageSpec;
  global.phaseUsageCard = phaseUsageCard;
  global.phaseUsageAllSpec = phaseUsageAllSpec;
  global.phaseUsageWsSpec = phaseUsageWsSpec;
  global.phaseUsageWsCard = phaseUsageWsCard;
  global.phaseBar = phaseBar;
  global.phaseShares = phaseShares;
  global.cumSum = cumSum;
  global.phaseStackDays = phaseStackDays;
})(window);

