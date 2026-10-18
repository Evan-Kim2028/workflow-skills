/* Page card: Decisions (read-only mirror).
   Newest-first rows from GET /api/decisions with status pills (glyph plus
   word), a status filter, workstream chips, and a 12-row page with "show
   more". Registered once (scope "project", no slot) so it renders as its
   own section after the home slots. The source is optional: while loading,
   when the endpoint is absent, when it reports source.present as false,
   or when it holds no rows, the spec returns null and the section stays
   an empty node — never a placeholder. Links render as anchors only for
   http(s) URLs, else plain text; the card never writes.
   Reads: state.decisions, state.wss, state.wssAll, state.seq, state.view.
   Exports (on window.AdPage.cards, also kept as plain globals):
   decisionsSpec, decisionsCard, sortDecisions, decisionPill.
   Needs: el, api, keyOf, drawMain, secCache, state (plain globals). */
(function (global) {
  "use strict";
  var DEC_URL = "/api/decisions";
  var DEC_SEC = "decisions";
  var PAGE = 12;
  var decLoading = false;
  var decFilter = "all";
  var decExpanded = false;

  /* Pure: status pill (glyph plus word, never colour alone). Unknown
     statuses read as plain words with a neutral dot. */
  function decisionPill(status) {
    var word = String(status == null ? "" : status);
    if (word === "approved") return {cls: "dc-pill ok", text: "\u2713 Approved"};
    if (word === "denied") return {cls: "dc-pill no", text: "\u2717 Denied"};
    if (word === "pending") return {cls: "dc-pill wait", text: "\u25CB Pending"};
    return {cls: "dc-pill other", text: "\u25CF " + (word ? word : "Unknown")};
  }

  /* Pure: newest first by date; undated rows sink to the end, stable. */
  function sortDecisions(rows) {
    var stamped = (rows || []).map(function (row, index) {
      var parsed = Date.parse(row && row.date);
      return {row: row, index: index, ts: isFinite(parsed) ? parsed : null};
    });
    stamped.sort(function (a, b) {
      if (a.ts == null && b.ts == null) return a.index - b.index;
      if (a.ts == null) return 1;
      if (b.ts == null) return -1;
      if (b.ts !== a.ts) return b.ts - a.ts;
      return a.index - b.index;
    });
    return stamped.map(function (entry) { return entry.row; });
  }

  function decPayload() {
    var d = state.decisions;
    return (d && typeof d === "object") ? d : null;
  }

  function decLive() {
    var d = decPayload();
    return !!(d && d.source && d.source.present && Array.isArray(d.decisions) && d.decisions.length);
  }

  function redrawDecisions() {
    try {
      if (typeof secCache !== "undefined" && secCache) delete secCache[DEC_SEC];
    } catch (e) {}
    if (state.view === "project" && typeof drawMain === "function") drawMain();
  }

  function loadDecisions() {
    if (decLoading) return;
    decLoading = true;
    api(DEC_URL, {}).then(function (j) {
      decLoading = false;
      state.decisions = (j && typeof j === "object") ? j : {decisions: [], counts: {}, source: {present: false}};
      redrawDecisions();
    }).catch(function () {
      decLoading = false;
      state.decisions = {decisions: [], counts: {}, source: {present: false}};
      redrawDecisions();
    });
  }

  function wsTitle(id) {
    var pools = [state.wssAll, state.wss];
    for (var i = 0; i < pools.length; i++) {
      var rows = pools[i] || [];
      for (var k = 0; k < rows.length; k++) {
        if (rows[k] && String(rows[k].ws).toUpperCase() === String(id).toUpperCase()) return rows[k].name || id;
      }
    }
    return id;
  }

  function decLink(row) {
    var link = row && row.link;
    if (typeof link === "string" && /^https?:\/\//i.test(link)) {
      var a = el("a", {class: "dc-link", href: link, rel: "noopener", target: "_blank"});
      a.textContent = link;
      return a;
    }
    if (link != null && String(link) !== "") {
      var span = el("span", {class: "dc-linktext"});
      span.textContent = String(link);
      return span;
    }
    return null;
  }

  function decRow(row) {
    var line = el("li", {class: "dc-row"});
    var top = el("div", {class: "dc-top"});
    var title = el("b", {class: "dc-title", text: (row && row.title) || (row && row.id) || "Decision"});
    title.title = (row && row.id) || "";
    top.appendChild(title);
    var pill = decisionPill(row && row.status);
    top.appendChild(el("span", {class: pill.cls, text: pill.text, title: String((row && row.status) || "unknown")}));
    line.appendChild(top);
    var meta = [];
    if (row && row.owner) meta.push(row.owner);
    if (row && row.date) meta.push(row.date);
    if (row && row.area) meta.push(row.area);
    if (row && row.workstream) {
      var chip = el("span", {class: "dc-ws", text: row.workstream, title: wsTitle(row.workstream)});
      line.appendChild(chip);
    }
    if (meta.length) line.appendChild(el("div", {class: "dc-meta", text: meta.join(" \u00B7 ")}));
    var link = decLink(row);
    if (link) line.appendChild(link);
    return line;
  }

  function decisionsCard() {
    var d = decPayload() || {};
    var rows = sortDecisions(d.decisions || []);
    var counts = (d.counts && typeof d.counts === "object") ? d.counts : {};
    var c = el("div", {class: "card dc-card", id: "exec-decisions", "data-nav": "Decisions"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Decisions"}));
    head.appendChild(el("p", {class: "sub", text: "Read-only mirror, newest first."}));
    c.appendChild(head);
    var statuses = [];
    rows.forEach(function (row) {
      var key = String((row && row.status) || "unknown");
      if (statuses.indexOf(key) < 0) statuses.push(key);
    });
    statuses.sort();
    var bar = el("div", {class: "dc-filters", role: "group", "aria-label": "Filter by status"});
    ["all"].concat(statuses).forEach(function (key) {
      var n = key === "all" ? rows.length : rows.filter(function (row) { return String((row && row.status) || "unknown") === key; }).length;
      if (counts[key] != null && key !== "all") n = +counts[key] || 0;
      var pill = key === "all" ? {cls: "", text: "All"} : decisionPill(key);
      var b = el("button", {type: "button", class: "dc-filter" + (decFilter === key ? " on" : ""), "aria-pressed": decFilter === key ? "true" : "false"});
      b.textContent = (key === "all" ? "All" : pill.text) + " (" + n + ")";
      (function (pick) {
        b.addEventListener("click", function () { decFilter = pick; redrawDecisions(); });
      })(key);
      bar.appendChild(b);
    });
    c.appendChild(bar);
    var shown = rows.filter(function (row) {
      return decFilter === "all" || String((row && row.status) || "unknown") === decFilter;
    });
    if (!shown.length) {
      c.appendChild(el("p", {class: "pj-note", text: "No " + decFilter + " decisions."}));
      return c;
    }
    var capped = decExpanded ? shown : shown.slice(0, PAGE);
    var list = el("ol", {class: "dc-list"});
    capped.forEach(function (row) { list.appendChild(decRow(row)); });
    c.appendChild(list);
    if (shown.length > PAGE) {
      var more = el("button", {type: "button", class: "dc-more", text: decExpanded ? "Show less" : "Show more (" + (shown.length - PAGE) + " more)"});
      more.addEventListener("click", function () { decExpanded = !decExpanded; redrawDecisions(); });
      var acts = el("div", {class: "dc-acts"});
      acts.appendChild(more);
      c.appendChild(acts);
    }
    return c;
  }

  function decisionsSpec(passedState, snap) {
    if (!decPayload() && !decLoading) loadDecisions();
    if (!decLive()) return null;
    if (decFilter !== "all") {
      var any = (decPayload().decisions || []).some(function (row) {
        return String((row && row.status) || "unknown") === decFilter;
      });
      if (!any) decFilter = "all";
    }
    return ["decisions", keyOf([state.decisions, decFilter, decExpanded, state.seq, state.view]), decisionsCard, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.decisions = decisionsSpec;
  global.decisionsSpec = decisionsSpec;
  global.decisionsCard = decisionsCard;
  global.sortDecisions = sortDecisions;
  global.decisionPill = decisionPill;
})(window);
