/* Page card: Where it goes, one stacked bar chart right below the tiles.
   Three toggles on a single 24-hour chart: tokens by workstream, cost by
   workstream, tokens by model. Reads: state.wherego (per-minute usage
   payload cache); Date.now for the fixed 24 h window; localStorage for
   the persisted toggle (guarded, per browser).
   Exports (on window.AdPage, also kept as plain globals): wheregoSpec
   (the registry entry point), wheregoCard, wheregoMode, plus the pure
   helpers sharesOf (folding shares, node-tested), wheregoVal and
   wheregoHours (window math, node-tested).
   Needs: el, s, CW, axes, timeTicks, fmtN, fmtUsd, fmtDate, fmtPct,
   niceMax, tokTotal, keyOf, since, api, showTip, hideTip, drawMain,
   secCache, state, ADM, colorOf, DashNav (plain globals). */
(function (global) {
  "use strict";
  var MODES = [
    ["ws-tokens", "Tokens by workstream"],
    ["ws-cost", "Cost by workstream"],
    ["model", "Tokens by model"]
  ];
  var STORE_KEY = "agent-dash-wherego";
  var FOLD_PCT = 0.02;
  var HOUR = 3600;
  var FOCUS = {};

  /* One bucket cell to its chart value: billed tokens, or cost dollars. */
  function wheregoVal(b, metric) {
    b = b || {};
    if (metric === "cost") return Math.max(0, +b.cost_usd || 0);
    return Math.max(0, (+b.input || 0) + (+b.output || 0) + (+b.cache_read || 0) + (+b.cache_write || 0));
  }

  /* Folded shares over raw /api/usage bucket cells: [{key, total, share}]
     sorted by total, shares summing to 1. Keys under 2% of the 24 h total
     fold into one muted Other segment; unassigned is never folded so
     unattributed spend stays visible; a server-folded Other absorbs the
     small keys instead of doubling. Pure: no DOM, no state. */
  function sharesOf(buckets, metric, opts) {
    opts = opts || {};
    var thresh = opts.threshold != null && isFinite(+opts.threshold) ? +opts.threshold : FOLD_PCT;
    var totals = {}, grand = 0;
    (buckets || []).forEach(function (b) {
      if (!b) return;
      var k = b.key == null || b.key === "" ? "unassigned" : String(b.key);
      var v = wheregoVal(b, metric);
      if (!(v > 0)) return;
      totals[k] = (totals[k] || 0) + v;
      grand += v;
    });
    if (!(grand > 0)) return [];
    var out = [], folded = 0, otherAt = -1;
    Object.keys(totals).forEach(function (k) {
      var t = totals[k];
      if (k === "Other") { otherAt = out.length; out.push({key: k, total: t, share: t / grand}); }
      else if (k.toLowerCase() === "unassigned" || t >= thresh * grand) out.push({key: k, total: t, share: t / grand});
      else folded += t;
    });
    if (folded > 0) {
      if (otherAt >= 0) {
        out[otherAt].total += folded;
        out[otherAt].share = out[otherAt].total / grand;
      } else out.push({key: "Other", total: folded, share: folded / grand});
    }
    out.sort(function (a, b) { return b.total - a.total; });
    return out;
  }

  /* The 24 hourly bars for the fixed window: 23 complete hours plus the
     current partial hour, oldest first. Stale data (everything older than
     a day, e.g. fixtures) anchors on the newest data hour instead of the
     wall clock so the shape still draws. Pure: no DOM, no state. */
  function wheregoHours(nowS, maxT) {
    var wall = Math.floor(nowS / HOUR) * HOUR, end = wall + HOUR;
    if (maxT && isFinite(+maxT) && maxT < wall - 24 * HOUR) end = Math.floor(maxT / HOUR) * HOUR + HOUR;
    var hours = [];
    for (var i = 24; i >= 1; i--) hours.push(end - i * HOUR);
    return {hours: hours, t0: end - 24 * HOUR, t1: end, live: end === wall + HOUR};
  }

  function wheregoMode() {
    var m = MODES[0][0];
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      for (var i = 0; i < MODES.length; i++) if (MODES[i][0] === raw) return raw;
    } catch (e) {}
    return m;
  }
  function wheregoGroup(mode) { return mode === "model" ? "model" : "workstream"; }
  function wheregoMetric(mode) { return mode === "ws-cost" ? "cost" : "tokens"; }
  function wheregoStamp() { return Math.floor(Date.now() / 60000); }
  function wheregoFocus(mode) { return FOCUS[mode || wheregoMode()] || null; }

  /* Section-key fragment: mode plus the minute bucket plus whether this
     mode's payload for the bucket is in hand (loading and loaded key
     apart, so the arrival redraws the card once). */
  function wheregoKey() {
    var mode = wheregoMode(), stamp = wheregoStamp(), c = null;
    try { c = state.wherego; } catch (e) {}
    var data = c && c.stamp === stamp && c.modes ? c.modes[mode] : null;
    var failed = c && c.failed === mode + "|" + stamp ? "1" : "0";
    return mode + "|" + stamp + "|" + (data ? data.length : -1) + "|" + failed;
  }

  /* One in-flight usage fetch per mode and minute: concurrent drawMain
     calls share it instead of stacking fetches, and a failed fetch marks
     the mode so the card reads "request failed" instead of zeros. */
  var wheregoInflight = null;
  function wheregoEnsure() {
    var mode = wheregoMode(), stamp = wheregoStamp();
    var c = null;
    try { c = state.wherego; } catch (e) {}
    if (c && c.stamp === stamp && c.modes && c.modes[mode]) return;
    if (wheregoInflight === mode + "|" + stamp) return;
    wheregoInflight = mode + "|" + stamp;
    api("/api/usage", {group: wheregoGroup(mode), bucket: "hour", since: since()}).then(function (j) {
      wheregoInflight = null;
      if (state.view !== "exec" || wheregoMode() !== mode || wheregoStamp() !== stamp) return;
      var w = state.wherego;
      if (!w || w.stamp !== stamp) w = state.wherego = {stamp: stamp, modes: {}};
      w.modes[mode] = (j && j.buckets) || [];
      try { delete secCache.wherego; } catch (e) {}
      drawMain();
    }, function () {
      wheregoInflight = null;
      if (state.view !== "exec" || wheregoMode() !== mode) return;
      var w2 = state.wherego;
      if (!w2 || w2.stamp !== stamp) w2 = state.wherego = {stamp: stamp, modes: {}};
      w2.failed = mode + "|" + stamp;
      try { delete secCache.wherego; } catch (e2) {}
      drawMain();
    });
  }

  /* Compact builders: every number the user reads is compact (fmtN,
     fmtUsd, fmtPct); exact values stay in data-v/data-t attributes. */
  function wheregoFmt(total, metric) { return metric === "cost" ? fmtUsd(total) : fmtN(total); }
  function wheregoName(key, mode, allKeys) {
    if (key === "Other") return "Other";
    if (key === "unassigned" || key === "unattributed") return key;
    if (wheregoGroup(mode) === "model" && typeof ADM !== "undefined") {
      if (String(key).indexOf("@") >= 0) return ADM.seriesName(key);
      return ADM.nameOf(key);
    }
    return key;
  }
  function wheregoFill(key, mode, allKeys) {
    if (key === "Other") return "var(--other)";
    if (key === "unassigned" || key === "unattributed") return "var(--muted)";
    if (wheregoGroup(mode) === "model" && typeof ADM !== "undefined") {
      if (String(key).indexOf("@") >= 0) return ADM.seriesColor(key, allKeys || []);
      return ADM.colorOf(key);
    }
    if (AdPage.wsColor && typeof AdPage.wsColor === "function") {
      try { return AdPage.wsColor(key, (typeof state !== "undefined" && state.wss) || null); } catch (e) {}
    }
    return colorOf(key, "workstream");
  }
  function wheregoLegendTip(name, share, total, metric) {
    return name + " " + fmtPct(share) + " of the 24 h total (" + wheregoFmt(total, metric) + ")";
  }
  function wheregoSegRows(name, hourLabel, value, metric, share) {
    return [
      {k: name, v: wheregoFmt(value, metric)},
      {k: "hour", v: hourLabel},
      {k: "share of that hour", v: fmtPct(share)}
    ];
  }
  function wheregoAria(metric, top) {
    var words = top.map(function (r) { return r.name + " " + fmtPct(r.share); });
    var noun = metric === "cost" ? "cost went" : "tokens went";
    return "Where the " + noun + " in the last 24 hours: " + (words.length ? words.join(", ") : "no usage");
  }

  /* The stacked chart: 24 hourly bars, oldest first, on one x axis with
     one y axis (niceMax ceiling). Other paints at the bottom, unassigned
     above it, the rest by ascending share so the largest crowns the bar;
     the live partial hour draws lighter. */
  function wheregoChart(hours, perHour, shares, metric, mode, t0, t1, live) {
    var W = Math.max(560, CW()), L = 52, R = 10, T = 30, Bm = 44, pw = W - L - R, ph = 290 - T - Bm;
    var X = function (t) { return L + pw * (t - t0) / Math.max(1, t1 - t0); };
    var max = 0;
    hours.forEach(function (t) {
      var s = 0;
      shares.forEach(function (r) { s += (perHour[t] && perHour[t][r.key]) || 0; });
      if (s > max) max = s;
    });
    var nice = niceMax(max);
    var Y = function (v) { return T + ph - ph * v / nice; };
    var order = shares.slice().sort(function (a, b) { return a.total - b.total; });
    var mutedFirst = order.filter(function (r) { return r.key === "Other"; })
      .concat(order.filter(function (r) { return r.key !== "Other" && (r.key === "unassigned" || r.key === "unattributed"); }))
      .concat(order.filter(function (r) { return r.key !== "Other" && r.key !== "unassigned" && r.key !== "unattributed"; }));
    var focus = wheregoFocus(mode);
    var yFmt = metric === "cost" ? fmtUsd : fmtN;
    var yTitle = metric === "cost" ? "USD" : "tokens";
    var takeaway = wheregoAria(metric, shares.slice(0, 4).map(function (r) {
      return {name: wheregoName(r.key, mode), share: r.share};
    }));
    var svg = s("svg", {viewBox: "0 0 " + W + " 290", width: W, height: 290, style: "display:block;max-width:100%", role: "img", "data-strip": "wherego", "data-x-from": t0, "data-x-to": t1, "aria-label": takeaway});
    hours.forEach(function (t) {
      var hourTot = 0;
      shares.forEach(function (r) { hourTot += (perHour[t] && perHour[t][r.key]) || 0; });
      var bx1 = X(t) + 2, bx2 = X(t + HOUR) - 2;
      var bw = Math.max(2, bx2 - bx1), x = bx1, y0 = T + ph;
      var partial = live && t === hours[hours.length - 1];
      var tops = mutedFirst.filter(function (r) { return (perHour[t] && perHour[t][r.key]) || 0; });
      var topKey = tops.length ? tops[tops.length - 1].key : null;
      mutedFirst.forEach(function (r, ri) {
        var v = (perHour[t] && perHour[t][r.key]) || 0;
        if (!v) return;
        var h = ph * v / nice, top = y0 - h;
        var gap = ri !== 0 ? 2 : 0, y = top + gap, hh = h - gap;
        if (hh < 0) { hh = 0; y = y0; }
        var ry = r.key === topKey ? Math.min(4, bw / 2, Math.max(hh / 2, 1)) : 1.5;
        var rect = s("rect", {x: x, y: y, width: bw, height: Math.max(hh, 0.4), rx: ry, fill: wheregoFill(r.key, mode, shares.map(function (q) { return q.key; })), class: "wg-seg", "data-key": r.key, "data-v": v, "data-t": t});
        if (partial) rect.setAttribute("opacity", "0.55");
        if (focus && r.key !== focus) rect.setAttribute("data-dim", "1");
        else rect.removeAttribute("data-dim");
        svg.appendChild(rect);
        y0 = top;
      });
      var hit = s("rect", {x: x - 1, y: T, width: bw + 2, height: ph, fill: "transparent", "data-t": t, "data-v": hourTot});
      var hourLabel = fmtDate(t);
      hit.addEventListener("mousemove", function (ev) {
        showTip(ev, hourLabel, shares.filter(function (r) {
          return (perHour[t] && perHour[t][r.key]) || 0;
        }).map(function (r) {
          var v = perHour[t][r.key] || 0;
          return {k: wheregoName(r.key, mode), v: wheregoFmt(v, metric) + " · " + fmtPct(hourTot ? v / hourTot : 0), c: wheregoFill(r.key, mode)};
        }));
      });
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    var ticks = timeTicks(t0, t1, X, Math.max(2, Math.floor(pw / 150)));
    axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, yTicks: 4, yFmt: yFmt, xTicks: ticks, yTitle: yTitle, xTitle: "time"});
    return svg;
  }

  function wheregoCard() {
    var mode = wheregoMode(), metric = wheregoMetric(mode);
    var card = el("div", {class: "card", id: "exec-wherego", "data-nav": "Where it goes"});
    var head = el("div", {class: "wg-head"});
    head.appendChild(el("h2", {text: "Where it goes"}));
    var seg = el("div", {class: "seg-ctl", role: "group", "aria-label": "Where-it-goes grouping"});
    MODES.forEach(function (m) {
      var b = el("button", {type: "button", "aria-pressed": mode === m[0] ? "true" : "false", text: m[1], title: m[1] + " on one 24-hour stacked bar"});
      b.addEventListener("click", function () {
        if (wheregoMode() === m[0]) return;
        try { window.localStorage.setItem(STORE_KEY, m[0]); } catch (e) {}
        try { delete secCache.wherego; } catch (e2) {}
        drawMain();
      });
      seg.appendChild(b);
    });
    head.appendChild(seg);
    card.appendChild(head);
    var sub = metric === "cost"
      ? "Estimated cost per hour, stacked by workstream · last 24 h"
      : (wheregoGroup(mode) === "model"
        ? "Billed tokens per hour, stacked by model · last 24 h"
        : "Billed tokens per hour, stacked by workstream · last 24 h");
    card.appendChild(el("p", {class: "sub", text: sub}));
    wheregoEnsure();
    var c = null;
    try { c = state.wherego; } catch (e) {}
    var stamp = wheregoStamp();
    var failed = c && c.failed === mode + "|" + stamp;
    var data = c && c.stamp === stamp && c.modes ? c.modes[mode] : null;
    if (failed || (state.snapError && !data)) {
      card.appendChild(el("div", {class: "empty", "data-failed-scope": "all", text: "request failed"}));
      return card;
    }
    if (!data) {
      card.setAttribute("aria-busy", "true");
      card.appendChild(el("div", {class: "skel"}));
      card.appendChild(el("div", {class: "skel"}));
      card.appendChild(el("div", {class: "skel"}));
      return card;
    }
    var nowS = Date.now() / 1000, maxT = 0;
    data.forEach(function (b) { if (b && isFinite(+b.t) && +b.t > maxT) maxT = +b.t; });
    var win = wheregoHours(nowS, maxT);
    var perHour = {}, inWin = [];
    data.forEach(function (b) {
      if (!b || !isFinite(+b.t)) return;
      if (!(+b.t + HOUR > win.t0 && +b.t < win.t1)) return;
      inWin.push(b);
      var v = wheregoVal(b, metric);
      if (!(v > 0)) return;
      var k = b.key == null || b.key === "" ? "unassigned" : String(b.key);
      perHour[+b.t] = perHour[+b.t] || {};
      perHour[+b.t][k] = (perHour[+b.t][k] || 0) + v;
    });
    var shares = sharesOf(inWin, metric);
    if (!shares.length) {
      card.appendChild(el("div", {class: "empty", text: "No usage in this window."}));
      return card;
    }
    var grand = 0;
    shares.forEach(function (r) { grand += r.total; });
    var lg = el("div", {class: "legend wg-legend"});
    var ltotal = el("span", {class: "ltotal", "data-legend-total": grand, "data-chart-sum": grand, title: "Share of the fixed 24-hour total"});
    ltotal.appendChild(el("b", {text: "Total " + wheregoFmt(grand, metric)}));
    lg.appendChild(ltotal);
    var focus = wheregoFocus(mode);
    shares.forEach(function (r) {
      var name = wheregoName(r.key, mode, shares.map(function (q) { return q.key; }));
      var b = el("button", {type: "button", class: "lentry", "aria-pressed": focus === r.key ? "true" : "false", "data-key": r.key, "data-v": r.total, "data-share": Math.round(r.share * 10000) / 100, title: wheregoLegendTip(name, r.share, r.total, metric)});
      var sw = el("i", {class: "sw"});
      sw.style.background = wheregoFill(r.key, mode, shares.map(function (q) { return q.key; }));
      b.appendChild(sw);
      b.appendChild(el("span", {class: "lname", text: name}));
      var lv = el("span", {class: "lv", text: fmtPct(r.share) + " · " + wheregoFmt(r.total, metric)});
      b.appendChild(lv);
      b.addEventListener("click", function () {
        FOCUS[mode] = FOCUS[mode] === r.key ? null : r.key;
        try { delete secCache.wherego; } catch (e) {}
        drawMain();
      });
      lg.appendChild(b);
    });
    card.appendChild(lg);
    var svg = wheregoChart(win.hours, perHour, shares, metric, mode, win.t0, win.t1, win.live);
    card.appendChild(svg);
    Array.prototype.forEach.call(svg.querySelectorAll("rect.wg-seg"), function (rect) {
      var k = rect.getAttribute("data-key");
      var t = +(rect.getAttribute("data-t") || 0);
      var v = +(rect.getAttribute("data-v") || 0);
      var hourTot = 0;
      shares.forEach(function (r) { hourTot += (perHour[t] && perHour[t][r.key]) || 0; });
      var name = wheregoName(k, mode);
      rect.addEventListener("mousemove", function (ev) {
        showTip(ev, name + " · " + fmtDate(t), wheregoSegRows(name, fmtDate(t), v, metric, hourTot ? v / hourTot : 0));
      });
      rect.addEventListener("mouseleave", hideTip);
      rect.addEventListener("focus", function (ev) {
        showTip(ev, name + " · " + fmtDate(t), wheregoSegRows(name, fmtDate(t), v, metric, hourTot ? v / hourTot : 0));
      });
      rect.addEventListener("blur", hideTip);
      if (wheregoGroup(mode) !== "model" && k !== "Other" && k !== "unassigned" && k !== "unattributed") {
        rect.setAttribute("tabindex", "0");
        var open = function () { if (window.DashNav && window.DashNav.openExec) window.DashNav.openExec(k); };
        rect.addEventListener("click", open);
        rect.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
        });
      }
    });
    return card;
  }

  function wheregoSpec(stateArg, snap) {
    return ["wherego", keyOf([wheregoMode(), wheregoKey()]), function () { return wheregoCard(); }, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.sharesOf = sharesOf;
  AdPage.wheregoVal = wheregoVal;
  AdPage.wheregoHours = wheregoHours;
  AdPage.wheregoMode = wheregoMode;
  AdPage.wheregoKey = wheregoKey;
  AdPage.wheregoCard = wheregoCard;
  AdPage.wheregoSpec = wheregoSpec;
  global.sharesOf = sharesOf;
  global.wheregoVal = wheregoVal;
  global.wheregoHours = wheregoHours;
  global.wheregoMode = wheregoMode;
  global.wheregoKey = wheregoKey;
  global.wheregoCard = wheregoCard;
  global.wheregoSpec = wheregoSpec;
})(window);
