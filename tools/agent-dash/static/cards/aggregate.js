/* Page card: Aggregated progress + accumulated activity (overview).
   Two panels side by side (stacked on phones) on one shared linear time
   axis from the earliest recorded history to now, same width, axes plus
   timeTicks. Left: program headline from /api/ws/progress?ws=all with the
   segmented done/pr/on-prod/working bar (same palette and order as the
   Where we are bar in ws-cards.js; deferred as a muted count beside it),
   under it a burn-up of cumulative done tasks stacked by workstream from
   /api/project/burnup (tasks.by_workstream[ws].cumulative over days) with
   the progress points overlaid as a thin line. Right: cumulative tokens
   (toggle Tokens|Cost) stacked by workstream from
   /api/usage?group=workstream&bucket=hour (full history, Other folded to
   one muted band at the bottom). Undated done tasks render as a flat
   muted baseline band labelled once in the legend, never a banner.
   Hover lifts a band with a tooltip; click opens that workstream's exec
   tab. Both payloads cached per minute with the stamp-in-key pattern.
   Why not reuse burnup.js drawing: its frame assumes UTC-day slots with
   its own tabs, gap band, UTC-day ticks and first-seen colorOf mapping;
   this card needs a shared linear axis across daily and hourly sources,
   the stable-hash AdPage.wsColor, and exec navigation, so it draws the
   smaller stacked areas below with the shared axes()/timeTicks() pair.
   No Agent-hours toggle: neither /api/runs nor the snapshot exposes an
   aggregated agent-hours bucket, and the brief forbids a new endpoint.
   Reads: state.wss, state.aggProg, state.aggBurn, state.aggUsage.
   Exports (on window.AdPage, also kept as plain globals): aggregateSpec,
   aggregateCard, aggFoldBuckets, aggCumulative, aggBandOrder, aggXDomain,
   aggUndatedTotal, aggModeIs.
   Needs: el, s, CW, axes, timeTicks, fmtN, fmtUsd, showTip, hideTip, api,
   keyOf, drawMain, colorOf (fallback), DashNav via window.DashNav. */
