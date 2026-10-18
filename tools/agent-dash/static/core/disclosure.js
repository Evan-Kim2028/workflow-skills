/* Page core: one shared progressive-disclosure helper for every capped
   table or list. Every list that can exceed five rows shows the first five
   by default with a real button below it ("Show all N <things>" /
   "Show top 5 <things>", aria-expanded and aria-controls), hidden when the
   list holds five or fewer. Expanded state persists per list in
   sessionStorage behind try/catch and defaults to COLLAPSED: the key
   carries a v2 namespace, so an expanded state stored under an older key
   never applies. Running now (static/cards/tables.js) keeps its
   group-aware cap and reuses the state and slicing here.
   Exports (on window.AdPage, also kept as plain globals): TOP_N,
   discloseKey, readDisclosed, writeDisclosed, discloseSlice,
   discloseRows, discloseLabel, discloseButton, discloseRedraw.
   Needs: el (plain global, call time only); secCache and drawMain for
   discloseRedraw (call time only, each touched inside try/catch so the
   pure helpers stay node-runnable). */
(function (global) {
  "use strict";
  /* The default cap: five rows, everywhere. Callers pass an explicit
     limit only when a list genuinely needs another number. */
  var TOP_N = 5;
  /* v2 namespace: keys from before the top-5 rollout (e.g. the old
     "agent-dash-running:<scope>" expanded flag) live outside it, so an
     earlier expanded state never reopens a list that now defaults shut. */
  var PREFIX = "agent-dash-disclose:v2:";
  function keyOf(name) { return PREFIX + (name || "list"); }
  /* Expanded state for one list, default collapsed. Private mode may
     throw, so every touch is guarded and falls back to collapsed. */
  function readDisclosed(name) {
    try {
      return window.sessionStorage.getItem(keyOf(name)) === "all";
    } catch (e) { return false; }
  }
  function writeDisclosed(name, showAll) {
    try {
      if (showAll) window.sessionStorage.setItem(keyOf(name), "all");
      else window.sessionStorage.removeItem(keyOf(name));
    } catch (e) {}
  }
  /* Pure slicing: the top rows collapsed, every row expanded. Never
     mutates the input; short lists pass through untouched. */
  function slice(rows, showAll, limit) {
    rows = rows || [];
    var n = (limit > 0) ? limit : TOP_N;
    return showAll ? rows.slice() : rows.slice(0, n);
  }
  /* One call per render: the visible rows plus everything the button
     needs (total, limit, expanded flag, storage name, noun). */
  function discloseRows(rows, name, noun, limit) {
    rows = rows || [];
    var n = (limit > 0) ? limit : TOP_N;
    var showAll = readDisclosed(name);
    return {rows: slice(rows, showAll, n), total: rows.length, limit: n,
      showAll: showAll, name: name, noun: noun || "rows"};
  }
  /* The button names things, never a bare count: "Show all 12 running
     agents" collapsed, "Show top 5 agents" expanded. */
  function label(total, showAll, noun, limit) {
    var n = (limit > 0) ? limit : TOP_N;
    return showAll ? "Show top " + n + " " + noun : "Show all " + total + " " + noun;
  }
  /* The control itself: a real button below the list, null when the list
     fits (five or fewer), so short lists carry no dead control. onToggle
     rebuilds the list after the state flips. */
  function button(info, targetId, onToggle) {
    if (!info || info.total <= info.limit) return null;
    var b = el("button", {type: "button", class: "disclose-btn",
      "aria-expanded": info.showAll ? "true" : "false",
      "aria-controls": targetId,
      text: label(info.total, info.showAll, info.noun, info.limit)});
    b.addEventListener("click", function () {
      writeDisclosed(info.name, !readDisclosed(info.name));
      if (typeof onToggle === "function") onToggle();
    });
    return b;
  }
  /* Section redraw after a disclosure click: rebuilds only that section
     (the caller folds the expanded flag into its spec key) and hands
     keyboard focus back to the rebuilt button. */
  function redraw(section, targetId) {
    return function () {
      try { delete secCache[section]; } catch (e) {}
      try { drawMain(); } catch (e) {}
      try {
        var next = document.querySelector('[aria-controls="' + targetId + '"]');
        if (next && next.focus) next.focus();
      } catch (e) {}
    };
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.discloseTOP_N = TOP_N;
  AdPage.discloseKey = keyOf;
  AdPage.readDisclosed = readDisclosed;
  AdPage.writeDisclosed = writeDisclosed;
  AdPage.discloseSlice = slice;
  AdPage.discloseRows = discloseRows;
  AdPage.discloseLabel = label;
  AdPage.discloseButton = button;
  AdPage.discloseRedraw = redraw;
  global.TOP_N = TOP_N;
  global.discloseKey = keyOf;
  global.readDisclosed = readDisclosed;
  global.writeDisclosed = writeDisclosed;
  global.discloseSlice = slice;
  global.discloseRows = discloseRows;
  global.discloseLabel = label;
  global.discloseButton = button;
  global.discloseRedraw = redraw;
})(window);
