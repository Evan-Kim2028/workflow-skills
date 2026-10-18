/* Page card: Progress over time (workstream Execution tab, below the
   planning-versus-execution card).
   Chart A (main): % of non-deferred tasks done over time as a step area,
   with the recorded prog (merged, awaiting deploy) count as a lighter
   band on the same % axis and labelled dots where completion first
   crossed 20/40/60/80/100%. Chart B (small, directly under A): agent
   runs started per hour as bars on the same time axis. Separate charts
   because runs are a different unit (one y axis per chart); x labels draw
   once at the bottom of B while A keeps gridlines only.
   Data is GET /api/ws/progress?ws=<ID> (../fixtures/ws-progress-<ID>.json
   under ?fixtures): the done moments follow the one shared replay rule,
   so this card and the replay agree task by task; undated work stays a
   labelled gap, never interpolated.
   Reads: state.ws, state.wsProgress, state.wsProgressError,
   state.wsProgressAt.
   Exports (on window.AdPage.cards, also kept as plain globals):
   progressSpec, progressCard, progMileRows, progStepRows, progBarRows,
   progLegendTip, progTakeaway.
   Needs: el, s, CW, axes, timeTicks, fmtN, fmtPct, fmtDur, fmtDate,
   showTip, hideTip, keyOf, drawMain, api, FIX (plain globals). */
