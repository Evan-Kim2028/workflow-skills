/* Page core: keyed caches and the keyed-section renderer.
   Exports (on window.AdPage, also kept as plain globals): execCache,
   execGen (one entry per tab and usage grouping for the fixed 24 h window;
   execGen is the refresh generation guard, ticked through the window copy
   by api.js, never read here); groupFor, reqKey, getCache, setCache,
   applyHit, showLoading (key, freshness, hit-apply, and miss-loading paths);
   keyOf (section data keys); applySections (the section-diff renderer used
   by drawMain: rebuilds only sections whose key changed, keeps node order,
   drops the rest). secCache (the section store) is also shared by plain
   global through an accessor, so the page script's scope reset and key
   deletes hit this same store instead of a stale copy.
   Needs: window.ADC (static/tokencache.js); state and el as plain globals. */
(function (global) {
  "use strict";
  var execCache = {};
  var execGen = 0;
  /* One source per number: the exec cache is keyed by tab and usage
     grouping (one fixed 24 h window), and an entry older than one refresh
     interval never draws. A tab switch draws cached data only when its key
     matches, otherwise a loading state, never another tab's numbers. */
  function groupFor() { return state.ws === "all" ? state.groupBy : "model"; }
  function reqKey() { return ADC.currentKey(state); }
  function getCache() {
    var hit = execCache[reqKey()];
    if (hit && ADC.fresh(hit, Date.now(), ADC.TTL_MS)) return hit;
    return null;
  }
  function setCache(key, body) { body.at = Date.now(); body.key = key; execCache[key] = body; }
  function applyHit(hit) {
    state.snap = hit.snap; state.runs = hit.runs; state.usage = hit.usage; state.conc = hit.conc; state.plan = hit.plan; state.seq = hit.seq; state.approvals = hit.approvals || [];
  }
  function showLoading() {
    state.snap = null; state.runs = []; state.usage = []; state.conc = []; state.plan = null; state.approvals = [];
    var m = document.getElementById("main");
    m.innerHTML = "";
    m.appendChild(el("div", {class: "card", id: "exec-loading"}, [el("h2", {text: "Loading " + (state.ws === "all" ? "overview" : state.ws) + " · last 24 h"}), el("div", {class: "skel"}), el("div", {class: "skel"}), el("div", {class: "skel"})]));
  }
  /* Keyed sections: a section is rebuilt only when its data key changes, and unchanged nodes are never moved, so animations and hover state survive live updates. */
  var secCache = {};
  function keyOf(v) { try { return JSON.stringify(v); } catch (e) { return String(Math.random()); } }
  /* Keyed sections, extracted verbatim from drawMain: a section is rebuilt
     only when its data key changes, and unchanged nodes are never moved, so
     animations and hover state survive live updates. */
  function applySections(m, specs) {
    var want = [];
    specs.forEach(function (sp) {
      var hit = secCache[sp[0]];
      if (!hit || hit.key !== sp[1]) {
        var node = el("section", {class: "sec", "data-sec": sp[0], id: "x-" + sp[0], style: sp[3]});
        var inner = sp[2](); if (inner) node.appendChild(inner);
        if (hit && hit.node.parentNode === m) m.replaceChild(node, hit.node);
        secCache[sp[0]] = hit = {key: sp[1], node: node};
      }
      want.push(hit.node);
    });
    want.forEach(function (node, idx) { if (m.children[idx] !== node) m.insertBefore(node, m.children[idx] || null); });
    while (m.children.length > want.length) m.removeChild(m.lastChild);
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.execCache = execCache;
  AdPage.execGen = execGen;
  AdPage.groupFor = groupFor;
  AdPage.reqKey = reqKey;
  AdPage.getCache = getCache;
  AdPage.setCache = setCache;
  AdPage.applyHit = applyHit;
  AdPage.showLoading = showLoading;
  AdPage.keyOf = keyOf;
  AdPage.applySections = applySections;
  Object.defineProperty(global, "secCache", {
    configurable: true,
    get: function () { return secCache; },
    set: function (v) { secCache = v; }
  });
  Object.defineProperty(AdPage, "secCache", {
    configurable: true,
    get: function () { return secCache; },
    set: function (v) { secCache = v; }
  });
  global.execCache = execCache;
  global.execGen = execGen;
  global.groupFor = groupFor;
  global.reqKey = reqKey;
  global.getCache = getCache;
  global.setCache = setCache;
  global.applyHit = applyHit;
  global.showLoading = showLoading;
  global.keyOf = keyOf;
  global.applySections = applySections;
})(window);
