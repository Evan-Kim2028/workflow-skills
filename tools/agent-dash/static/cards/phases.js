/* Page card: Planning versus execution (?view=project, slot exec-phases;
   compact variant on the workstream execution tab).
   The project card paints GET /api/project/phases (../fixtures/phases.json
   under ?fixtures): stacked horizontal bars per workstream with
   kind-of-work colours, a tokens/cost toggle, scope tabs, totals, and a
   plain-words takeaway. Clicking a workstream opens its tab. The
   workstream card paints the same payload for the open tab: a lifetime
   split bar plus cumulative cost by kind of work over time, so the
   planning spend reads as a line that flattens when planning ends.
   Unclassified is its own grey bucket, never merged.
   Reads: state.phaseUsage (per scope), state.phaseUsageError,
   state.phaseUsageAt, state.view, state.ws, state.wss/wssAll.
   Exports (on window.AdPage.cards, also kept as plain globals):
   phaseUsageSpec, phaseUsageCard, phaseUsageWsSpec, phaseUsageWsCard,
   phaseBar, phaseShares, cumSum.
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
    var items = (data.workstreams || []).filter(function (w) { return w.ws !== "Other"; });
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

  /* Cumulative cost by kind of work over time for one workstream: one
     line per kind with spend, planning first so the planning line
     flattening reads as planning ending. */
  function phaseCumulative(host, item) {
    var days = item.days || [];
    if (!days.length) return false;
    var order = PHASES.filter(function (ph) {
      var cum = ((item.series || {})[ph] || {}).cumulative_cost || [];
      return cum.length && cum[cum.length - 1] > 0;
    });
    if (!order.length) return false;
    var peak = 0;
    order.forEach(function (ph) {
      ((item.series || {})[ph].cumulative_cost || []).forEach(function (v) { if (v > peak) peak = v; });
    });
    var nice = niceMax(peak);
    var W = CW(), H = 240, L = 64, R = 14, T = 24, Bm = 44;
    var pw = W - L - R, ph = H - T - Bm;
    var n = Math.max(1, days.length);
    var X = function (t) { return L + ((t - days[0]) / 86400) * (pw / n) + (pw / n) / 2; };
    var Y = function (v) { return T + ph - v / nice * ph; };
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img",
      "aria-label": "Cumulative cost by kind of work over time for " + wsLabel(item.ws, item.name)});
    order.forEach(function (phase) {
      var cum = item.series[phase].cumulative_cost || [];
      var line = cum.map(function (v, i) { return X(days[i]) + "," + Y(v); }).join(" ");
      svg.appendChild(s("polyline", {points: line, fill: "none", stroke: PHASE_COLOR[phase], "stroke-width": "2.2", "data-key": phase, "data-v": String(cum.length ? cum[cum.length - 1] : 0)}));
      var last = cum.length ? cum[cum.length - 1] : 0;
      var dot = s("circle", {cx: X(days[days.length - 1]), cy: Y(last), r: "3.5", fill: PHASE_COLOR[phase], "data-key": phase, "data-v": String(last)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, PHASE_GLYPH[phase] + " " + PHASE_WORD[phase], [{k: "Cumulative", v: phaseExactUsd(last), c: PHASE_COLOR[phase]}]);
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
    var axg = axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, yTicks: 4, yFmt: fmtUsd, xTicks: xt, yTitle: "USD cumulative", xTitle: "UTC day"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
    return true;
  }

  /* Compact variant for the workstream tab: the lifetime split bar from
     the tab row plus the cumulative cost chart from the shared payload. */
  function phaseUsageWsCard() {
    var row = wsRow();
    var c = el("div", {class: "card phase-usage phase-ws", "data-nav": "Planning versus execution"});
    c.appendChild(el("h2", {text: "Planning versus execution"}));
    if (!row) {
      c.appendChild(el("div", {class: "empty", text: "No workstream selected."}));
      return c;
    }
    var label = wsLabel(row.ws, row.name);
    var bar = row.by_phase ? phaseBar(row.by_phase) : null;
    if (bar) {
      var wrap = el("div", {class: "ph-wsbar"});
      wrap.appendChild(bar);
      wrap.appendChild(el("span", {class: "ph-wsnote", text: "Lifetime tokens: planning in blue, execution in green."}));
      c.appendChild(wrap);
    }
    var data = phasePayloadFor("all");
    if (state.phaseUsageError && !data) {
      c.appendChild(el("div", {class: "empty err", text: "request failed \u2014 " + state.phaseUsageError}));
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
    var cells = PHASES.map(function (ph) { return {phase: ph, value: phaseValue((item.by_phase || {})[ph], "cost")}; });
    phaseLegend(c, cells, "cost");
    if (!phaseCumulative(c, item)) {
      c.appendChild(el("div", {class: "empty", text: "No cumulative cost yet for " + label + "."}));
    } else {
      c.appendChild(el("p", {class: "ph-foot", text: "Cumulative cost by kind of work \u2014 a flattening line means that kind of work stopped spending."}));
    }
    return c;
  }

  /* Registry spec for the workstream tab, near the top. */
  function phaseUsageWsSpec(passedState, snap) {
    var row = wsRow();
    return ["phases-ws", keyOf([state.ws, row && row.by_phase, state.phaseUsage, state.phaseUsageError, state.seq]), function () { return phaseUsageWsCard(); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.phaseUsage = phaseUsageCard;
  AdPage.cards.phaseUsageWs = phaseUsageWsCard;
  AdPage.cards.phaseShares = phaseShares;
  AdPage.cards.cumSum = cumSum;
  global.phaseUsageSpec = phaseUsageSpec;
  global.phaseUsageCard = phaseUsageCard;
  global.phaseUsageWsSpec = phaseUsageWsSpec;
  global.phaseUsageWsCard = phaseUsageWsCard;
  global.phaseBar = phaseBar;
  global.phaseShares = phaseShares;
  global.cumSum = cumSum;
})(window);