(function (global) {
  "use strict";
  var aggMode = "tokens";
  var aggProgInflight = null, aggBurnInflight = null, aggUsageInflight = null;

  function wsCol(ws, st) {
    try {
      if (typeof AdPage !== "undefined" && typeof AdPage.wsColor === "function") return AdPage.wsColor(ws, st || (typeof state !== "undefined" ? state : null));
    } catch (e) {}
    try { if (typeof colorOf === "function") return colorOf(ws, "workstream"); } catch (e2) {}
    return "var(--other)";
  }

  /* Pure: fold hourly usage buckets into per-time per-workstream values.
     mode "tokens" sums input+output+cache_read+cache_write, "cost" reads
     cost_usd (null as 0). Unassigned/null/Other fold into one "Other".
     Returns {times, byWs, totals}. No DOM, no state. */
  function aggFoldBuckets(buckets, mode) {
    var per = {}, keys = {};
    (buckets || []).forEach(function (b) {
      if (!b || typeof b.t === "undefined") return;
      var t = +b.t;
      if (!isFinite(t)) return;
      var k = b.key;
      if (!k || k === "null" || String(k).toLowerCase() === "unassigned" || String(k).toLowerCase() === "other") k = "Other";
      else k = String(k);
      var v = 0;
      if (mode === "cost") v = +b.cost_usd || 0;
      else v = (+b.input || 0) + (+b.output || 0) + (+b.cache_read || 0) + (+b.cache_write || 0);
      if (!isFinite(v) || v < 0) v = 0;
      per[t] = per[t] || {};
      per[t][k] = (per[t][k] || 0) + v;
      keys[k] = 1;
    });
    var times = Object.keys(per).map(Number).sort(function (a, z) { return a - z; });
    var byWs = {}, totals = {};
    Object.keys(keys).forEach(function (k) { byWs[k] = []; totals[k] = 0; });
    times.forEach(function (t) {
      Object.keys(keys).forEach(function (k) {
        var v = (per[t] && per[t][k]) || 0;
        byWs[k].push(v);
        totals[k] += v;
      });
    });
    return {times: times, byWs: byWs, totals: totals};
  }

  /* Pure: running cumulative per workstream over aligned per-time values.
     byWs maps ws to per-time values; returns {cumByWs, totals}. */
  function aggCumulative(byWs, n) {
    var cumByWs = {}, totals = {};
    Object.keys(byWs || {}).forEach(function (k) {
      var arr = byWs[k] || [], out = [], run = 0;
      var len = (n != null) ? n : arr.length;
      for (var i = 0; i < len; i++) { run += (+arr[i] || 0); out.push(run); }
      cumByWs[k] = out;
      totals[k] = run;
    });
    return {cumByWs: cumByWs, totals: totals};
  }

  /* Pure: band order, largest at bottom. "Other" always leads (bottommost
     muted band) regardless of size; the rest sort largest first. */
  function aggBandOrder(totals) {
    var ks = Object.keys(totals || {});
    var hasOther = ks.indexOf("Other") >= 0;
    var rest = ks.filter(function (k) { return k !== "Other"; });
    rest.sort(function (a, z) { return (totals[z] || 0) - (totals[a] || 0); });
    if (hasOther) return ["Other"].concat(rest);
    return rest;
  }

  /* Pure: shared x-domain from candidate starts to now. */
  function aggXDomain(cands, now) {
    var t1 = +now;
    if (!isFinite(t1)) t1 = Date.now() / 1000;
    var t0 = 1 / 0;
    (cands || []).forEach(function (c) {
      var v = +c;
      if (isFinite(v) && v < t0) t0 = v;
    });
    if (!isFinite(t0)) t0 = t1 - 86400;
    if (!(t0 < t1)) t0 = t1 - 3600;
    return {t0: t0, t1: t1};
  }

  /* Pure: total undated done tasks across burnup by_workstream. */
  function aggUndatedTotal(byWs) {
    var total = 0;
    Object.keys(byWs || {}).forEach(function (k) {
      var r = byWs[k];
      if (r && isFinite(+r.undated)) total += (+r.undated);
    });
    return total;
  }

  function aggModeIs() { return aggMode; }

  /* Stamps behind the section key: plan sums for progress, the ws id set
     for burnup/usage, each plus a loaded flag so arrivals redraw once.
     All three payloads refetch at most once per minute (the key carries
     the minute bucket; the conditional-GET layer 304s when unchanged). */
  function aggProgStamp() {
    try { if (typeof actProgStamp === "function") return actProgStamp("all"); } catch (e) {}
    try {
      var rows = (typeof shown === "function") ? shown(state.wss || []) : (state.wss || []);
      return rows.map(function (w) {
        var pr = null;
        try { pr = (typeof progressFor === "function") ? progressFor(w.ws) : null; } catch (e2) {}
        return w.ws + ":" + (pr ? pr.done + "/" + pr.total : "-");
      }).join(",");
    } catch (e3) { return "all"; }
  }
  function aggWsStamp() {
    try { return ((state.wss || []).map(function (w) { return w.ws; }).sort().join(",")); }
    catch (e) { return ""; }
  }
  function aggProgKey() {
    var c = (typeof state !== "undefined") && state.aggProg;
    var ok = (c && c.stamp === aggProgStamp() && c.data) ? "1" : "0";
    return "all|" + aggProgStamp() + "|" + ok;
  }
  function aggBurnKey() {
    var c = (typeof state !== "undefined") && state.aggBurn;
    var ok = (c && c.stamp === aggWsStamp() && c.data) ? "1" : "0";
    return aggWsStamp() + "|" + ok;
  }
  function aggUsageKey() {
    var c = (typeof state !== "undefined") && state.aggUsage;
    var ok = (c && c.stamp === aggWsStamp() && c.data) ? "1" : "0";
    return aggWsStamp() + "|" + ok;
  }
  function aggEnsureProg(stamp) {
    var c = state.aggProg;
    if (c && c.stamp === stamp) return;
    if (aggProgInflight === stamp) return;
    aggProgInflight = stamp;
    api("/api/ws/progress", {ws: "all"}).then(function (j) {
      aggProgInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggProg = {stamp: stamp, data: (j && j.wsprogress) || null, error: null};
      try { delete secCache.aggregate; } catch (e) {}
      drawMain();
    }, function (e) {
      aggProgInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggProg = {stamp: stamp, data: null, error: String((e && e.message) || e || "fetch failed")};
      try { delete secCache.aggregate; } catch (e2) {}
      drawMain();
    });
  }
  function aggEnsureBurn(stamp) {
    var c = state.aggBurn;
    if (c && c.stamp === stamp) return;
    if (aggBurnInflight === stamp) return;
    aggBurnInflight = stamp;
    api("/api/project/burnup", {}).then(function (j) {
      aggBurnInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggBurn = {stamp: stamp, data: (j && j.burnup) || null, error: null};
      try { delete secCache.aggregate; } catch (e) {}
      drawMain();
    }, function (e) {
      aggBurnInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggBurn = {stamp: stamp, data: null, error: String((e && e.message) || e || "fetch failed")};
      try { delete secCache.aggregate; } catch (e2) {}
      drawMain();
    });
  }
  function aggEnsureUsage(stamp) {
    var c = state.aggUsage;
    if (c && c.stamp === stamp) return;
    if (aggUsageInflight === stamp) return;
    aggUsageInflight = stamp;
    api("/api/usage", {group: "workstream", bucket: "hour"}).then(function (j) {
      aggUsageInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggUsage = {stamp: stamp, data: (j && j.buckets) || [], error: null};
      try { delete secCache.aggregate; } catch (e) {}
      drawMain();
    }, function (e) {
      aggUsageInflight = null;
      if (state.ws !== "all" || state.view !== "exec") return;
      state.aggUsage = {stamp: stamp, data: null, error: String((e && e.message) || e || "fetch failed")};
      try { delete secCache.aggregate; } catch (e2) {}
      drawMain();
    });
  }

  /* One shared geometry for both panels: same left margin and plot width
     so one X maps them both. Panel width halves the main column on
     desktop, full width on phones (CSS stacks the grid there too). */
  function aggGeo() {
    var full = CW(), phone = false;
    try { phone = window.innerWidth <= 700; } catch (e) { phone = full < 700; }
    var panelW = phone ? full : Math.max(320, Math.floor((full - 16) / 2));
    var L = panelW <= 480 ? 46 : 58, R = 12;
    return {W: panelW, L: L, R: R, pw: Math.max(200, panelW - L - R)};
  }
  function aggSvg(W, H, label) {
    return s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": label});
  }
  function aggR1(x) { return Math.round(x * 10) / 10; }
  function aggOpenWs(ws) {
    if (window.DashNav && typeof window.DashNav.openExec === "function") window.DashNav.openExec(ws);
    else if (typeof go === "function") go(ws);
  }
  function aggTipFor(ws, done, total, pct) {
    var rows = [{k: "Done", v: fmtN(done) + (total ? " of " + fmtN(total) + " tasks" : " tasks"), c: wsCol(ws)}];
    if (total) rows.push({k: "Share", v: pct + "%", c: "var(--muted)"});
    return rows;
  }
  function aggWireBand(node, ws, tipTitle, tipRows) {
    node.setAttribute("tabindex", "0");
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", "Open the " + ws + " execution tab");
    node.setAttribute("data-key", ws);
    function show(ev) {
      try {
        node.style.filter = "brightness(1.25)";
        node.style.transform = "translateY(-2px)";
        showTip(ev, tipTitle, tipRows);
      } catch (e) {}
    }
    function hide() {
      try { node.style.filter = ""; node.style.transform = ""; hideTip(); } catch (e) {}
    }
    node.addEventListener("mousemove", show);
    node.addEventListener("mouseenter", show);
    node.addEventListener("mouseleave", hide);
    node.addEventListener("focus", function () {
      try {
        node.style.filter = "brightness(1.25)";
        showTip({clientX: window.innerWidth / 2, clientY: window.innerHeight / 3}, tipTitle, tipRows);
      } catch (e) {}
    });
    node.addEventListener("blur", hide);
    node.addEventListener("click", function (ev) { ev.stopPropagation(); aggOpenWs(ws); });
    node.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); aggOpenWs(ws); }
    });
  }

  /* Segmented program bar: done green, to-merge amber, on-prod magenta,
     working blue (the Where we are order and palette). Deferred never
     enters the bar: it reads as a muted count beside the headline. */
  function aggProgBar(host, counts, total) {
    var bar = el("div", {class: "agg-bar", role: "img"});
    var parts = [["done", "var(--good)"], ["pr", "var(--warning)"], ["prog", "var(--s5)"], ["wip", "var(--s1)"]];
    var filled = 0;
    parts.forEach(function (seg) {
      var v = (counts && +counts[seg[0]]) || 0;
      if (!v || !total) return;
      var sp = el("span");
      sp.style.width = (100 * v / total) + "%";
      sp.style.background = seg[1];
      bar.appendChild(sp);
      filled += v;
    });
    var done = (counts && +counts.done) || 0;
    bar.setAttribute("aria-label", fmtN(done) + " of " + fmtN(total) + " plan tasks done");
    bar.title = "Done " + fmtN(counts.done || 0) + " \u00B7 to merge " + fmtN(counts.pr || 0) +
      " \u00B7 on prod " + fmtN(counts.prog || 0) + " \u00B7 working " + fmtN(counts.wip || 0) +
      " \u00B7 not started " + fmtN(counts.todo || 0) + " of " + fmtN(total) + " tasks";
    host.appendChild(bar);
  }

  function aggLegend(host, order, totalOf, fmt, unit, undatedLabel) {
    var lg = el("div", {class: "legend agg-legend"});
    var grand = 0;
    order.forEach(function (ws) { grand += totalOf(ws); });
    lg.appendChild(el("span", {class: "ltotal", "data-legend-total": String(grand), "data-chart-sum": String(grand), title: "Total " + unit}, [el("b", {text: "Total " + fmt(grand)})]));
    order.forEach(function (ws) {
      var v = totalOf(ws);
      var sp = el("span", {class: "lentry", "data-key": ws, "data-v": String(v), title: ws + " " + fmt(v) + " " + unit});
      var sw = el("i", {class: "sw"});
      sw.style.background = wsCol(ws);
      sp.appendChild(sw);
      sp.appendChild(el("span", {class: "lname", text: ws}));
      sp.appendChild(el("span", {class: "lv", text: fmt(v)}));
      lg.appendChild(sp);
    });
    if (undatedLabel) lg.appendChild(el("span", {class: "lentry agg-undated", title: undatedLabel}, [el("i", {class: "sw", style: "background:var(--other)"}), el("span", {class: "lname", text: undatedLabel})]));
    host.appendChild(lg);
  }

  /* Stacked-area path for one band between lower[] and upper[] over xs. */
  function aggAreaPath(xs, lower, upper) {
    var d = "";
    for (var i = 0; i < xs.length; i++) d += (i ? " L" : "M") + aggR1(xs[i]) + "," + aggR1(upper[i]);
    for (var j = xs.length - 1; j >= 0; j--) d += " L" + aggR1(xs[j]) + "," + aggR1(lower[j]);
    return d + " Z";
  }

  /* Left panel: program headline + bar + stacked burn-up. Shared domain
     dom {t0,t1}, geometry g, burnup b, progress p. Returns the panel node. */
  function aggLeftPanel(dom, g, b, p) {
    var host = el("div", {class: "agg-panel agg-left"});
    host.appendChild(el("h3", {text: "Program progress"}));
    var total = p.total || 0, done = p.done || 0;
    var pct = (p.pct != null) ? p.pct : (total ? Math.round(1000 * done / total) / 10 : 0);
    var head = el("div", {class: "agg-headline"});
    head.appendChild(el("b", {text: fmtN(done) + " / " + fmtN(total) + " tasks \u00B7 " + pct + "%"}));
    if (p.counts && p.counts.deferred) head.appendChild(el("span", {class: "agg-deferred", text: fmtN(p.counts.deferred) + " deferred", title: "Deferred tasks stay out of every count"}));
    host.appendChild(head);
    aggProgBar(host, p.counts || {}, total);
    var byWs = (b.tasks && b.tasks.by_workstream) || {};
    var days = b.days || [];
    var undated = aggUndatedTotal(byWs);
    var order = Object.keys(byWs).filter(function (ws) { return ws !== "Other"; });
    var lastOf = {};
    order.forEach(function (ws) {
      var cum = (byWs[ws] && byWs[ws].cumulative) || [];
      lastOf[ws] = cum.length ? cum[cum.length - 1] : 0;
    });
    order = aggBandOrder(lastOf);
    var undLabel = undated ? (fmtN(undated) + " done before history") : null;
    /* Legend values carry each workstream's undated share too, so the
       legend total agrees with the headline; flat-zero series stay out
       of the legend (the Total covers them), as on the burn-up card. */
    var legOrder = order.filter(function (ws) {
      return (lastOf[ws] || 0) + ((byWs[ws] && +byWs[ws].undated) || 0) > 0;
    });
    aggLegend(host, legOrder, function (ws) { return (lastOf[ws] || 0) + ((byWs[ws] && +byWs[ws].undated) || 0); }, fmtN, "tasks", undLabel);
    var H = 230, T = 10, Bm = 34, ph = H - T - Bm;
    var X = function (t) { return g.L + g.pw * (t - dom.t0) / Math.max(1, dom.t1 - dom.t0); };
    var ticks = timeTicks(dom.t0, dom.t1, X, Math.max(2, Math.floor(g.pw / 120)));
    var proj = ((b.tasks && b.tasks.project) || []).slice();
    var top = 0;
    proj.forEach(function (v) { if (v > top) top = v; });
    var yMax = niceMax(top + undated);
    var Y = function (v) { return T + ph - v / yMax * ph; };
    var xs = days.map(function (t) { return X(t); });
    var takeaway = fmtN(done) + " of " + fmtN(total) + " tasks done (" + pct + "%) across " + order.length + " workstreams";
    var svg = aggSvg(g.W, H, "Program burn-up stacked by workstream: " + takeaway);
    svg.setAttribute("data-panel", "left");
    if (undated > 0) {
      var uh = Y(0) - Y(undated);
      var base = s("rect", {x: g.L, y: Y(undated), width: g.pw, height: Math.max(uh, 1), fill: "var(--other)", opacity: ".35", "data-undated": String(undated)});
      svg.appendChild(base);
    }
    var baseLine = [];
    for (var i = 0; i < days.length; i++) baseLine.push(undated);
    var cumTop = baseLine.slice();
    order.forEach(function (ws) {
      var cum = ((byWs[ws] && byWs[ws].cumulative) || []).slice();
      while (cum.length < days.length) cum.push(cum.length ? cum[cum.length - 1] : 0);
      var upper = [], lower = [];
      for (var k = 0; k < days.length; k++) {
        lower.push(cumTop[k]);
        cumTop[k] += (+cum[k] || 0);
        upper.push(cumTop[k]);
      }
      var lo = lower.map(Y), hi = upper.map(Y);
      var path = s("path", {d: aggAreaPath(xs, lo, hi), fill: wsCol(ws), opacity: ".78", class: "agg-band", "data-key": ws, "data-v": String(lastOf[ws] || 0)});
      (function (w, last) {
        var planT = (byWs[w] && byWs[w].plan_total) || total || 0;
        var wUnd = (byWs[w] && +byWs[w].undated) || 0;
        var pp = planT ? Math.round(1000 * (last + wUnd) / planT) / 10 : 0;
        aggWireBand(path, w, w, aggTipFor(w, last + wUnd, planT, pp));
      })(ws, lastOf[ws] || 0);
      svg.appendChild(path);
    });
    var topLine = cumTop.map(function (v, i) { return aggR1(xs[i]) + "," + aggR1(Y(v)); }).join(" L");
    if (topLine) svg.appendChild(s("polyline", {points: topLine, fill: "none", stroke: "var(--ink)", "stroke-width": "1.6", "data-top": String(cumTop.length ? cumTop[cumTop.length - 1] : 0)}));
    (p.points || []).forEach(function () {});
    if ((p.points || []).length > 1) {
      var lp = (p.points || []).filter(function (pt) { return pt.t >= dom.t0 && pt.t <= dom.t1; }).map(function (pt) {
        return aggR1(X(pt.t)) + "," + aggR1(Y(undated + (+pt.done || 0)));
      }).join(" L");
      if (lp) svg.appendChild(s("polyline", {points: lp, fill: "none", stroke: "var(--ink-2)", "stroke-width": "1.2", opacity: ".9", "data-overlay": "progress"}));
    }
    var axg = axes(svg, {L: g.L, T: T, pw: g.pw, ph: ph, yMax: yMax, xTicks: ticks, yTitle: "tasks", xTitle: "time"});
    svg.insertBefore(axg, svg.firstChild);
    svg.setAttribute("data-ticks", ticks.map(function (tk) { return tk.label; }).join("|"));
    host.appendChild(svg);
    return host;
  }

  /* Right panel: cumulative usage stacked by workstream. Buckets are the
     full-history hourly cells; mode picks the value (tokens or cost).
     Rebuilds in place on toggle so the left panel node never moves. */
  function aggRightPanel(dom, g, ticks, buckets, mode) {
    var host = el("div", {class: "agg-panel agg-right"});
    var head = el("div", {class: "agg-right-head"});
    head.appendChild(el("h3", {text: "Accumulated activity"}));
    var seg = el("div", {class: "seg-ctl agg-toggle", role: "group", "aria-label": "Activity units"});
    [["tokens", "Tokens"], ["cost", "Cost"]].forEach(function (m) {
      var b = el("button", {type: "button", "aria-pressed": String(mode === m[0])}, [m[1]]);
      b.addEventListener("click", function () { aggSetMode(m[0]); });
      seg.appendChild(b);
    });
    head.appendChild(seg);
    host.appendChild(head);
    var body = el("div", {class: "agg-right-body"});
    host.appendChild(body);
    aggPaintRight(body, dom, g, ticks, buckets, mode);
    return host;
  }
  function aggPaintRight(body, dom, g, ticks, buckets, mode) {
    body.textContent = "";
    var folded = aggFoldBuckets(buckets || [], mode);
    var cum = aggCumulative(folded.byWs, folded.times.length);
    var order = aggBandOrder(folded.totals);
    var fmt = mode === "cost" ? fmtUsd : fmtN;
    var unit = mode === "cost" ? "USD" : "tokens";
    var grand = 0;
    order.forEach(function (ws) { grand += folded.totals[ws] || 0; });
    aggLegend(body, order, function (ws) { return folded.totals[ws] || 0; }, fmt, unit, null);
    var H = 230, T = 10, Bm = 34, ph = H - T - Bm;
    var X = function (t) { return g.L + g.pw * (t - dom.t0) / Math.max(1, dom.t1 - dom.t0); };
    var yMax = niceMax(grand);
    var Y = function (v) { return T + ph - v / yMax * ph; };
    var xs = folded.times.map(function (t) { return X(t); });
    var takeaway = "Cumulative " + unit + " across workstreams: " + fmt(grand) + " total";
    var svg = aggSvg(g.W, H, "Accumulated activity stacked by workstream: " + takeaway);
    svg.setAttribute("data-panel", "right");
    svg.setAttribute("data-mode", mode);
    var cumTop = [];
    for (var i = 0; i < folded.times.length; i++) cumTop.push(0);
    order.forEach(function (ws) {
      /* Stack the cumulative series (not the per-hour increments): each
         band's top edge is the stacked accumulated total through that
         hour, so the top edge reads the running total. */
      var series = cum.cumByWs[ws] || [];
      var upper = [], lower = [];
      for (var k = 0; k < folded.times.length; k++) {
        lower.push(cumTop[k]);
        cumTop[k] += (+series[k] || 0);
        upper.push(cumTop[k]);
      }
      var lo = lower.map(Y), hi = upper.map(Y);
      var path = s("path", {d: aggAreaPath(xs, lo, hi), fill: ws === "Other" ? "var(--other)" : wsCol(ws), opacity: ws === "Other" ? ".45" : ".78", class: "agg-band", "data-key": ws, "data-v": String(Math.round(folded.totals[ws] || 0))});
      (function (w) {
        var tot = folded.totals[w] || 0;
        var rows = mode === "cost"
          ? [{k: "Cost", v: fmtUsd(tot), c: w === "Other" ? "var(--other)" : wsCol(w)}]
          : [{k: "Tokens", v: fmtN(tot) + " tokens", c: w === "Other" ? "var(--other)" : wsCol(w)}];
        aggWireBand(path, w, w, rows);
      })(ws);
      svg.appendChild(path);
    });
    var topLine = cumTop.map(function (v, i) { return aggR1(xs[i]) + "," + aggR1(Y(v)); }).join(" L");
    if (topLine) {
      svg.appendChild(s("polyline", {points: topLine, fill: "none", stroke: "var(--ink)", "stroke-width": "1.6", "data-top": String(Math.round(grand))}));
      var lab = s("text", {x: g.L + g.pw, y: Math.max(Y(grand) - 6, 10), "text-anchor": "end", class: "agg-toplab"});
      lab.textContent = fmt(grand);
      svg.appendChild(lab);
    }
    var axg = axes(svg, {L: g.L, T: T, pw: g.pw, ph: ph, yMax: yMax, yFmt: fmt, xTicks: ticks, yTitle: unit, xTitle: "time"});
    svg.insertBefore(axg, svg.firstChild);
    svg.setAttribute("data-ticks", ticks.map(function (tk) { return tk.label; }).join("|"));
    body.appendChild(svg);
  }
  /* Toggle rebuilds only the right panel node: the left svg keeps its
     identity so hover state and the shared axis never flicker. */
  function aggSetMode(mode) {
    if (mode !== "tokens" && mode !== "cost") return;
    if (aggMode === mode) return;
    aggMode = mode;
    try {
      var card = document.querySelector("#exec-aggregate");
      if (!card) { drawMain(); return; }
      var right = card.querySelector(".agg-right");
      var leftSvg = card.querySelector('.agg-left svg[data-panel="left"]');
      if (!right) { drawMain(); return; }
      var g = aggGeo();
      var dom = aggSharedDom();
      if (!dom) { drawMain(); return; }
      var X = function (t) { return g.L + g.pw * (t - dom.t0) / Math.max(1, dom.t1 - dom.t0); };
      var ticks = timeTicks(dom.t0, dom.t1, X, Math.max(2, Math.floor(g.pw / 120)));
      var buckets = (state.aggUsage && state.aggUsage.data) || [];
      var fresh = aggRightPanel(dom, g, ticks, buckets, aggMode);
      right.parentNode.replaceChild(fresh, right);
      var leftNow = card.querySelector('.agg-left svg[data-panel="left"]');
      if (leftSvg && leftNow && leftSvg !== leftNow) {
        try { card.querySelector(".agg-left").replaceChild(leftSvg, leftNow); } catch (e) {}
      }
    } catch (e) { try { drawMain(); } catch (e2) {} }
  }
  function aggSharedDom() {
    var now = Date.now() / 1000;
    var cands = [];
    try {
      var b = state.aggBurn && state.aggBurn.data;
      if (b) {
        if (b.first_recorded) cands.push(+b.first_recorded);
        if (b.days && b.days.length) cands.push(+b.days[0]);
      }
      var p = state.aggProg && state.aggProg.data;
      if (p && p.start) cands.push(+p.start);
      var u = state.aggUsage && state.aggUsage.data;
      if (u && u.length) {
        var m = 1 / 0;
        u.forEach(function (row) { if (row && isFinite(+row.t) && +row.t < m) m = +row.t; });
        if (isFinite(m)) cands.push(m);
      }
    } catch (e) {}
    return aggXDomain(cands, now);
  }

  function aggSkeleton(msg) {
    var c = el("div", {class: "agg-panel"});
    c.appendChild(el("div", {class: "skel"}));
    c.appendChild(el("div", {class: "skel"}));
    c.appendChild(el("div", {class: "skel"}));
    if (msg) c.appendChild(el("div", {class: "empty", text: msg}));
    return c;
  }

  function aggregateCard() {
    var c = el("div", {class: "card agg", id: "exec-aggregate", "data-nav": "Aggregate"});
    c.appendChild(el("h2", {text: "Aggregated progress"}));
    c.appendChild(el("p", {class: "sub", text: "Program totals plus accumulated activity across workstreams on one shared time axis."}));
    var ps = aggProgStamp(), ws = aggWsStamp();
    aggEnsureProg(ps);
    aggEnsureBurn(ws);
    aggEnsureUsage(ws);
    var prog = state.aggProg, burn = state.aggBurn, usage = state.aggUsage;
    var bad = (prog && prog.error) || (burn && burn.error) || (usage && usage.error);
    var missing = !(prog && prog.data) || !(burn && burn.data) || !(usage && usage.data);
    if (bad && missing) {
      c.appendChild(el("div", {class: "empty err", "data-failed-scope": "all", text: "request failed"}));
      return c;
    }
    if (missing) {
      var grid0 = el("div", {class: "agg-grid"});
      grid0.appendChild(aggSkeleton());
      grid0.appendChild(aggSkeleton());
      c.appendChild(grid0);
      return c;
    }
    var g = aggGeo();
    var dom = aggSharedDom();
    var X = function (t) { return g.L + g.pw * (t - dom.t0) / Math.max(1, dom.t1 - dom.t0); };
    var ticks = timeTicks(dom.t0, dom.t1, X, Math.max(2, Math.floor(g.pw / 120)));
    var grid = el("div", {class: "agg-grid"});
    grid.appendChild(aggLeftPanel(dom, g, burn.data, prog.data));
    grid.appendChild(aggRightPanel(dom, g, ticks, usage.data || [], aggMode));
    c.appendChild(grid);
    return c;
  }

  function aggregateSpec(passedState, snap) {
    if ((typeof state !== "undefined" ? state.ws : "all") !== "all") return null;
    var minute = Math.floor(Date.now() / 60000);
    var key = keyOf([aggProgKey(), aggBurnKey(), aggUsageKey(), minute]);
    return ["aggregate", key, function () { return aggregateCard(); }, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.aggregate = aggregateCard;
  AdPage.aggregateSpec = aggregateSpec;
  AdPage.aggFoldBuckets = aggFoldBuckets;
  AdPage.aggCumulative = aggCumulative;
  AdPage.aggBandOrder = aggBandOrder;
  AdPage.aggXDomain = aggXDomain;
  AdPage.aggUndatedTotal = aggUndatedTotal;
  AdPage.aggModeIs = aggModeIs;
  AdPage.aggSetMode = aggSetMode;
  global.aggregateSpec = aggregateSpec;
  global.aggregateCard = aggregateCard;
  global.aggFoldBuckets = aggFoldBuckets;
  global.aggCumulative = aggCumulative;
  global.aggBandOrder = aggBandOrder;
  global.aggXDomain = aggXDomain;
  global.aggUndatedTotal = aggUndatedTotal;
  global.aggModeIs = aggModeIs;
  global.aggSetMode = aggSetMode;
})(window);
