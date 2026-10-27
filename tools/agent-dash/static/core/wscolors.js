/* Shared workstream colours: one stable colour per workstream id.
   Hashes the id onto the categorical palette (--s1..--s11); when the
   caller passes the page workstream list, the first eight active ids
   probe to distinct slots. Done workstreams desaturate toward muted;
   unassigned/Other stay muted. */
(function (global) {
  "use strict";
  var PALETTE = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8", "--s9", "--s10", "--s11"];
  function hashStr(s) {
    var h = 5381;
    s = String(s == null ? "" : s);
    for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return h;
  }
  function isMutedKey(ws) {
    var k = String(ws == null ? "" : ws).toLowerCase();
    return !k || k === "unassigned" || k === "other" || k === "null" || k === "unattributed";
  }
  function wsStateOf(ws, state) {
    if (!state) return "";
    if (typeof state === "string") return state.toLowerCase();
    try {
      if (state && typeof state === "object" && !Array.isArray(state) && state.state) {
        if (String(state.state).toLowerCase() === "done") return "done";
      }
      var list = Array.isArray(state) ? state : state.wss || state.workstreams || null;
      if (list && typeof list.length === "number") {
        for (var i = 0; i < list.length; i++) {
          var w = list[i];
          if (!w) continue;
          var id = w.ws != null ? w.ws : w.id;
          if (String(id).toUpperCase() !== String(ws).toUpperCase()) continue;
          var st = w.state != null ? w.state : (w.status != null ? w.status : "");
          if (String(st).toLowerCase() === "done") return "done";
          if (w.done != null && w.total != null && +w.total > 0 && +w.done >= +w.total) return "done";
          return String(st || "").toLowerCase();
        }
      }
      if (!Array.isArray(state) && state[String(ws)]) {
        var e = state[String(ws)];
        var es = e && typeof e === "object" ? (e.state || e.status || "") : e;
        return String(es || "").toLowerCase();
      }
    } catch (e) {}
    return "";
  }
  function slotOf(ws, state) {
    var n = PALETTE.length, want = hashStr(ws) % n, slot = want;
    var peers = null;
    try {
      var list = Array.isArray(state) ? state : (state && (state.wss || state.workstreams)) || null;
      if (list && typeof list.length === "number" && list.length > 1) {
        peers = [];
        for (var i = 0; i < list.length && peers.length < 8; i++) {
          var w = list[i];
          if (!w) continue;
          var id = w.ws != null ? w.ws : w.id;
          if (id == null || isMutedKey(id)) continue;
          peers.push(String(id));
        }
      }
    } catch (e) { peers = null; }
    if (peers && peers.indexOf(String(ws)) >= 0) {
      var taken = {};
      for (var p = 0; p < peers.length; p++) {
        if (peers[p] === String(ws)) break;
        taken[hashStr(peers[p]) % n] = 1;
        var s2 = hashStr(peers[p]) % n, guard = 0;
        while (taken[s2] && guard++ < n) s2 = (s2 + 1) % n;
        taken[s2] = 1;
      }
      var g = 0;
      while (taken[slot] && g++ < n) slot = (slot + 1) % n;
    }
    return slot;
  }
  function wsColor(ws, state) {
    if (isMutedKey(ws)) return "var(--other)";
    var base = "var(" + PALETTE[slotOf(ws, state)] + ")";
    if (wsStateOf(ws, state) === "done") return "color-mix(in srgb, " + base + " 45%, var(--muted))";
    return base;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  /* core/el.js loads after this file and assigns AdPage.wsColor to its
     first-seen cache object; keep this contract a function through a
     guarded accessor so both stay intact (the cache lands on
     __wsColorCache, the closure reader in el.js is unaffected). */
  AdPage.__wsColorFn = wsColor;
  AdPage.__wsColorCache = AdPage.__wsColorCache || null;
  try {
    Object.defineProperty(AdPage, "wsColor", {
      configurable: true, enumerable: true,
      get: function () { return AdPage.__wsColorFn || wsColor; },
      set: function (v) {
        if (typeof v === "function") AdPage.__wsColorFn = v;
        else AdPage.__wsColorCache = v;
      }
    });
  } catch (e) {
    AdPage.wsColor = wsColor;
  }
})(typeof window !== "undefined" ? window : globalThis);
