/* Page card: Log search (opt-in, read-only).
   A search box over archived agent logs held by the local VictoriaLogs
   store (scripts/vlogs_install.sh). Registered once (scope "project", no
   slot) so it renders as its own section on the Project home.

   Nothing loads until the owner opens the card: the closed card is one
   button. Opening it fetches facets (GET /api/logs/facets) and the newest
   results (GET /api/logs/search); typing is debounced (250 ms) and each
   new search aborts the one in flight. A down or empty store shows a
   plain empty state with the install command, never an error page.
   Results and the selected event render through textContent only.

   Deliberate deviation from the card convention: the search uses a raw
   GET fetch with an AbortController, because the shared api() helper has
   no abort signal and core/api.js is outside this feature's edit scope.
   Facets use api(). Both are GETs; the card never writes.

   Reads: state (not used for data), window.drawMain, secCache.
   Exports (on window.AdPage.cards, also kept as plain globals):
   logSearchSpec, logSearchCard, logSearchQueryString.
   Needs: el, api, keyOf, drawMain, secCache, fmtN, fmtDate,
   discloseRows, discloseButton, discloseRedraw, readDisclosed (plain
   globals, call time only). */
(function (global) {
  "use strict";
  var LS_SEC = "logsearch";
  var LS_DEBOUNCE_MS = 250;
  var LS_SINCE = [
    {v: "1h", t: "Last hour"},
    {v: "24h", t: "Last 24 hours"},
    {v: "7d", t: "Last 7 days"},
    {v: "30d", t: "Last 30 days"}
  ];
  var LS_LIMITS = ["100", "250", "500"];
  var LS_INSTALL = "sh scripts/vlogs_install.sh";

  /* Module state. Survives card rebuilds, so a redraw (opening, a
     disclosure toggle, a view switch and back) shows the same search. */
  var ls = {
    opened: false, q: "", ws: "", label: "", type: "", since: "30d", limit: "100",
    facets: null, facetsError: false, status: "idle", results: [], count: 0,
    selected: -1, error: ""
  };
  var lsTimer = null;
  var lsAbort = null;
  var lsSeq = 0;
  var lsFacetSeq = 0;
  /* Node references of the card most recently built. Paint helpers write
     through these, never through document lookups, because a section is
     built before it is inserted into the page. */
  var refs = null;

  /* Pure: the query string for one search. Empty filters are left out. */
  function logSearchQueryString(s) {
    var parts = [];
    ["q", "ws", "label", "type", "since", "limit"].forEach(function (key) {
      var value = s && s[key] != null ? String(s[key]).trim() : "";
      if (value !== "") parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
    });
    return parts.join("&");
  }

  /* Pure: the status sentence under the filters (words and fmtN numbers). */
  function lsStatusText(s) {
    if (!s) return "";
    if (s.status === "loading") return "Searching\u2026";
    if (s.status === "error") return "Search failed. Try again.";
    if (s.status === "down") return "The log store is not running on this machine.";
    if (s.status === "ok") {
      var n = s.count || 0;
      var text = fmtN(n) + (n === 1 ? " result" : " results") + ", newest first.";
      if (n > 0 && n >= (+s.limit || 0)) {
        text += " Showing the newest " + fmtN(+s.limit) + "; narrow the search for older events.";
      }
      return text;
    }
    return "";
  }

  function lsWhen(ts) {
    var parsed = Date.parse(ts);
    return isFinite(parsed) ? fmtDate(parsed / 1000) : String(ts || "");
  }

  function fillSelect(sel, items, allText, current) {
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    sel.appendChild(el("option", {value: "", text: allText}));
    var seen = false;
    items.forEach(function (item) {
      var o = el("option", {value: item.value});
      o.textContent = item.text;
      if (item.value === current) seen = true;
      sel.appendChild(o);
    });
    /* A filter picked before a facet reload keeps its value when the
       option still exists; otherwise it falls back to "all". */
    if (current && !seen) {
      var kept = el("option", {value: current});
      kept.textContent = current;
      sel.appendChild(kept);
    }
    sel.value = current || "";
  }

  function facetOptions(kind) {
    var f = ls.facets;
    if (!f) return [];
    if (kind === "ws") {
      return (f.workstreams || []).map(function (row) {
        return {value: row.ws, text: (row.ws || "(none)") + " (" + fmtN(row.count) + ")"};
      });
    }
    if (kind === "label") {
      return (f.labels || []).map(function (row) {
        return {value: row.label, text: (row.label || "(none)") + " (" + fmtN(row.count) + ")"};
      });
    }
    return (f.types || []).map(function (row) {
      return {value: row.type, text: (row.type || "(none)") + " (" + fmtN(row.count) + ")"};
    });
  }

  function paintFilters() {
    if (!refs) return;
    fillSelect(refs.ws, facetOptions("ws"), "All workstreams", ls.ws);
    fillSelect(refs.label, facetOptions("label"), "All labels", ls.label);
    fillSelect(refs.type, facetOptions("type"), "All event types", ls.type);
  }

  function paintStatus() {
    if (!refs) return;
    var text = lsStatusText(ls);
    if (ls.status === "ok" && ls.count === 0 && ls.facets && ls.facets.total === 0) {
      text = "The log store holds no archived events yet.";
    }
    if (ls.facetsError && !text) text = "Filters could not load.";
    refs.status.textContent = text;
  }

  function emptyBlock(lead) {
    var box = el("div", {class: "ls-empty"});
    box.appendChild(el("p", {text: lead}));
    box.appendChild(el("p", {class: "ls-empty-note", text: "Install and start the local store once (loopback only):"}));
    var pre = el("pre", {class: "ls-cmd"});
    var code = el("code");
    code.textContent = LS_INSTALL;
    pre.appendChild(code);
    box.appendChild(pre);
    return box;
  }

  function resultRow(row, index) {
    var li = el("li", {class: "ls-item"});
    var pick = el("button", {type: "button", class: "ls-row", "aria-pressed": ls.selected === index ? "true" : "false"});
    var meta = el("span", {class: "ls-meta"});
    var when = el("span", {class: "ls-when"});
    when.textContent = lsWhen(row.ts);
    meta.appendChild(when);
    [row.ws, row.label, row.type].forEach(function (part) {
      if (!part) return;
      var chip = el("span", {class: "ls-chip"});
      chip.textContent = part;
      meta.appendChild(chip);
    });
    pick.appendChild(meta);
    if (row.path) {
      var path = el("span", {class: "ls-path"});
      path.textContent = row.path;
      pick.appendChild(path);
    }
    var snip = el("span", {class: "ls-snip"});
    snip.textContent = row.snippet || "";
    pick.appendChild(snip);
    pick.addEventListener("click", function () {
      ls.selected = index;
      paintResults();
    });
    li.appendChild(pick);
    return li;
  }

  function detailBlock() {
    var row = ls.results[ls.selected];
    if (!row) return null;
    var box = el("div", {class: "ls-detail", "aria-label": "Selected event"});
    var head = el("p", {class: "ls-detail-head"});
    head.textContent = lsWhen(row.ts) + " \u00B7 " + [row.ws, row.label, row.type].filter(Boolean).join(" \u00B7 ");
    box.appendChild(head);
    if (row.path) {
      var path = el("p", {class: "ls-path"});
      path.textContent = row.path;
      box.appendChild(path);
    }
    var pre = el("pre", {class: "ls-text"});
    pre.textContent = row.text || "";
    box.appendChild(pre);
    if (row.text_truncated) {
      box.appendChild(el("p", {class: "ls-note", text: "Longer event: showing its first " + fmtN(2000) + " characters."}));
    }
    var close = el("button", {type: "button", class: "ls-close", text: "Hide event"});
    close.addEventListener("click", function () { ls.selected = -1; paintResults(); });
    box.appendChild(close);
    return box;
  }

  /* Rebuilds the results area from state: the empty states, the top-five
     list with the shared disclosure, and the selected event. */
  function resultsInto(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    if (ls.status === "down" || (ls.status === "ok" && ls.count === 0 && ls.facets && ls.facets.total === 0)) {
      node.appendChild(emptyBlock(ls.status === "down"
        ? "Log search needs the local log store, which is not running."
        : "No archived events are stored yet."));
      return;
    }
    if (ls.status === "ok" && ls.count === 0) {
      node.appendChild(el("p", {class: "ls-note", text: "No events match. Try fewer filters or a wider time window."}));
      return;
    }
    if (!ls.results.length) return;
    var info = discloseRows(ls.results, "logsearch", "events");
    var list = el("ol", {class: "ls-list", "aria-label": "Matching events, newest first"});
    info.rows.forEach(function (row, index) { list.appendChild(resultRow(row, index)); });
    node.appendChild(list);
    var more = discloseButton(info, "ls-list", discloseRedraw(LS_SEC, "ls-list"));
    if (more) {
      var acts = el("div", {class: "ls-acts"});
      acts.appendChild(more);
      node.appendChild(acts);
    }
    var detail = detailBlock();
    if (detail) node.appendChild(detail);
  }

  function paintResults() {
    if (!refs) return;
    resultsInto(refs.body);
    paintStatus();
  }

  function runSearch() {
    if (!refs) return;
    if (lsTimer) { clearTimeout(lsTimer); lsTimer = null; }
    if (lsAbort) { try { lsAbort.abort(); } catch (e) {} }
    var ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    lsAbort = ctl;
    var seq = ++lsSeq;
    var qs = logSearchQueryString(ls);
    ls.status = "loading";
    ls.selected = -1;
    paintStatus();
    var opts = {cache: "no-store", headers: {Accept: "application/json"}};
    if (ctl) opts.signal = ctl.signal;
    fetch("/api/logs/search" + (qs ? "?" + qs : ""), opts).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (j) {
      if (seq !== lsSeq) return;
      lsAbort = null;
      ls.status = j.status || "ok";
      ls.results = j.results || [];
      ls.count = j.count || ls.results.length;
      ls.error = j.error || "";
      paintResults();
    }).catch(function (err) {
      if (seq !== lsSeq) return;
      if (err && err.name === "AbortError") return;
      lsAbort = null;
      ls.status = "error";
      ls.results = [];
      paintResults();
    });
  }

  function loadFacets() {
    var seq = ++lsFacetSeq;
    api("/api/logs/facets", {since: ls.since}).then(function (j) {
      if (seq !== lsFacetSeq) return;
      ls.facets = j || null;
      ls.facetsError = false;
      paintFilters();
      paintStatus();
      if (ls.status === "ok" && ls.count === 0) paintResults();
    }).catch(function () {
      if (seq !== lsFacetSeq) return;
      ls.facets = null;
      ls.facetsError = true;
      paintStatus();
    });
  }

  function scheduleSearch() {
    if (lsTimer) clearTimeout(lsTimer);
    lsTimer = setTimeout(function () { lsTimer = null; runSearch(); }, LS_DEBOUNCE_MS);
  }

  function openLs() {
    ls.opened = true;
    try {
      if (typeof secCache !== "undefined" && secCache) delete secCache[LS_SEC];
    } catch (e) {}
    if (typeof drawMain === "function") drawMain();
    var box = document.getElementById("ls-q");
    if (box) box.focus();
    loadFacets();
    runSearch();
  }

  function closedBlock(c) {
    var open = el("button", {type: "button", class: "ls-open", "aria-expanded": "false", "aria-controls": "ls-panel", text: "Open log search"});
    open.addEventListener("click", openLs);
    c.appendChild(open);
    c.appendChild(el("p", {class: "ls-note", text: "Nothing loads until you open it. Results come from the local log store only."}));
  }

  function openedBlock(c) {
    var panel = el("div", {class: "ls-panel", id: "ls-panel"});
    var form = el("form", {class: "ls-form", role: "search"});
    form.addEventListener("submit", function (ev) { ev.preventDefault(); if (lsTimer) clearTimeout(lsTimer); runSearch(); });
    var q = el("input", {type: "search", id: "ls-q", class: "ls-q", maxlength: "200", autocomplete: "off", spellcheck: "false", "aria-label": "Text to find", placeholder: "Text to find, e.g. PASS or a label fragment", value: ls.q});
    q.addEventListener("input", function () { ls.q = q.value; scheduleSearch(); });
    form.appendChild(q);
    var ws = el("select", {class: "ls-sel", "aria-label": "Workstream"});
    var label = el("select", {class: "ls-sel", "aria-label": "Label"});
    var type = el("select", {class: "ls-sel", "aria-label": "Event type"});
    var since = el("select", {class: "ls-sel", "aria-label": "Time window"});
    LS_SINCE.forEach(function (opt) {
      var o = el("option", {value: opt.v});
      o.textContent = opt.t;
      since.appendChild(o);
    });
    since.value = ls.since;
    var limit = el("select", {class: "ls-sel", "aria-label": "Most results"});
    LS_LIMITS.forEach(function (v) {
      var o = el("option", {value: v});
      o.textContent = "Up to " + fmtN(+v);
      limit.appendChild(o);
    });
    limit.value = ls.limit;
    ws.addEventListener("change", function () { ls.ws = ws.value; runSearch(); });
    label.addEventListener("change", function () { ls.label = label.value; runSearch(); });
    type.addEventListener("change", function () { ls.type = type.value; runSearch(); });
    since.addEventListener("change", function () { ls.since = since.value; loadFacets(); runSearch(); });
    limit.addEventListener("change", function () { ls.limit = limit.value; runSearch(); });
    [ws, label, type, since, limit].forEach(function (s) { form.appendChild(s); });
    panel.appendChild(form);
    var status = el("p", {class: "ls-status", role: "status", "aria-live": "polite"});
    panel.appendChild(status);
    var body = el("div", {class: "ls-body"});
    panel.appendChild(body);
    c.appendChild(panel);
    refs = {ws: ws, label: label, type: type, status: status, body: body};
    paintFilters();
    paintStatus();
    resultsInto(body);
  }

  function logSearchCard() {
    var c = el("div", {class: "card ls-card", id: "exec-logsearch", "data-nav": "Log search"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Log search"}));
    head.appendChild(el("p", {class: "sub", text: "Opt-in, read-only search over archived agent logs in the local store. Newest first."}));
    c.appendChild(head);
    if (!ls.opened) {
      refs = null;
      closedBlock(c);
      return c;
    }
    openedBlock(c);
    return c;
  }

  function logSearchSpec(passedState, snap) {
    /* Stable key: the card is rebuilt only when it opens, so typing in the
       search box never rebuilds the section and keeps focus. */
    return [LS_SEC, keyOf(["logsearch", ls.opened ? 1 : 0]), logSearchCard, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.logsearch = logSearchSpec;
  global.logSearchSpec = logSearchSpec;
  global.logSearchCard = logSearchCard;
  global.logSearchQueryString = logSearchQueryString;
})(window);
