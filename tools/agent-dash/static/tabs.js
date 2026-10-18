/* Workstream tab filter and order: pure helpers shared by the page and its tests.
   No DOM and no fetch here. The tab filter and the "Where we are" status use
   the same isCompleted() rule over the same server progress object. */
(function (global) {
  "use strict";
  var MODES = ["all", "active", "completed"];
  var MODE_KEY = "agent-dash-tab-filter";
  var ORDER_KEY = "agent-dash-tab-order";

  /* One rule for "completed": total > 0 and done === total, read from the
     server progress object. A workstream with no plan (null progress) is
     never completed. */
  function isCompleted(progress) {
    if (!progress) return false;
    var total = +progress.total, done = +progress.done;
    return !!(total > 0 && done === total);
  }

  /* Lifecycle states that keep a tab: active, paused, and done. Planned
     workstreams live in the Planned view ("no tab yet"); archived and
     dropped leave the tab bar for the Archive shelf. Unknown or absent
     reads as active, the same default the API uses. */
  var TAB_STATES = ["active", "paused", "done"];
  function wsStateOf(row) {
    var s = row && row.state;
    return s === "planned" || s === "active" || s === "paused" || s === "done" || s === "archived" || s === "dropped" ? s : "active";
  }
  function isTabState(state) {
    return state === "active" || state === "paused" || state === "done";
  }

  function normMode(mode) {
    return mode === "active" || mode === "completed" ? mode : "all";
  }

  function idOf(row) {
    return String((row && (row.ws || row.id)) || "").toUpperCase();
  }

  function isOverview(row) {
    var id = idOf(row);
    return id === "ALL" || id === "OVERVIEW";
  }

  /* Order workstream rows by a saved id list. The overview row stays pinned
     first. Ids in the saved order that are gone are dropped; rows not in the
     saved order (new workstreams) appear at the end in their input order. */
  function applyOrder(rows, saved) {
    rows = rows || [];
    var pinned = rows.length && isOverview(rows[0]) ? [rows[0]] : [];
    var pool = pinned.length ? rows.slice(1) : rows.slice();
    if (!saved || !saved.length) return pinned.concat(pool);
    var want = saved.map(function (id) { return String(id).toUpperCase(); });
    var byId = {};
    pool.forEach(function (row) {
      var id = idOf(row);
      if (id && !byId[id]) byId[id] = row;
    });
    var out = [], seen = {};
    want.forEach(function (id) {
      if (byId[id] && !seen[id]) { out.push(byId[id]); seen[id] = true; }
    });
    pool.forEach(function (row) {
      if (!seen[idOf(row)]) { out.push(row); seen[idOf(row)] = true; }
    });
    return pinned.concat(out);
  }

  /* Counts for the filter control, over workstream rows (overview excluded). */
  function countByMode(rows) {
    var counts = {all: 0, active: 0, completed: 0};
    (rows || []).forEach(function (row) {
      if (isOverview(row)) return;
      counts.all += 1;
      if (isCompleted(row && row.progress)) counts.completed += 1;
      else counts.active += 1;
    });
    return counts;
  }

  function visibleInMode(row, mode) {
    if (isOverview(row)) return true;
    var done = isCompleted(row && row.progress);
    if (mode === "completed") return done;
    if (mode === "active") return !done;
    return true;
  }

  /* Filter rows by mode. The overview tab always shows. The selected tab
     always stays visible: when the mode would hide it, it is kept and
     marked muted so the exception is visible. Returns fresh row objects
     with a muted boolean; the input rows are not modified. */
  function filterByMode(rows, mode, selectedWs) {
    mode = normMode(mode);
    var selected = String(selectedWs == null ? "all" : selectedWs).toUpperCase();
    if (selected === "OVERVIEW") selected = "ALL";
    var out = [];
    (rows || []).forEach(function (row) {
      var id = idOf(row);
      var keep = visibleInMode(row, mode);
      var muted = false;
      if (!keep && id === selected) { keep = true; muted = true; }
      if (!keep) return;
      var copy = {};
      for (var key in row) copy[key] = row[key];
      copy.muted = muted;
      out.push(copy);
    });
    return out;
  }

  /* Move one id left (dir -1) or right (dir +1) inside an order list.
     Clamped at the ends; unknown ids return the list unchanged. */
  function move(order, ws, dir) {
    var list = (order || []).slice();
    var id = String(ws == null ? "" : ws).toUpperCase();
    var at = -1;
    for (var i = 0; i < list.length; i++) {
      if (String(list[i]).toUpperCase() === id) { at = i; break; }
    }
    if (at < 0) return list;
    var to = at + (dir < 0 ? -1 : 1);
    if (to < 0 || to >= list.length) return list;
    var tmp = list[at];
    list[at] = list[to];
    list[to] = tmp;
    return list;
  }

  function orderOf(rows) {
    return (rows || []).filter(function (row) { return !isOverview(row); }).map(idOf);
  }

  function completeTitle(progress) {
    var done = (progress && progress.done) || 0, total = (progress && progress.total) || 0;
    return "complete: " + done + " of " + total + " tasks done";
  }

  function readKey(key) {
    try {
      if (typeof localStorage === "undefined") return null;
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function writeKey(key, value) {
    try {
      if (typeof localStorage === "undefined") return;
      localStorage.setItem(key, value);
    } catch (e) {}
  }

  function loadMode() {
    return normMode(readKey(MODE_KEY));
  }

  function saveMode(mode) {
    writeKey(MODE_KEY, normMode(mode));
  }

  function loadOrder() {
    var raw = readKey(ORDER_KEY);
    if (!raw) return null;
    try {
      var parsed = JSON.parse(raw);
      if (parsed && parsed.length !== undefined) {
        var list = [];
        for (var i = 0; i < parsed.length; i++) {
          var id = String(parsed[i] == null ? "" : parsed[i]).toUpperCase();
          if (id && list.indexOf(id) < 0) list.push(id);
        }
        return list;
      }
      return null;
    } catch (e) {
      return null;
    }
  }

  function saveOrder(order) {
    try {
      writeKey(ORDER_KEY, JSON.stringify(order || []));
    } catch (e) {}
  }

  global.ADT = {
    MODES: MODES,
    MODE_KEY: MODE_KEY,
    ORDER_KEY: ORDER_KEY,
    TAB_STATES: TAB_STATES,
    wsStateOf: wsStateOf,
    isTabState: isTabState,
    isCompleted: isCompleted,
    normMode: normMode,
    applyOrder: applyOrder,
    countByMode: countByMode,
    filterByMode: filterByMode,
    move: move,
    orderOf: orderOf,
    completeTitle: completeTitle,
    loadMode: loadMode,
    saveMode: saveMode,
    loadOrder: loadOrder,
    saveOrder: saveOrder
  };
})(typeof window !== "undefined" ? window : globalThis);
