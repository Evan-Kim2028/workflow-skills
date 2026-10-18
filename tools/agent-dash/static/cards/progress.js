/* Progress-over-time helpers: the pure series math and hover text for
   the Activity over time card's progress strip (static/cards/activity.js).
   The standalone Progress over time card is gone: the strip below the
   run timeline paints the same Done plus Merged-waiting-on-prod series
   as % of tasks on the card's one shared x axis. Series meaning and
   colours are unchanged (Done var(--s1) blue, Merged waiting on prod
   var(--s5) magenta). Data is GET /api/ws/progress?ws=<ID> per
   workstream, ?ws=all for the project total (../fixtures/ws-progress-<ID>.json
   under ?fixtures); the strip owns the fetch and its cache.
   Exports (on window.AdPage.cards, also kept as plain globals):
   progLegendTip, progMileRows, progStepRows, progBarRows, progTakeaway,
   progBest, progBestElapsed, progStep, progPctFmt.
   Needs: fmtN, fmtPct, fmtDur (plain globals). */
(function (global) {
  "use strict";

  /* Pure hover-text builders: every number the user sees on hover is
     compact (fmtN/fmtPct/fmtDur), with the exact value left to
     data-v/data-t attributes for tests and agents. */
  function progLegendTip(name, share) { return name + " " + fmtPct(share) + " of tasks"; }
  function progMileRows(done, total, elapsed) {
    return [{k: "Done", v: fmtN(done) + " of " + fmtN(total) + " tasks", c: "var(--s1)"},
      {k: "Elapsed", v: fmtDur(elapsed), c: "var(--muted)"}];
  }
  function progStepRows(done, totalAll, prog) {
    return [{k: "Done", v: fmtN(done) + " of " + fmtN(totalAll) + " tasks", c: "var(--s1)"},
      {k: "Merged, waiting on prod", v: fmtN(prog) + " tasks", c: "var(--s5)"}];
  }
  function progBarRows(n) { return [{k: "Runs started", v: fmtN(n), c: "var(--s2)"}]; }
  function progTakeaway(done, total, pct, best, bestElapsed, runs) {
    var head = fmtN(done) + " of " + fmtN(total) + " tasks done (" + fmtPct(pct / 100) + ")";
    if (best) return head + " — first reached " + best + " after " + fmtDur(bestElapsed) + "; " + fmtN(runs) + " agent runs started.";
    return head + " — no 20% milestone reached yet; " + fmtN(runs) + " agent runs started.";
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

  function progR1(x) { return Math.round(x * 10) / 10; }

  /* Step path through (t, v) points: flat from the axis, then up at each
     event (step-after). One-decimal pixel rounding via progR1. Pure: f
     carries only the left margin ({L}), X maps time to pixels, Y maps
     percent to pixels, get reads the plotted value off a point. */
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

  function progPctFmt(v) { return Math.round(v) + "%"; }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.progLegendTip = progLegendTip;
  AdPage.cards.progMileRows = progMileRows;
  AdPage.cards.progStepRows = progStepRows;
  AdPage.cards.progBarRows = progBarRows;
  AdPage.cards.progTakeaway = progTakeaway;
  AdPage.cards.progBest = progBest;
  AdPage.cards.progBestElapsed = progBestElapsed;
  AdPage.cards.progStep = progStep;
  AdPage.cards.progPctFmt = progPctFmt;
  global.progLegendTip = progLegendTip;
  global.progMileRows = progMileRows;
  global.progStepRows = progStepRows;
  global.progBarRows = progBarRows;
  global.progTakeaway = progTakeaway;
  global.progBest = progBest;
  global.progBestElapsed = progBestElapsed;
  global.progStep = progStep;
  global.progPctFmt = progPctFmt;
})(window);
