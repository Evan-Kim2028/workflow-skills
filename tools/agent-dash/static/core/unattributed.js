/* Runs without a recorded model ("model not recorded") stay listed
   nowhere by default: every model legend, tooltip and model list goes
   through isUnattributed/filterKnown here, and a default-off
   Show/Hide unattributed button pair (one in the Activity over time
   header, one in the usage/tiles area) flips the one shared flag.
   The flag persists per page scope in sessionStorage behind try/catch
   (private mode never breaks the render), default off. Totals never
   change: tiles, costs, token sums and all-scope sums still include
   these runs; only their listed entries fold away (a stacked total
   keeps them as one muted Unattributed segment) or omit.
   Exports (on window.AdPage.unattributed as AdUnatt, also kept as plain
   globals): isUnattributed, filterKnown, billed, shown, setShown,
   noteText, toggleButton, flip, scopeKey.
   Needs: el, fmtN, secCache, drawMain (plain globals, call time only). */
(function (global) {
  "use strict";
  var KEY_PREFIX = "agent-dash-unattributed:";

  /* The same -unknown rule static/models.js nameOf uses: a key ending
     -unknown reads "model not recorded". Recorded-but-unknown ids read
     Other, never unknown, so they stay listed. */
  function isUnattributed(key) {
    return /-unknown$/.test(String(key == null ? "" : key));
  }

  /* Pure: the listed keys minus the unattributed ones. Lists simply
     omit them; totals are summed elsewhere and never pass through here. */
  function filterKnown(keys) {
    return (keys || []).filter(function (k) { return !isUnattributed(k); });
  }

  /* One-bucket billed tokens, the same four fields the tiles sum, so a
     hidden-run token note agrees with the tile headline by construction. */
  function billed(t) {
    t = t || {};
    return (t.input || 0) + (t.output || 0) + (t.cache_read || 0) + (t.cache_write || 0);
  }

  function scopeKey(ws) {
    return KEY_PREFIX + (ws || "all");
  }

  function store() {
    try {
      if (typeof window !== "undefined" && window.sessionStorage) return window.sessionStorage;
    } catch (e) {}
    return null;
  }

  /* Default off: absent storage (or any failure) reads hidden. */
  function shown(ws) {
    var s = store();
    if (!s) return false;
    try {
      return s.getItem(scopeKey(ws)) === "1";
    } catch (e) {
      return false;
    }
  }

  function setShown(ws, on) {
    var s = store();
    if (s) {
      try {
        if (on) s.setItem(scopeKey(ws), "1");
        else s.removeItem(scopeKey(ws));
      } catch (e) {}
    }
    return !!on;
  }

  /* Muted one-line note: "Hidden: 12 runs without a recorded model,
     1.2M tokens". Numbers are compact through the caller's fmt; a null
     token figure (no server field at hand) reads runs only. Pure. */
  function noteText(runs, tokens, fmt) {
    runs = Math.max(0, Math.floor(+runs || 0));
    var f = typeof fmt === "function" ? fmt : function (n) { return String(n); };
    var r = f(runs) + " run" + (runs === 1 ? "" : "s") + " without a recorded model";
    if (tokens == null) return "Hidden: " + r;
    return "Hidden: " + r + ", " + f(tokens) + " tokens";
  }

  /* The real button: "Show unattributed" when off (aria-pressed false),
     "Hide unattributed" when on. Clicking flips the shared flag and
     rebuilds the tiles and activity sections in place. */
  function toggleButton(ws) {
    var on = shown(ws);
    var b = el("button", {
      type: "button",
      class: "unatt-btn",
      "aria-pressed": on ? "true" : "false",
      title: on ? "List every model again, including runs without a recorded model" : "Fold runs without a recorded model out of the model lists"
    }, [on ? "Hide unattributed" : "Show unattributed"]);
    b.addEventListener("click", function () { flip(ws); });
    return b;
  }

  /* One flip repaints both control areas: the tiles and activity
     sections rebuild from the new flag on the next draw. */
  function flip(ws) {
    setShown(ws, !shown(ws));
    try {
      if (typeof secCache !== "undefined") {
        delete secCache.tiles;
        delete secCache.activity;
      }
    } catch (e) {}
    try {
      if (typeof drawMain === "function") drawMain();
      else if (global && typeof global.drawMain === "function") global.drawMain();
    } catch (e) {}
  }

  var api = {
    isUnattributed: isUnattributed,
    filterKnown: filterKnown,
    billed: billed,
    scopeKey: scopeKey,
    shown: shown,
    setShown: setShown,
    noteText: noteText,
    toggleButton: toggleButton,
    flip: flip
  };
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.unattributed = api;
  global.AdUnatt = api;
  global.isUnattributed = isUnattributed;
})(typeof window !== "undefined" ? window : globalThis);
