/* Shared number formatters and the one axes() helper. Both views call these. */
(function (global) {
  "use strict";
  var NF = new Intl.NumberFormat("en", {notation: "compact", maximumFractionDigits: 1});
  var UF = new Intl.NumberFormat("en", {style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1});
  var NS = "http://www.w3.org/2000/svg";

  function s(tag, attrs) {
    var node = document.createElementNS(NS, tag);
    for (var key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  function fmtDate(t) {
    return new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }

  function fmtN(n) {
    if (n == null || !isFinite(+n)) return "-";
    return NF.format(+n);
  }

  /* Shares as percents: one decimal only below 10 percent ("62%", "4.3%"). */
  function fmtPct(share) {
    if (share == null || !isFinite(+share)) return "–";
    var pct = (+share) * 100;
    if (Math.abs(pct) >= 10) return Math.round(pct) + "%";
    return (Math.round(pct * 10) / 10) + "%";
  }

  /* Byte counts, compact like fmtN ("950 B", "1.2 KB", "3.4 MB"). */
  function fmtBytes(n) {
    if (n == null || !isFinite(+n)) return "–";
    var v = Math.max(0, +n), units = ["B", "KB", "MB", "GB", "TB"], u = 0;
    while (v >= 1000 && u < units.length - 1) { v /= 1000; u++; }
    if (!u) return Math.round(v) + " B";
    return (Math.round(v * 10) / 10) + " " + units[u];
  }

  function fmtUsd(n) {
    return n == null ? "\u2013" : (Math.abs(n) < 1 ? "$" + (+n).toFixed(2) : UF.format(n));
  }

  function fmtDur(seconds) {
    if (seconds == null || !isFinite(+seconds)) return "\u2013";
    seconds = Math.max(0, +seconds);
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + Math.floor(seconds % 60) + "s";
    return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
  }

  function fmtT(t) {
    var d = new Date(t * 1000);
    return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  function axes(svg, o) {
    var g = s("g", {class: "ax"}), x0 = o.L, x1 = o.L + o.pw, yb = o.T + o.ph;
    if (o.yMax != null) {
      var n = o.yTicks || 4;
      for (var i = 0; i <= n; i++) { var v = o.yMax * i / n, y = yb - o.ph * i / n; if (i && o.grid !== false) g.appendChild(s("line", {x1: x0, x2: x1, y1: y, y2: y, class: "gl"})); g.appendChild(s("line", {x1: x0 - 4, x2: x0, y1: y, y2: y, class: "bl"})); var tx = s("text", {x: x0 - 7, y: y + 3.5, "text-anchor": "end"}); tx.textContent = (o.yFmt || fmtN)(v); g.appendChild(tx); }
    }
    g.appendChild(s("line", {x1: x0, x2: x0, y1: o.T, y2: yb, class: "bl"}));
    g.appendChild(s("line", {x1: x0, x2: x1, y1: yb, y2: yb, class: "bl"}));
    (o.xTicks || []).forEach(function (tk) { g.appendChild(s("line", {x1: tk.x, x2: tk.x, y1: yb, y2: yb + 4, class: "bl"})); if (tk.label) { var lt = s("text", {x: tk.x, y: yb + 15, "text-anchor": tk.anchor || "middle"}); lt.textContent = tk.label; g.appendChild(lt); } });
    if (o.yTitle) { var yt = s("text", {x: x0, y: Math.max(10, o.T - 12), "text-anchor": "start", class: "axt"}); yt.textContent = o.yTitle; g.appendChild(yt); }
    if (o.xTitle) { var xt = s("text", {x: x1, y: yb + 28, "text-anchor": "end", class: "axt"}); xt.textContent = o.xTitle; g.appendChild(xt); }
    svg.appendChild(g); return g;
  }

  function fmtHour(t) {
    var d = new Date(t * 1000);
    if (d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString([], {month: "short", day: "numeric"});
    return d.toLocaleTimeString([], {hour: "numeric"});
  }

  /* Label for one grid time at the chosen step: minute steps read HH:MM,
     hour steps read the hour, day steps read the date, and a tick at
     midnight always reads the date (the date line when a range crosses
     midnight). Once the grid covers more than a day plus one step, an
     hour of the day must repeat, so sub-day ticks carry their date and
     stay unique. */
  function fmtTick(t, step, span) {
    var d = new Date(t * 1000);
    if (step >= 86400 || (d.getHours() === 0 && d.getMinutes() === 0))
      return d.toLocaleDateString([], {month: "short", day: "numeric"});
    if (span >= 86400 + step)
      return d.toLocaleDateString([], {month: "short", day: "numeric"}) + ", " + fmtHour(t);
    if (step >= 3600) return fmtHour(t);
    return fmtT(t);
  }

  /* Ticks on whole grid steps from one minute to one week (the smallest
     step that fits n labels: 15 minutes across 2 h, hours across 6 h,
     every few hours across a day, dates past that), plus a "now" tick
     at t1. Small zooms keep minute-level labels instead of collapsing
     to the end tick; grid ticks stop a label half-width short of it so
     one never doubles the end label.
     Tick labels are always real dates and times; X maps them to pixels, so a
     warped X (see warpPos) places real-time labels on a compressed axis.
     opts.minGap drops grid ticks closer than that many pixels to the
     previous kept tick (compressed stretches would pile labels up);
     opts.avoid lists pixel positions (e.g. idle-break markers) that ticks
     keep their distance from. The end tick at t1 is always kept. */
  function timeTicks(t0, t1, X, n, opts) {
    n = n || 6;
    opts = opts || {};
    var span = Math.max(1, t1 - t0);
    var steps = [60, 120, 300, 900, 1800, 3600, 10800, 14400, 21600, 43200, 86400, 172800, 604800];
    var step = 604800;
    for (var i = 0; i < steps.length; i++) { if (span / steps[i] <= n) { step = steps[i]; break; } }
    var d = new Date(t0 * 1000);
    if (step >= 86400) d.setHours(0, 0, 0, 0);
    else if (step >= 3600) d.setMinutes(0, 0, 0);
    else { d.setSeconds(0, 0); d.setMinutes(Math.floor(d.getMinutes() / (step / 60)) * (step / 60)); }
    var t = d.getTime() / 1000, out = [], gap = opts.minGap || 0, avoid = opts.avoid || [];
    var x1 = X(t1);
    function clearOf(x) {
      if (out.length && x - out[out.length - 1].x < gap) return false;
      for (var j = 0; j < avoid.length; j++) { if (Math.abs(x - avoid[j]) < gap) return false; }
      return true;
    }
    /* Grid ticks stop a label half-width short of the end tick (pixels,
       so warped axes thin the same way); the end tick always draws. */
    function clearOfEnd(x) { return x1 - x >= 34; }
    if (step >= 86400) {
      while (t < t0) t += step;
      while (t < t1) {
        var xd = X(t);
        if (clearOfEnd(xd) && clearOf(xd)) out.push({x: xd, label: fmtTick(t, step, span), anchor: "middle"});
        t += step;
      }
    } else if (step >= 3600) {
      var need = step / 3600;
      while (t < t0) t += 3600;
      while (t < t1) {
        if (new Date(t * 1000).getHours() % need === 0) {
          var x = X(t);
          if (clearOfEnd(x) && clearOf(x)) out.push({x: x, label: fmtTick(t, step, span), anchor: "middle"});
        }
        t += 3600;
      }
    } else {
      while (t < t0) t += step;
      while (t < t1) {
        var xm = X(t);
        if (clearOfEnd(xm) && clearOf(xm)) out.push({x: xm, label: fmtTick(t, step, span), anchor: "middle"});
        t += step;
      }
    }
    out.push({x: X(t1), label: Math.abs(t1 - Date.now() / 1000) < 300 ? "now" : fmtT(t1), anchor: "end"});
    return out;
  }

  /* One shared idle-compressing mapping between real time and chart
     position. pts are {w, t} sorted by both (see the replay warp); start
     and end bound the real span. warpTime maps a position fraction to
     real time (the playhead); warpPos maps real time back to a position
     fraction (the timeline axis). They are inverses of each other, so the
     cursor, bars and ticks line up under the same warp. With no points
     both fall back to the linear mapping. */
  function warpTime(pts, frac, start, end) {
    frac = Math.min(1, Math.max(0, +frac || 0));
    if (!pts || !pts.length) return start + frac * (end - start);
    if (frac <= 0) return pts[0].t;
    if (frac >= 1) return pts[pts.length - 1].t;
    var lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (pts[mid].w <= frac) lo = mid; else hi = mid; }
    var a = pts[lo], b = pts[hi], span = Math.max(1e-9, b.w - a.w), k = (frac - a.w) / span;
    return a.t + k * (b.t - a.t);
  }
  function warpPos(pts, t, start, end) {
    if (!pts || !pts.length) return (t - start) / Math.max(1, end - start);
    if (t <= pts[0].t) return 0;
    var last = pts[pts.length - 1];
    if (t >= last.t) return 1;
    var lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (pts[mid].t <= t) lo = mid; else hi = mid; }
    var a = pts[lo], b = pts[hi], span = Math.max(1e-9, b.t - a.t), k = (t - a.t) / span;
    return a.w + k * (b.w - a.w);
  }

  global.AD = {fmtHour: fmtHour, fmtN: fmtN, fmtUsd: fmtUsd, fmtDur: fmtDur, fmtDate: fmtDate, fmtT: fmtT, fmtPct: fmtPct, fmtBytes: fmtBytes, axes: axes, timeTicks: timeTicks, warpTime: warpTime, warpPos: warpPos};
})(window);
