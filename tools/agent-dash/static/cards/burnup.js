/* Page card: Project burn-up (?view=project, slot exec-burnup).
   Three small-multiple panels with tabs: tasks done (cumulative recorded
   moves per UTC day as an area, dashed gray plan-total line, a labelled
   gap band where history starts late), cost burn per UTC day stacked by
   workstream, and token burn per UTC day stacked by workstream. Data is
   GET /api/project/burnup (../fixtures/burnup.json under ?fixtures);
   per-workstream lines toggle from the legend. Recorded moves only:
   undated work stays a labelled gap, never interpolated.
   Reads: state.burnup, state.burnupError, state.burnupAt, state.view.
   Exports (on window.AdPage.cards, also kept as plain globals):
   burnupSpec, burnupCard, burnupTabIs.
   Needs: el, s, CW, axes, colorOf, niceMax, fmtN, fmtUsd,
   showTip, hideTip, keyOf, drawMain, api, FIX (plain globals). */
(function (global) {
  "use strict";
  var burnupTab = "tasks";
  var burnupHidden = {};
  var BURNUP_TTL_MS = 120000;

  function burnupTabIs() { return burnupTab; }

  /* The home slot caches by state.seq, which tab and legend toggles do
     not move: drop the slot section (the plan-sums pattern) so drawMain
     repaints this card. */
  function burnupRedraw() {
    try { delete secCache["exec-burnup"]; } catch (e) {}
    drawMain();
  }

  /* Pure: UTC calendar day for an epoch, built from date parts (never a
     locale string, so screenshots and tests agree). */
  function burnupDay(t) {
    var d = new Date(+t * 1000);
    var m = d.getUTCMonth() + 1, day = d.getUTCDate();
    return d.getUTCFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (day < 10 ? "0" + day : day);
  }

  function burnupFetch() {
    if (state._burnupLoading) return;
    state._burnupLoading = true;
    var settle = function (b, err) {
      state._burnupLoading = false;
      state.burnup = b || null;
      state.burnupError = err || null;
      state.burnupAt = Date.now();
      /* The home slot caches by state.seq, which a burnup arrival does
         not move: repaint through the slot invalidation below instead of
         keeping "Loading". */
      if (state.view === "project") burnupRedraw();
    };
    var req;
    if (FIX) {
      req = fetch("../fixtures/burnup.json").then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function (j) { return j.burnup || j; });
    } else {
      req = api("/api/project/burnup", {}).then(function (j) { return j && j.burnup; });
    }
    req.then(function (b) { settle(b, null); }, function (e) { settle(null, String((e && e.message) || e || "fetch failed")); });
  }

  function burnupVisible(order) {
    return (order || []).filter(function (ws) { return !burnupHidden[ws]; });
  }

  function burnupSum(list) {
    var s = 0;
    (list || []).forEach(function (v) { s += (+v || 0); });
    return s;
  }

  /* Pure hover-text builders: compact numbers only (fmt), with the exact
     value left to data-v/data-t attributes for tests and agents. */
  function burnupLegendTip(ws, v, unit, fmt, off) { return ws + " " + fmt(v) + " " + unit + " — activate to " + (off ? "show" : "hide"); }
  function burnupTasksAria(doneNow, planTotal) { return "Tasks done cumulative per day with plan total: " + fmtN(doneNow) + " of " + fmtN(planTotal) + " recorded"; }
  function burnupDoneRows(v, planTotal) { return [{k: "Recorded done", v: fmtN(v) + " tasks", c: "var(--s1)"}, {k: "Plan total", v: fmtN(planTotal) + " tasks", c: "var(--muted)"}]; }
  function burnupWsRow(last, color) { return [{k: "Recorded done", v: fmtN(last) + " tasks", c: color}]; }

  function burnupTabs(host) {
    var seg = el("div", {class: "seg-ctl burnup-tabs", role: "group", "aria-label": "Burn-up panels"});
    [["tasks", "Tasks"], ["cost", "Cost burn"], ["tokens", "Token burn"]].forEach(function (t) {
      seg.appendChild(el("button", {type: "button", "aria-pressed": String(burnupTab === t[0]), onclick: function () { burnupTab = t[0]; burnupRedraw(); }}, [t[1]]));
    });
    host.appendChild(seg);
  }

  /* Legend with workstream swatches and compact totals; every entry is a
     toggle (aria-pressed) and carries raw values for agents. */
  function burnupLegend(host, keys, totalOf, fmt, unit) {
    var lg = el("div", {class: "legend burnup-legend"});
    var grand = 0;
    keys.forEach(function (ws) { grand += totalOf(ws); });
    lg.appendChild(el("span", {class: "ltotal", "data-legend-total": String(grand), "data-chart-sum": String(grand), title: "Project total for this panel in " + unit}, [el("b", {text: "Total " + fmt(grand)})]));
    /* Flat-zero series are not listed: they draw nothing, and with many
       workstreams they bury the series that moved. The Total covers them. */
    keys.forEach(function (ws) {
      var v = totalOf(ws);
      if (!v) return;
      var off = !!burnupHidden[ws];
      var sp = el("span", {class: "lentry" + (off ? " off" : ""), "data-key": ws, "data-v": String(v), role: "button", tabindex: "0", "aria-pressed": String(!off), title: burnupLegendTip(ws, v, unit, fmt, off)});
      var flip = function () { if (burnupHidden[ws]) delete burnupHidden[ws]; else burnupHidden[ws] = 1; burnupRedraw(); };
      sp.addEventListener("click", flip);
      sp.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); flip(); } });
      var sw = el("i", {class: "sw"});
      sw.style.background = colorOf(ws, "workstream");
      sp.appendChild(sw);
      sp.appendChild(el("span", {class: "lname", text: ws}));
      sp.appendChild(el("span", {class: "lv", text: fmt(v)}));
      lg.appendChild(sp);
    });
    host.appendChild(lg);
  }

  function burnupFrame(days) {
    var W = CW(), H = 300, L = 60, R = 14, T = 26, Bm = 46;
    var pw = W - L - R, ph = H - T - Bm;
    var n = Math.max(1, days.length);
    var slot = pw / n;
    var X = function (t) { return L + ((t - days[0]) / 86400) * slot; };
    return {W: W, H: H, L: L, R: R, T: T, Bm: Bm, pw: pw, ph: ph, slot: slot, X: X};
  }

  /* Tick counts that keep every y label an integer: niceMax ceilings are
     1/2/5 times a power of ten, so one of 5/4/2/1 always divides evenly. */
  function burnupIntTicks(nice) {
    var prefs = [5, 4, 2, 1];
    for (var i = 0; i < prefs.length; i++) {
      if (Math.abs(nice / prefs[i] - Math.round(nice / prefs[i])) < 1e-9) return prefs[i];
    }
    return 4;
  }

  /* X ticks are UTC days, thinned to fit: the buckets are UTC-midnight
     days, so the shared local-time timeTicks would label the wrong dates
     in most timezones. Built for the shared axes() helper. */
  function burnupDayTicks(f, days) {
    /* One label per ~70 px keeps phone widths readable. */
    var maxTicks = Math.max(2, Math.floor(f.pw / 70));
    var step = Math.max(1, Math.ceil(days.length / maxTicks));
    var out = [];
    for (var i = 0; i < days.length; i += step) {
      out.push({x: f.X(days[i]) + f.slot / 2, label: burnupDay(days[i]), anchor: "middle"});
    }
    return out;
  }

  function burnupSvg(W, H, label) {
    return s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": label});
  }

  /* Panel 1: recorded tasks-done cumulative per day (area), dashed plan
     line, gap band over the pre-history span, togglable per-WS lines. */
  function burnupTasks(host, b) {
    var days = b.days || [];
    var order = burnupVisible(b.workstreams);
    var byWs = (b.tasks && b.tasks.by_workstream) || {};
    var proj = ((b.tasks && b.tasks.project) || []).slice();
    var planTotal = (b.tasks && b.tasks.plan_total) || 0;
    var peak = planTotal;
    proj.forEach(function (v) { if (v > peak) peak = v; });
    var nice = niceMax(peak);
    var f = burnupFrame(days);
    var doneNow = proj.length ? proj[proj.length - 1] : 0;
    var svg = burnupSvg(f.W, f.H, burnupTasksAria(doneNow, planTotal));
    var Y = function (v) { return f.T + f.ph - v / nice * f.ph; };
    /* Gap band: the UTC days before the first recorded move. Undated
       work is named under the chart, never drawn as data. */
    if (b.first_recorded != null) {
      var x1 = f.X(b.first_recorded);
      if (x1 > f.L + 1) {
        var band = s("rect", {x: f.L, y: f.T, width: Math.max(0, x1 - f.L), height: f.ph, fill: "var(--raised)", "data-gap": "pre-history", "data-first": String(b.first_recorded)});
        svg.appendChild(band);
        var blab = s("text", {x: f.L + 8, y: f.T + 16, class: "blab"});
        blab.textContent = "no history before " + burnupDay(b.first_recorded);
        svg.appendChild(blab);
      }
    }
    /* Project area + line, one dot per day with raw values for agents. */
    var line = proj.map(function (v, i) { return (f.X(days[i]) + f.slot / 2) + "," + Y(v); }).join(" ");
    var base = f.T + f.ph;
    var poly = s("polygon", {points: f.L + "," + base + " " + line + " " + (f.L + f.pw) + "," + base, fill: "var(--s1)", opacity: ".16"});
    svg.appendChild(poly);
    svg.appendChild(s("polyline", {points: line, fill: "none", stroke: "var(--s1)", "stroke-width": "2.5"}));
    proj.forEach(function (v, i) {
      var cx = f.X(days[i]) + f.slot / 2;
      var dot = s("circle", {cx: cx, cy: Y(v), r: "3.5", fill: "var(--s1)", "data-t": String(days[i]), "data-v": String(v)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, burnupDay(days[i]), burnupDoneRows(v, planTotal));
      });
      dot.addEventListener("mouseleave", hideTip);
      svg.appendChild(dot);
    });
    /* Per-workstream lines, togglable from the legend. */
    var tagged = [];
    order.forEach(function (ws) {
      var row = byWs[ws];
      if (!row) return;
      var cum = row.cumulative || [];
      var lp = cum.map(function (v, i) { return (f.X(days[i]) + f.slot / 2) + "," + Y(v); }).join(" ");
      svg.appendChild(s("polyline", {points: lp, fill: "none", stroke: colorOf(ws, "workstream"), "stroke-width": "1.8"}));
      var last = cum.length ? cum[cum.length - 1] : 0;
      var ex = f.X(days[days.length - 1]) + f.slot / 2;
      var end = s("circle", {cx: ex, cy: Y(last), r: "3", fill: colorOf(ws, "workstream"), "data-key": ws, "data-v": String(last)});
      end.addEventListener("mousemove", function (ev) {
        showTip(ev, ws, burnupWsRow(last, colorOf(ws, "workstream")));
      });
      end.addEventListener("mouseleave", hideTip);
      svg.appendChild(end);
      /* Flat zero lanes stay in the legend; labelling each would pile
         every name onto one point. */
      if (last) tagged.push({ws: ws, last: last, y: Y(last)});
    });
    /* Stagger end labels so close lanes stay readable. */
    tagged.sort(function (a, z) { return a.y - z.y; });
    tagged.forEach(function (tag, i) {
      if (i) tag.y = Math.max(tag.y, tagged[i - 1].y + 12);
      var lab = s("text", {x: Math.min(f.X(days[days.length - 1]) + f.slot / 2 + 7, f.L + f.pw - 4), y: tag.y + 4, class: "wlab", fill: colorOf(tag.ws, "workstream")});
      lab.textContent = tag.ws + " " + tag.last;
      svg.appendChild(lab);
    });
    /* Dashed plan-total line, gray like the mockup. */
    var py = Y(planTotal);
    svg.appendChild(s("line", {x1: f.L, x2: f.L + f.pw, y1: py, y2: py, stroke: "var(--muted)", "stroke-width": "1.4", "stroke-dasharray": "6 5", opacity: ".9", "data-plan-total": String(planTotal)}));
    var plab = s("text", {x: f.L + f.pw, y: Math.max(py - 6, 10), "text-anchor": "end", class: "wlab"});
    plab.textContent = "plan " + planTotal;
    svg.appendChild(plab);
    var xt = burnupDayTicks(f, days);
    var axg = axes(svg, {L: f.L, T: f.T, pw: f.pw, ph: f.ph, yMax: nice, yTicks: burnupIntTicks(nice), xTicks: xt, yTitle: "tasks", xTitle: "UTC day"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
    /* Honest gap list: one line per workstream with undated done tasks. */
    var gaps = (b.workstreams || []).map(function (ws) { return [ws, byWs[ws]]; }).filter(function (pair) { return pair[1] && pair[1].gap; });
    if (!proj.some(function (v) { return v > 0; }) && !gaps.length) {
      host.appendChild(el("p", {class: "burnup-note", text: "No recorded task moves yet."}));
    }
    gaps.forEach(function (pair) {
      host.appendChild(el("p", {class: "burnup-gap", "data-ws": pair[0], text: pair[0] + ": " + pair[1].gap}));
    });
  }

  /* Panels 2 and 3: stacked per-day bars by workstream (cost or tokens). */
  function burnupStacks(host, b, kind) {
    var days = b.days || [];
    var order = burnupVisible(b.workstreams);
    var table = kind === "cost" ? (b.cost && b.cost.by_workstream) || {} : (b.tokens && b.tokens.by_workstream) || {};
    var fmt = kind === "cost" ? fmtUsd : fmtN;
    var unit = kind === "cost" ? "USD per day" : "tokens per day";
    var per = {}, keys = {};
    order.forEach(function (ws) {
      ((table[ws]) || []).forEach(function (v, i) {
        if (!v) return;
        per[days[i]] = per[days[i]] || {};
        per[days[i]][ws] = (per[days[i]][ws] || 0) + (+v || 0);
        keys[ws] = 1;
      });
    });
    var ks = order.filter(function (ws) { return keys[ws]; });
    var tots = {};
    var max = 0;
    days.forEach(function (t) {
      var sum = 0;
      ks.forEach(function (ws) { sum += (per[t] && per[t][ws]) || 0; });
      tots[t] = sum;
      if (sum > max) max = sum;
    });
    var nice = niceMax(max);
    var f = burnupFrame(days);
    var svg = burnupSvg(f.W, f.H, (kind === "cost" ? "Cost" : "Tokens") + " burned per day stacked by workstream");
    var grandStack = 0; days.forEach(function (t) { grandStack += tots[t] || 0; });
    svg.setAttribute("aria-label", (kind === "cost" ? "Cost" : "Tokens") + " burned per day stacked by workstream: " + fmt(grandStack) + " total, peak " + fmt(max) + " in a day");
    var Y = function (v) { return f.T + f.ph - v / nice * f.ph; };
    var GAP = 2, RAD = 5;
    var bw = Math.max(3, Math.min(28, f.slot - 4));
    var top3 = days.slice().sort(function (a, z) { return tots[z] - tots[a]; }).slice(0, 3);
    var peakT = days.slice().sort(function (a, z) { return tots[z] - tots[a]; })[0];
    days.forEach(function (t) {
      var x = f.X(t) + (f.slot - bw) / 2, y0 = f.T + f.ph;
      var seq = [];
      ks.forEach(function (ws, ki) { if (per[t] && per[t][ws]) seq.push(ki); });
      var topKi = seq.length ? seq[seq.length - 1] : -1;
      ks.forEach(function (ws, ki) {
        var v = (per[t] && per[t][ws]) || 0;
        if (!v) return;
        var h = f.ph * v / nice, top = y0 - h;
        var gap = ki !== seq[0] ? GAP : 0;
        var ry = ki === topKi ? Math.min(RAD, bw / 2, Math.max((h - gap) / 2, 1)) : 1.5;
        svg.appendChild(s("rect", {x: x, y: top + gap, width: bw, height: Math.max(h - gap, 0.4), rx: ry, fill: colorOf(ws, "workstream"), "data-key": ws, "data-t": String(t), "data-v": String(v)}));
        y0 = top;
      });
      var cx = x + bw / 2;
      if (tots[t] > 0 && top3.indexOf(t) >= 0) {
        var lab = s("text", {x: cx, y: Math.max(Y(tots[t]) - 5, 10), "text-anchor": "middle", class: t === peakT ? "peaklab" : "vallab"});
        lab.textContent = (t === peakT ? "peak " : "") + fmt(tots[t]);
        svg.appendChild(lab);
      }
      var hit = s("rect", {x: x - 1, y: f.T, width: bw + 2, height: f.ph, fill: "transparent", "data-t": String(t), "data-v": String(tots[t])});
      hit.addEventListener("mousemove", function (ev) {
        showTip(ev, burnupDay(t), [{k: "Total", v: fmt(tots[t]), c: "var(--ink)"}].concat(ks.filter(function (ws) { return per[t] && per[t][ws]; }).map(function (ws) { return {k: ws, v: fmt(per[t][ws]), c: colorOf(ws, "workstream")}; })));
      });
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    var xt = burnupDayTicks(f, days);
    var axg = axes(svg, {L: f.L, T: f.T, pw: f.pw, ph: f.ph, yMax: nice, yTicks: burnupIntTicks(nice), yFmt: fmt, xTicks: xt, yTitle: unit, xTitle: "UTC day"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
    if (max <= 0) {
      var unpriced = kind === "cost" ? burnupSum((b.cost && b.cost.unpriced_tokens) || []) : 0;
      host.appendChild(el("p", {class: "burnup-note", text: kind === "cost" && unpriced > 0 ? "No priced usage in these UTC days — excluding " + fmtN(unpriced) + " unpriced tokens." : "No " + (kind === "cost" ? "cost" : "tokens") + " recorded in these UTC days."}));
    }
  }

  function burnupCard() {
    /* No id here: the slot host already carries id exec-burnup, and the
       card styles scope under it. */
    var c = el("div", {class: "card burnup", "data-nav": "Burn-up"});
    c.appendChild(el("h2", {text: "Burn-up"}));
    c.appendChild(el("p", {class: "sub", text: "Recorded task moves vs plan total, cost and token burn per UTC day — gaps stay labelled, never filled in."}));
    if (state.burnupError && !state.burnup) {
      c.appendChild(el("div", {class: "empty err", "data-failed-scope": "project", text: "request failed — " + state.burnupError}));
      return c;
    }
    if (!state.burnup) {
      var stale = !state.burnupAt || (Date.now() - state.burnupAt > BURNUP_TTL_MS);
      if (stale) burnupFetch();
      c.appendChild(el("div", {class: "empty", text: "Loading burn-up…"}));
      return c;
    }
    var b = state.burnup;
    var days = b.days || [];
    var bByWs = (b.tasks && b.tasks.by_workstream) || {};
    var bGaps = (b.workstreams || []).filter(function (ws) { return bByWs[ws] && bByWs[ws].gap; });
    var bLined = ((b.tasks && b.tasks.project) || []).some(function (v) { return v > 0; });
    var bSpent = ((b.tokens && b.tokens.project) || []).some(function (v) { return v > 0; }) ||
      ((b.cost && b.cost.project) || []).some(function (v) { return v > 0; });
    if (!days.length || (!bLined && !bGaps.length && !bSpent)) {
      c.appendChild(el("div", {class: "empty", text: "No recorded history or usage yet."}));
      return c;
    }
    burnupTabs(c);
    if (burnupTab === "tasks") {
      var byWs = (b.tasks && b.tasks.by_workstream) || {};
      burnupLegend(c, b.workstreams || [], function (ws) { var r = byWs[ws]; return r ? (r.recorded || 0) + (r.undated || 0) : 0; }, fmtN, "tasks");
      burnupTasks(c, b);
    } else if (burnupTab === "cost") {
      var costT = (b.cost && b.cost.by_workstream) || {};
      burnupLegend(c, b.workstreams || [], function (ws) { return burnupSum(costT[ws]); }, fmtUsd, "USD");
      burnupStacks(c, b, "cost");
    } else {
      var tokT = (b.tokens && b.tokens.by_workstream) || {};
      burnupLegend(c, b.workstreams || [], function (ws) { return burnupSum(tokT[ws]); }, fmtN, "tokens");
      burnupStacks(c, b, "tokens");
    }
    return c;
  }

  /* Registry spec for the Project home slot exec-burnup. */
  function burnupSpec(passedState, snap) {
    return ["burnup", keyOf([state.burnup, state.burnupError, state.seq, burnupTab, burnupHidden]), function () { return burnupCard(); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.burnup = burnupCard;
  AdPage.cards.burnupLegendTip = burnupLegendTip;
  AdPage.cards.burnupTasksAria = burnupTasksAria;
  AdPage.cards.burnupDoneRows = burnupDoneRows;
  AdPage.cards.burnupWsRow = burnupWsRow;
  global.burnupLegendTip = burnupLegendTip;
  global.burnupTasksAria = burnupTasksAria;
  global.burnupDoneRows = burnupDoneRows;
  global.burnupWsRow = burnupWsRow;
  global.burnupSpec = burnupSpec;
  global.burnupCard = burnupCard;
  global.burnupTabIs = burnupTabIs;
})(window);
