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
    return NF.format(+n || 0);
  }

  function fmtUsd(n) {
    return n == null ? "\u2013" : (Math.abs(n) < 1 ? "$" + (+n).toFixed(2) : UF.format(n));
  }

  function fmtDur(seconds) {
    if (seconds == null || !isFinite(+seconds)) return "\u2013";
    seconds = Math.max(0, +seconds);
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + Math.round(seconds % 60) + "s";
    return Math.floor(seconds / 3600) + "h " + Math.round((seconds % 3600) / 60) + "m";
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

  /* Ticks on whole local hours (step 1, 2, 3, 6, 12 or 24 h, the smallest that fits n labels), plus a "now" tick at t1.
     Tick labels are always real dates and times; X maps them to pixels, so a
     warped X (see warpPos) places real-time labels on a compressed axis.
     opts.minGap drops hour ticks closer than that many pixels to the
     previous kept tick (compressed stretches would pile labels up);
     opts.avoid lists pixel positions (e.g. idle-break markers) that ticks
     keep their distance from. The "now" tick at t1 is always kept. */
  function timeTicks(t0, t1, X, n, opts) {
    n = n || 6;
    opts = opts || {};
    var span = Math.max(1, t1 - t0), steps = [1, 2, 3, 6, 12, 24], step = 24;
    for (var i = 0; i < steps.length; i++) { if (span / (steps[i] * 3600) <= n) { step = steps[i]; break; } }
    var d = new Date(t0 * 1000); d.setMinutes(0, 0, 0);
    var t = d.getTime() / 1000, out = [], gap = opts.minGap || 0, avoid = opts.avoid || [];
    function clearOf(x) {
      if (out.length && x - out[out.length - 1].x < gap) return false;
      for (var j = 0; j < avoid.length; j++) { if (Math.abs(x - avoid[j]) < gap) return false; }
      return true;
    }
    while (t < t0) t += 3600;
    while (t <= t1 - 1800) {
      var hr = new Date(t * 1000).getHours();
      if (hr % step === 0) {
        var x = X(t);
        if (clearOf(x)) out.push({x: x, label: fmtHour(t), anchor: "middle"});
      }
      t += 3600;
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

  global.AD = {fmtHour: fmtHour, fmtN: fmtN, fmtUsd: fmtUsd, fmtDur: fmtDur, fmtDate: fmtDate, fmtT: fmtT, axes: axes, timeTicks: timeTicks, warpTime: warpTime, warpPos: warpPos};
})(window);
