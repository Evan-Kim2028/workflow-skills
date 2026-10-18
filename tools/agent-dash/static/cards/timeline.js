/* Timeline row model and outcome colours, shared by the Activity over
   time stack (static/cards/activity.js) and the plan replay.
   expanded is the per-family lane open state; isAgent skips lead and
   shell rows in agent counts; runEnd draws a running run to now and an
   ended run with no recorded end as a one-minute stub; packRows
   greedy-packs a lane so every run is drawn, never a sample; outcomeOf,
   OUTCOME and outcomeColor colour bars by outcome (the replay inside
   plan-progress.js reads OUTCOME at call time).
   Exports (on window.AdPage, also kept as plain globals): expanded,
   isAgent, runEnd, packRows, outcomeOf, OUTCOME, outcomeColor.
   Needs: ADM, tierKey (plain globals). */
(function (global) {
  "use strict";
  /* timeline: one lane per model family, packed so every run is drawn */
  var expanded = {};
  /* A packed lane past this many rows becomes a density heat strip. */
  var DENSE_ROWS = 400;
  /* A run with no end time is drawn to now only while it is running; an ended run whose end was never seen gets a one-minute stub. */
  /* Lead sessions and shell rows are drawn but not counted as agents. */
  function isAgent(r) { return r.source !== "claude-main" && r.source !== "shell" && !r.lane_shadow; }
  function runEnd(r, now) { return r.ended_at || (r.status === "running" ? now : (r.started_at || now) + 60); }
  function packRows(runs, gapS) {
    var rows = [];
    runs.slice().sort(function (x, y) { return x.started_at - y.started_at; }).forEach(function (r) {
      var end = runEnd(r, Date.now() / 1000), placed = false;
      for (var i = 0; i < rows.length; i++) { if (rows[i].end + gapS <= r.started_at) { rows[i].runs.push(r); rows[i].end = end; placed = true; break; } }
      if (!placed) rows.push({end: end, runs: [r]});
    });
    return rows;
  }
  function outcomeOf(r) {
    if (!r.ended_at) return r.status === "running" ? "run" : "unk";
    if (r.status === "died_early" || r.status === "killed" || r.verdict === "BLOCKED" || r.verdict === "REQUEST_CHANGES" || r.verdict === "FAILURE" || r.verdict === "CANCELED") return "bad";
    if (r.verdict === "PARTIAL" || r.verdict === "APPROVE_WITH_CHANGES") return "part";
    if (r.status === "done" || r.verdict === "SUCCESS") return "ok";
    return "unk";
  }
  var OUTCOME = {run: ["var(--s1)", "\u25CF", "running"], ok: ["var(--good)", "\u2713", "finished"], part: ["var(--warning)", "\u25D0", "partial"], bad: ["var(--critical)", "\u2715", "died or blocked"], unk: ["var(--muted)", "?", "ended, outcome unknown"]};
  function outcomeColor(r) { var o = outcomeOf(r); return o === "run" || o === "unk" ? null : OUTCOME[o][0]; }
  function famOfRun(r) { return ADM.famOf(tierKey(r)); }
  function famName(f) { return (ADM.FAM[f] || ADM.FAM.other).name; }
  function famHue(f) { return "var(" + (ADM.FAM[f] || ADM.FAM.other).hue + ")"; }
  /* workstream cards */
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  global.expanded = expanded;
  global.isAgent = isAgent;
  global.runEnd = runEnd;
  global.packRows = packRows;
  global.outcomeOf = outcomeOf;
  global.OUTCOME = OUTCOME;
  global.outcomeColor = outcomeColor;
})(window);
