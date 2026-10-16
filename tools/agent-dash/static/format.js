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
    seconds = Math.max(0, +seconds || 0);
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

  function timeTicks(t0, t1, X, n) {
    var out = [];
    n = n || 6;
    for (var i = 0; i <= n; i++) {
      var t = t0 + (t1 - t0) * i / n;
      out.push({x: X(t), label: i === n && Math.abs(t1 - Date.now() / 1000) < 120 ? "now" : fmtT(t), anchor: i === 0 ? "start" : i === n ? "end" : "middle"});
    }
    return out;
  }

  global.AD = {fmtN: fmtN, fmtUsd: fmtUsd, fmtDur: fmtDur, fmtDate: fmtDate, fmtT: fmtT, axes: axes, timeTicks: timeTicks};
})(window);