(function (global) {
  "use strict";
  var PROG_TTL_MS = 120000;

  /* Pure hover-text builders: every number the user sees on hover is
     compact (fmtN/fmtPct/fmtDur/fmtDate), with the exact value left to
     data-v/data-t attributes for tests and agents. */
  function progLegendTip(name, share) { return name + " " + fmtPct(share) + " of tasks"; }
  function progMileRows(done, total, elapsed) {
    return [{k: "Done", v: fmtN(done) + " of " + fmtN(total) + " tasks", c: "var(--s1)"},
      {k: "Elapsed", v: fmtDur(elapsed), c: "var(--muted)"}];
  }
  function progStepRows(done, totalAll, prog) {
    return [{k: "Done", v: fmtN(done) + " of " + fmtN(totalAll) + " tasks", c: "var(--s1)"},
      {k: "Awaiting deploy", v: fmtN(prog) + " tasks", c: "var(--s5)"}];
  }
  function progBarRows(n) { return [{k: "Runs started", v: fmtN(n), c: "var(--s2)"}]; }
  function progAria(ws, done, total, best, bestElapsed) {
    return "Progress over time for " + ws + ": " + fmtN(done) + " of " + fmtN(total) + " tasks done" +
      (best ? ", first reached " + best + " after " + fmtDur(bestElapsed) : ", no milestone reached yet");
  }
  function progTakeaway(done, total, pct, best, bestElapsed, runs) {
    var head = fmtN(done) + " of " + fmtN(total) + " tasks done (" + fmtPct(pct / 100) + ")";
    if (best) return head + " — first reached " + best + " after " + fmtDur(bestElapsed) + "; " + fmtN(runs) + " agent runs started.";
    return head + " — no 20% milestone reached yet; " + fmtN(runs) + " agent runs started.";
  }

  function progFetch(ws) {
    if (state._progLoading) return;
    state._progLoading = true;
    var settle = function (b, err) {
      state._progLoading = false;
      if (state.ws !== ws) return;
      state.wsProgress = b || null;
      state.wsProgressError = err || null;
      state.wsProgressAt = Date.now();
      if (state.view === "exec" && state.ws !== "all") {
        try { delete secCache["progress-ws"]; } catch (e) {}
        drawMain();
      }
    };
    var req;
    if (FIX) {
      req = fetch("../fixtures/ws-progress-" + ws + ".json").then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function (j) { return j.wsprogress || j; });
    } else {
      req = api("/api/ws/progress", {ws: ws}).then(function (j) { return j && j.wsprogress; });
    }
    req.then(function (b) { settle(b, null); }, function (e) { settle(null, String((e && e.message) || e || "fetch failed")); });
  }

  function progFrame(H, Bm) {
    var W = CW(), L = 56, R = 44, T = 26;
    var pw = W - L - R, ph = H - T - Bm;
    return {W: W, H: H, L: L, R: R, T: T, Bm: Bm, pw: pw, ph: ph};
  }

  function progSvg(f, label) {
    return s("svg", {viewBox: "0 0 " + f.W + " " + f.H, width: f.W, height: f.H, style: "display:block;max-width:100%", role: "img", "aria-label": label});
  }

  function progR1(x) { return Math.round(x * 10) / 10; }

  /* Step path through (t, v) points: flat from the axis, then up at each
     event (step-after). One-decimal pixel rounding via progR1. */
  function progStep(f, X, Y, pts, get) {
    var d = "", prev = null;
    for (var i = 0; i < pts.length; i++) {
      var x = progR1(X(pts[i].t)), y = progR1(Y(get(pts[i])));
      if (!i) d = "M" + progR1(f.L) + "," + y + " L" + x + "," + y;
      else d += " L" + x + "," + prev + " L" + x + "," + y;
      prev = y;
    }
    return d;
  }

  /* Legend: Done plus Awaiting deploy (two series, so a legend), each a
     swatch plus glyph plus word, never color alone. */
  function progLegend(host, p) {
    var lg = el("div", {class: "legend prog-legend"});
    var doneNow = p.done || 0, progNow = 0;
    if (p.points && p.points.length) progNow = p.points[p.points.length - 1].prog || 0;
    lg.appendChild(el("span", {class: "ltotal", "data-legend-total": String(doneNow), "data-chart-sum": String(doneNow), title: "Tasks done now in this workstream"}, [el("b", {text: "Done " + fmtN(doneNow)})]));
    [["done", "✓ Done", "var(--s1)", doneNow], ["prog", "⇡ Awaiting deploy", "var(--s5)", progNow]].forEach(function (row) {
      lg.appendChild(el("span", {class: "lentry", "data-key": row[0], "data-v": String(row[3]), title: progLegendTip(row[1], row[3] / Math.max(1, p.total || 0))}, [
        (function () { var sw = el("i", {class: "sw"}); sw.style.background = row[2]; return sw; })(),
        el("span", {class: "lname", text: row[1]}),
        el("span", {class: "lv", text: fmtN(row[3])})
      ]));
    });
    host.appendChild(lg);
  }

  function progPctFmt(v) { return Math.round(v) + "%"; }

  /* Chart A: done-% step area with the prog band and milestone dots. The
     shared domain maps through X; gridlines follow the B ticks while the
     labels draw once at the bottom of B. */
  function progChartA(host, p, X, gridTicks) {
    var f = progFrame(260, 18);
    var total = Math.max(1, p.total || 0);
    var pts = p.points || [];
    var Y = function (pct) { return f.T + f.ph - pct / 100 * f.ph; };
    var svg = progSvg(f, progAria(p.ws, p.done, p.total, progBest(p), progBestElapsed(p)));
    var doneOf = function (pt) { return pt.done / total * 100; };
    var withProg = function (pt) { return (pt.done + (pt.prog || 0)) / total * 100; };
    /* Prog band between the done line and done+prog, then the done area. */
    var top = progStep(f, X, Y, pts, withProg);
    if (pts.length) {
      svg.appendChild(s("path", {d: top + " L" + progR1(X(pts[pts.length - 1].t)) + "," + progR1(Y(doneOf(pts[pts.length - 1]))) + " L" + progR1(f.L) + "," + progR1(Y(doneOf(pts[0]))) + " Z", fill: "var(--s5)", opacity: ".12"}));
      var line = progStep(f, X, Y, pts, doneOf);
      var baseY = f.T + f.ph;
      svg.appendChild(s("path", {d: line + " L" + progR1(X(pts[pts.length - 1].t)) + "," + baseY + " L" + progR1(f.L) + "," + baseY + " Z", fill: "var(--s1)", opacity: ".16"}));
      svg.appendChild(s("path", {d: line, fill: "none", stroke: "var(--s1)", "stroke-width": "2.5"}));
      svg.appendChild(s("path", {d: top, fill: "none", stroke: "var(--s5)", "stroke-width": "1.6", "stroke-dasharray": "5 4"}));
    }
    /* One dot per step vertex: every mark tooltips with time, counts,
       and elapsed time since the workstream started. */
    pts.forEach(function (pt) {
      var dot = s("circle", {cx: progR1(X(pt.t)), cy: progR1(Y(doneOf(pt))), r: "2.6", fill: "var(--s1)", "data-t": String(pt.t), "data-v": String(pt.done)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(pt.t), progStepRows(pt.done, total, pt.prog || 0));
      });
      dot.addEventListener("mouseleave", hideTip);
      dot.addEventListener("focus", function (ev) {
        showTip(ev, fmtDate(pt.t), progStepRows(pt.done, total, pt.prog || 0));
      });
      dot.addEventListener("blur", hideTip);
      dot.setAttribute("tabindex", "0");
      svg.appendChild(dot);
    });
    /* Milestone dots with % labels, staggered so close marks stay apart. */
    var marks = (p.milestones || []).filter(function (m) { return m.reached; });
    var tagged = marks.map(function (m) { return {m: m, y: Y(m.pct)}; });
    tagged.sort(function (a, z) { return a.y - z.y; });
    tagged.forEach(function (tag, i) {
      if (i) tag.y = Math.max(tag.y, tagged[i - 1].y + 14);
      var m = tag.m;
      var dot = s("circle", {cx: progR1(X(m.t)), cy: progR1(Y(m.pct)), r: "4.5", fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": "1.5", "data-milestone": String(m.pct), "data-t": String(m.t), "data-v": String(m.done)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(m.t) + " — " + m.pct + "% first reached", progMileRows(m.done, total, m.elapsed));
      });
      dot.addEventListener("mouseleave", hideTip);
      dot.addEventListener("focus", function (ev) {
        showTip(ev, fmtDate(m.t) + " — " + m.pct + "% first reached", progMileRows(m.done, total, m.elapsed));
      });
      dot.addEventListener("blur", hideTip);
      dot.setAttribute("tabindex", "0");
      svg.appendChild(dot);
      var lab = s("text", {x: Math.min(progR1(X(m.t)) + 8, f.L + f.pw - 4), y: tag.y + 4, class: "plab"});
      lab.textContent = m.pct + "%";
      svg.appendChild(lab);
    });
    var axg = axes(svg, {L: f.L, T: f.T, pw: f.pw, ph: f.ph, yMax: 100, yTicks: 4, yFmt: progPctFmt, xTicks: gridTicks || [], yTitle: "% done", xTitle: ""});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
  }

  /* Chart B: runs started per hour, same x domain, labels once here. */
  function progChartB(host, p, X, t0, t1) {
    var f = progFrame(120, 46);
    var bars = p.runs_per_hour || [];
    var peak = 0;
    bars.forEach(function (b) { if (b.n > peak) peak = b.n; });
    var nice = niceMax(Math.max(1, peak));
    var Y = function (v) { return f.T + f.ph - v / nice * f.ph; };
    var svg = progSvg(f, "Agent runs started per hour for " + p.ws + ": " + fmtN(p.runs_total || 0) + " runs in " + fmtN(bars.length) + " hours");
    var slot = bars.length ? f.pw / bars.length : f.pw;
    var bw = Math.max(3, Math.min(28, slot - 4));
    bars.forEach(function (b) {
      var h = f.ph * b.n / nice;
      var x = progR1(X(b.t) + (slot - bw) / 2);
      var bar = s("rect", {x: x, y: progR1(Y(b.n)), width: bw, height: Math.max(progR1(h), 0.4), rx: Math.min(5, bw / 2), fill: "var(--s2)", "data-t": String(b.t), "data-v": String(b.n)});
      bar.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(b.t), progBarRows(b.n));
      });
      bar.addEventListener("mouseleave", hideTip);
      svg.appendChild(bar);
      var hit = s("rect", {x: x - 1, y: f.T, width: bw + 2, height: f.ph, fill: "transparent", "data-t": String(b.t), "data-v": String(b.n)});
      hit.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(b.t), progBarRows(b.n));
      });
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    var xt = timeTicks(t0, t1, X, 6);
    var axg = axes(svg, {L: f.L, T: f.T, pw: f.pw, ph: f.ph, yMax: nice, yTicks: 2, xTicks: xt, yTitle: "runs", xTitle: "time"});
    svg.insertBefore(axg, svg.firstChild);
    host.appendChild(svg);
  }

  function progBest(p) {
    var hit = null;
    (p.milestones || []).forEach(function (m) { if (m.reached && (!hit || m.pct > hit.pct)) hit = m; });
    return hit ? hit.pct + "%" : null;
  }

  function progBestElapsed(p) {
    var hit = null;
    (p.milestones || []).forEach(function (m) { if (m.reached && (!hit || m.pct > hit.pct)) hit = m; });
    return hit ? hit.elapsed : null;
  }

  function progressCard() {
    var ws = state.ws;
    var c = el("div", {class: "card ws-progress", "data-nav": "Progress over time"});
    c.appendChild(el("h2", {text: "Progress over time"}));
    c.appendChild(el("p", {class: "sub", text: "Completion for this workstream — when each share of the work finished, plus agent runs started per hour below."}));
    if (ws === "all") {
      c.appendChild(el("div", {class: "empty", text: "Pick a workstream to see its progress over time."}));
      return c;
    }
    if (state.wsProgressError && !state.wsProgress) {
      c.appendChild(el("div", {class: "empty err", "data-failed-scope": "workstream", text: "request failed — " + state.wsProgressError}));
      return c;
    }
    if (!state.wsProgress || state.wsProgress.ws !== ws) {
      var stale = !state.wsProgressAt || (Date.now() - state.wsProgressAt > PROG_TTL_MS);
      if (stale) progFetch(ws);
      c.appendChild(el("div", {class: "empty", text: "Loading progress over time…"}));
      return c;
    }
    var p = state.wsProgress;
    var pts = p.points || [];
    if (!pts.length) {
      c.appendChild(el("div", {class: "empty", text: "No recorded task moves yet."}));
      return c;
    }
    progLegend(c, p);
    /* One shared time domain for both charts: the data span extended to
       now so the end tick reads live. A keeps the tick positions as
       gridlines while the labels draw once at the bottom of B. */
    var t0 = p.start, t1 = Math.max(p.end || 0, Date.now() / 1000);
    if (!(t1 > t0)) t1 = t0 + 3600;
    var ref = progFrame(260, 18);
    var X = function (t) { return ref.L + (t - t0) / (t1 - t0) * ref.pw; };
    var ticks = timeTicks(t0, t1, X, 6);
    var grid = ticks.map(function (tk) { return {x: tk.x}; });
    progChartA(c, p, X, grid);
    progChartB(c, p, X, t0, t1);
    c.appendChild(el("p", {class: "prog-takeaway", text: progTakeaway(p.done, p.total, p.pct || 0, progBest(p), progBestElapsed(p), p.runs_total || 0)}));
    if (p.gap) c.appendChild(el("p", {class: "prog-gap", "data-ws": p.ws, text: p.ws + ": " + p.gap}));
    if ((p.inferred || 0) > 0) c.appendChild(el("p", {class: "prog-note", text: fmtN(p.inferred) + " done moments estimated from runs or dependencies, not recorded transitions."}));
    return c;
  }

  /* Registry spec for the workstream tab, below the
     planning-versus-execution card. */
  function progressSpec(passedState, snap) {
    return ["progress-ws", keyOf([state.ws, state.wsProgress, state.wsProgressError, state.seq]), function () { return progressCard(); }, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.progress = progressCard;
  AdPage.cards.progLegendTip = progLegendTip;
  AdPage.cards.progMileRows = progMileRows;
  AdPage.cards.progStepRows = progStepRows;
  AdPage.cards.progBarRows = progBarRows;
  global.progLegendTip = progLegendTip;
  global.progMileRows = progMileRows;
  global.progStepRows = progStepRows;
  global.progBarRows = progBarRows;
  global.progTakeaway = progTakeaway;
  global.progressSpec = progressSpec;
  global.progressCard = progressCard;
})(window);
