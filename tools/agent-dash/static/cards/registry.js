/* Page cards registry: the ordered card list for every view.
   drawMain builds its section specs by iterating this list in order, so
   adding a card is one file under static/cards/ plus one line here:
   {id: "<section>", order: <n>, scope: "<scope>",
    spec: function (state, snap) { ... }}.
   spec returns one applySections spec [name, key, render, style], or null
   when the card does not apply (wrong tab, no data). Scope filters on the
   view: "overview" cards only render on ws=all, "workstream" cards only
   off it, "both" on either exec tab; "project" entries render on the
   Project home (?view=project) and "planned" entries on the Planned view
   (?view=planned). A project/planned entry with a slot naming one of the
   view's slot containers (exec-burnup, exec-health, exec-depgraph)
   mounts inside that slot; one without a slot renders as its own section
   after the slots, in registry order. A chart needed on both views adds
   one line per scope over the same spec function. So a
   later chart card stays one file plus one registry line, with no routing
   edits. registryFor(scope) returns the matching entries in order.
   Each spec closes over nothing at load; every name it calls (tiles,
   keyOf, planSum, ...) is a plain global read at call time. */
(function (global) {
  "use strict";
  var AdPage = (global.AdPage = global.AdPage || {});
  function registryFor(scope) {
    return AdPage.cardRegistry.filter(function (c) { return c.scope === scope; }).sort(function (a, b) { return a.order - b.order; });
  }
  AdPage.registryFor = registryFor;
  if (typeof window === "undefined") global.registryFor = registryFor;
  else window.registryFor = registryFor;
  AdPage.cardRegistry = [
    {id: "program", order: 10, scope: "overview", spec: function (state, snap) {
      if (!state.wss.length) return null;
      return ["program", keyOf([state.wss.map(function (w) { return [w.ws, w.running, w.name, w.state]; }), planSum, planPrompt]), function () { return programPanel(["active", "paused", "done"]); }, ""];
    }},
    {id: "tiles", order: 20, scope: "both", spec: function (state, snap) {
      return ["tiles", keyOf([snap.counts, snap.totals, snap.totals_all_time, snap.last_activity, state.ws, state.snapError]), function () { return tiles(snap); }, state.ws === "all" ? "margin-top:14px" : ""];
    }},
    {id: "running", order: 30, scope: "both", spec: function (state, snap) {
      return ["running", keyOf((snap.running || []).map(runRow)), function () { return runsTable("Running now", "Click a row for details. Output \u25B2 means the out file grew since the last poll.", snap.running || [], state.snapError ? "request failed" : "Nothing running."); }, "margin-top:14px"];
    }},
    /* Workstream cards on the overview read the same snapshot rows the tiles
       read, so each card matches its workstream tab for the fixed 24 h window. */
    {id: "cards", order: 40, scope: "overview", spec: function (state, snap) {
      return ["cards", keyOf([(snap.by_workstream || []), planSum]), function () { return wsCards(); }, "margin-top:14px"];
    }},
    {id: "plan", order: 50, scope: "both", spec: function (state, snap) {
      if (!state.plan) return null;
      return ["plan", keyOf([state.plan, (snap.running || []).map(function (r) { return [r.label, r.workstream, r.run_id]; }), (snap.recent || []).map(function (r) { return [r.label, r.ended_at, r.verdict]; }), (state.runs || []).length]), function () { return planCard(state.plan, snap); }, "margin-top:14px"];
    }},
    {id: "tokens", order: 60, scope: "both", spec: function (state, snap) {
      return ["tokens", keyOf([state.usage, state.groupBy, Math.floor(Date.now() / 60000)]), function () { return tokenChart(); }, "margin-top:14px"];
    }},
    {id: "conc", order: 70, scope: "both", spec: function (state, snap) {
      return ["conc", keyOf([state.conc, Math.floor(Date.now() / 60000)]), function () { return concChart(snap); }, "margin-top:14px"];
    }},
    {id: "gantt", order: 80, scope: "both", spec: function (state, snap) {
      return ["gantt", keyOf([(state.runs || []).map(runRow), expanded, Math.floor(Date.now() / 60000)]), function () { return gantt(); }, "margin-top:14px"];
    }},
    {id: "finished", order: 90, scope: "both", spec: function (state, snap) {
      return ["finished", keyOf([(snap.recent || []).map(runRow), snap.recent_shown, snap.recent_total, snap.counts && snap.counts.finished_in_range]), function () { return runsTable("Finished (24 h)", finishedSub(snap), snap.recent || [], state.snapError ? "request failed" : "Nothing finished in the last 24 h."); }, "margin-top:14px"];
    }},
    {id: "decisions", order: 58, scope: "project", spec: function (state, snap) {
      return decisionsSpec(state, snap);
    }},
    /* Cross-workstream dependency graph: one file, one line per scope over
       the same spec (both views mount the exec-depgraph slot). */
    {id: "depgraph", order: 62, scope: "project", slot: "exec-depgraph", spec: function (state, snap) {
      return depGraphSpec(state, snap);
    }},
    {id: "depgraph-plan", order: 62, scope: "planned", slot: "exec-depgraph", spec: function (state, snap) {
      return depGraphSpec(state, snap);
    }},
    {id: "burnup", order: 50, scope: "project", slot: "exec-burnup", spec: function (state, snap) {
      return burnupSpec(state, snap);
    }}
  ];
})(window);
