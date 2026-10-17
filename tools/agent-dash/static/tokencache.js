/* Cache keys and stale-response guards for the execution view.
   No DOM and no fetch here: pure helpers shared by the page and its tests. */
(function (global) {
  "use strict";
  var TTL_MS = 30000;

  function key(ws, hours, groupBy) {
    return String(ws == null ? "all" : ws) + "|" + String(hours || 0) + "|" + String(groupBy || "model");
  }

  function fresh(entry, nowMs, ttlMs) {
    if (!entry || entry.at == null) return false;
    if (ttlMs == null) ttlMs = TTL_MS;
    return (nowMs - entry.at) <= ttlMs;
  }

  function currentKey(state) {
    var group = state.ws === "all" ? state.groupBy : "model";
    return key(state.ws, state.hours, group);
  }

  function applies(reqKey, state) {
    return reqKey === currentKey(state);
  }

  function billed(tokens) {
    tokens = tokens || {};
    return (tokens.input || 0) + (tokens.output || 0) + (tokens.cache_read || 0) + (tokens.cache_write || 0);
  }

  /* Short relative stamp for the zero-token tile ("last run 9 h ago"). */
  function ago(nowSec, stampSec) {
    if (stampSec == null) return null;
    var d = Math.max(0, nowSec - stampSec);
    if (d < 90) return "just now";
    if (d < 5400) return Math.round(d / 60) + " m ago";
    if (d < 129600) return Math.round(d / 3600) + " h ago";
    return Math.round(d / 86400) + " d ago";
  }

  global.ADC = {
    TTL_MS: TTL_MS,
    key: key,
    fresh: fresh,
    currentKey: currentKey,
    applies: applies,
    billed: billed,
    ago: ago
  };
})(typeof window !== "undefined" ? window : globalThis);
