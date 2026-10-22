/* Page card: Linear (Execution view, scope "both").
   One chart first: status changes by source (local vs Linear) for the last
   24 h and 7 days, stacked, on the shared axes() helper with a legend. Then
   one row per linked workstream (project link, sync age, sign-in word,
   outbox count, sync state as glyph plus word), the recent status moves
   (top 5, shared disclosure), and the decisions waiting in Linear.
   Data: GET /api/linear (fixtures/linear.json in ?fixtures mode). The card
   filters to the selected workstream on a workstream tab and hides entirely
   when the list is empty or the endpoint fails. Read-only; links open in a
   new tab with rel noopener. Colours come from tokens.css only.
   Needs (plain globals, call time only): el, axes, fmtN, fmtDur, niceMax,
   showTip, hideTip, keyOf, drawMain, secCache, api, state, discloseRows,
   discloseButton, discloseRedraw, readDisclosed. */
(function (global) {
  "use strict";
  var LIN_URL = "/api/linear";
  var LIN_SEC = "linear";
  var LIN_STALE_MS = 30000;
  var NS = "http://www.w3.org/2000/svg";
  var linLoading = false;
  var linAt = 0;
  var linSeq = 0;
  var linData = null;

  var SOURCE = {
    local: {glyph: "■", word: "Local", fill: "var(--muted)"},
    linear: {glyph: "◆", word: "Linear", fill: "var(--s7)"}
  };
  var STATUS_WORD = {todo: "To do", wip: "In progress", pr: "In review", prog: "On prod", done: "Done", deferred: "Deferred"};
  var WINDOWS = [{key: "changes_24h", label: "Last 24 h"}, {key: "changes_7d", label: "Last 7 days"}];

  function svgEl(tag, attrs) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  /* Pure: the workstreams a tab shows. "all" (Overall) shows every linked
     workstream; a workstream id shows only its own row. */
  function linFilter(rows, ws) {
    var all = Array.isArray(rows) ? rows : [];
    if (!ws || ws === "all") return all;
    return all.filter(function (w) { return String(w.ws || "").toUpperCase() === String(ws).toUpperCase(); });
  }

  /* Pure: summed counts by source for one window over the given rows. */
  function linTotals(rows, key) {
    var t = {local: 0, linear: 0};
    rows.forEach(function (w) {
      var c = (w && w[key]) || {};
      t.local += +c.local || 0;
      t.linear += +c.linear || 0;
    });
    return t;
  }

  /* Pure: the y scale for a count chart. niceMax ceilings are 1/2/5 times a
     power of ten, so the tick count is the largest of 5/4/2/1 that divides
     the ceiling evenly: every label is then a whole number of changes. */
  function linScale(max) {
    var yMax = niceMax(max > 0 ? max : 1);
    var prefs = [5, 4, 2, 1];
    for (var i = 0; i < prefs.length; i++) {
      if (Math.abs(yMax / prefs[i] - Math.round(yMax / prefs[i])) < 1e-9) return {yMax: yMax, yTicks: prefs[i]};
    }
    return {yMax: yMax, yTicks: 1};
  }

  /* Chart width in px: the 560 px design width, shrunk on phones so the
     SVG renders at 1:1 and its 11 px axis text keeps its size. */
  function linWidth() {
    var vw = (typeof window !== "undefined" && window.innerWidth) || 560;
    return Math.min(560, Math.max(280, Math.floor(vw - 72)));
  }

  /* Pure: sync state as glyph plus word. Errors win, then queued, then synced. */
  function linSyncState(w) {
    if (w && w.last_error) return {glyph: "⚠", word: "Sync error"};
    if (w && w.outbox > 0) return {glyph: "◔", word: "Queued"};
    return {glyph: "✓", word: "Synced"};
  }

  /* Pure: the sign-in method as a word. Never a token. */
  function linSignIn(method) {
    if (method === "oauth") return "Signed in (browser)";
    if (method === "key") return "Signed in (API key)";
    return "Not signed in";
  }

  function linAgo(ts) {
    var age = (Date.now() / 1000) - (+ts || 0);
    return fmtDur(age) + " ago";
  }

  function linPayload() { return linData; }

  function loadLinear() {
    if (linLoading) return;
    linLoading = true;
    api(LIN_URL, {}).then(function (j) {
      linLoading = false;
      linAt = Date.now();
      linData = (j && typeof j === "object" && Array.isArray(j.workstreams)) ? j : null;
      linSeq++;
      redrawLinear();
    }).catch(function () {
      linLoading = false;
      linAt = Date.now();
      linData = null;
      linSeq++;
      redrawLinear();
    });
  }

  function redrawLinear() {
    try { if (typeof secCache !== "undefined" && secCache) delete secCache[LIN_SEC]; } catch (e) {}
    if (typeof drawMain === "function") drawMain();
  }

  /* Hover and keyboard tooltip on one mark: mouse uses the pointer, focus
     anchors to the mark's box, so both paths show the same rows. */
  function tipOn(node, title, rows) {
    node.addEventListener("mousemove", function (ev) { showTip(ev, title, rows); });
    node.addEventListener("mouseleave", hideTip);
    node.addEventListener("focus", function () {
      var box = node.getBoundingClientRect();
      showTip({clientX: box.left + box.width / 2, clientY: box.top}, title, rows);
    });
    node.addEventListener("blur", hideTip);
  }

  function linChart(rows) {
    var W = linWidth(), H = 220, L = 56, T = 26, R = 16, B = 44;
    var pw = W - L - R, ph = H - T - B, yb = T + ph;
    var data = WINDOWS.map(function (win) {
      var t = linTotals(rows, win.key);
      return {label: win.label, local: t.local, linear: t.linear, total: t.local + t.linear};
    });
    var max = data.reduce(function (m, d) { return Math.max(m, d.total); }, 0);
    var scale = linScale(max);
    var yMax = scale.yMax;
    var slot = pw / data.length;
    var barW = Math.min(90, slot * 0.4);
    var svg = svgEl("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "lin-svg", role: "img"});
    var parts = data.map(function (d) {
      return d.label.toLowerCase() + ": " + fmtN(d.local) + " local, " + fmtN(d.linear) + " from Linear";
    });
    svg.setAttribute("aria-label", "Status changes by source. " + parts.join("; ") + ".");
    axes(svg, {
      L: L, T: T, pw: pw, ph: ph, yMax: yMax, yTicks: scale.yTicks, yFmt: fmtN,
      xTicks: data.map(function (d, i) { return {x: L + slot * i + slot / 2, label: d.label}; }),
      yTitle: "status changes", xTitle: null
    });
    data.forEach(function (d, i) {
      var cx = L + slot * i + slot / 2, base = yb;
      ["local", "linear"].forEach(function (key) {
        var n = d[key];
        var h = ph * n / yMax;
        if (h <= 0) return;
        var mark = svgEl("rect", {
          x: cx - barW / 2, y: base - h, width: barW, height: h, rx: 3,
          fill: SOURCE[key].fill, stroke: "var(--surface)", "stroke-width": 2,
          class: "lin-bar lin-" + key, tabindex: "0", "data-source": key,
          role: "img", "aria-label": d.label + ", " + SOURCE[key].word + ": " + fmtN(n) + " changes"
        });
        tipOn(mark, d.label, [{k: SOURCE[key].glyph + " " + SOURCE[key].word, v: fmtN(n) + " changes", c: SOURCE[key].fill}]);
        svg.appendChild(mark);
        base -= h;
      });
    });
    return svg;
  }

  function linLegend() {
    var list = document.createElement("ul");
    list.className = "lin-legend";
    list.setAttribute("aria-label", "Legend: where a status change came from");
    ["local", "linear"].forEach(function (key) {
      var li = document.createElement("li");
      var sw = document.createElement("i");
      sw.className = "lin-sw lin-" + key;
      sw.setAttribute("aria-hidden", "true");
      li.appendChild(sw);
      var word = document.createElement("span");
      word.textContent = SOURCE[key].glyph + " " + SOURCE[key].word;
      li.appendChild(word);
      list.appendChild(li);
    });
    return list;
  }

  function linLink(url, ws) {
    if (typeof url === "string" && /^https:\/\//.test(url)) {
      var a = el("a", {class: "lin-link", href: url, target: "_blank", rel: "noopener",
        "aria-label": "Open " + ws + " in Linear (opens in a new tab)", text: "Linear ↗"});
      return a;
    }
    return el("span", {class: "lin-link lin-off", text: "Linear"});
  }

  function linWsRow(w) {
    var li = el("li", {class: "lin-ws"});
    var head = el("div", {class: "lin-ws-head"});
    head.appendChild(el("b", {class: "lin-ws-title", text: w.ws + " " + (w.title || "")}));
    head.appendChild(linLink(w.url, w.ws));
    li.appendChild(head);
    var sync = linSyncState(w);
    var meta = el("div", {class: "lin-ws-meta"});
    meta.appendChild(el("span", {text: w.synced_at > 0 ? "Synced " + linAgo(w.synced_at) : "Not synced yet"}));
    meta.appendChild(el("span", {text: linSignIn(linData && linData.signed_in)}));
    meta.appendChild(el("span", {text: fmtN(w.outbox || 0) + " in outbox"}));
    meta.appendChild(el("span", {class: "lin-state", text: sync.glyph + " " + sync.word}));
    li.appendChild(meta);
    return li;
  }

  function linRecentRow(row) {
    var li = el("li", {class: "lin-recent"});
    li.appendChild(el("b", {class: "lin-recent-title", text: row.title || row.task || ""}));
    var from = (Object.prototype.hasOwnProperty.call(STATUS_WORD, row.from) ? STATUS_WORD[row.from] : null) || row.from || "New";
    var to = (Object.prototype.hasOwnProperty.call(STATUS_WORD, row.to) ? STATUS_WORD[row.to] : null) || row.to || "";
    var src = SOURCE[row.by === "linear" ? "linear" : "local"];
    li.appendChild(el("div", {class: "lin-recent-meta", text: from + " → " + to + " · " + src.glyph + " " + src.word + " · " + linAgo(row.ts)}));
    return li;
  }

  function linPendingRow(item, ws) {
    var li = el("li", {class: "lin-pending"});
    li.appendChild(el("span", {class: "lin-pending-step", text: item.step || item.id || ""}));
    if (item.identifier && typeof item.url === "string" && /^https:\/\//.test(item.url)) {
      li.appendChild(el("a", {class: "lin-link", href: item.url, target: "_blank", rel: "noopener",
        "aria-label": "Open decision " + item.identifier + " for " + ws + " in Linear (opens in a new tab)",
        text: item.identifier + " ↗"}));
    } else if (item.identifier) {
      li.appendChild(el("span", {class: "lin-link lin-off", text: item.identifier}));
    }
    return li;
  }

  function linearCard() {
    var rows = linFilter(linPayload() && linPayload().workstreams, state.ws);
    var c = el("div", {class: "card lin-card", id: "exec-linear", "data-nav": "Linear"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Linear"}));
    head.appendChild(el("p", {class: "sub", text: "Status changes by source, sync state, and decisions waiting in Linear."}));
    c.appendChild(head);

    var figure = el("figure", {class: "lin-figure"});
    figure.appendChild(linChart(rows));
    figure.appendChild(linLegend());
    c.appendChild(figure);

    var wsList = el("ul", {class: "lin-wslist", "aria-label": "Linked workstreams"});
    rows.forEach(function (w) { wsList.appendChild(linWsRow(w)); });
    c.appendChild(wsList);

    var recentAll = [];
    rows.forEach(function (w) {
      (w.recent || []).forEach(function (r) { recentAll.push(r); });
    });
    recentAll.sort(function (a, b) { return (+b.ts || 0) - (+a.ts || 0); });
    c.appendChild(el("h3", {class: "lin-h", text: "Recent changes"}));
    if (!recentAll.length) {
      c.appendChild(el("p", {class: "pj-note", text: "No status changes recorded yet."}));
    } else {
      var info = discloseRows(recentAll, "linear-recent", "changes");
      var recentList = el("ol", {class: "lin-recent-list", id: "lin-recent-list", "aria-label": "Recent status changes"});
      info.rows.forEach(function (r) { recentList.appendChild(linRecentRow(r)); });
      c.appendChild(recentList);
      var more = discloseButton(info, "lin-recent-list", discloseRedraw(LIN_SEC, "lin-recent-list"));
      if (more) {
        var acts = el("div", {class: "lin-acts"});
        acts.appendChild(more);
        c.appendChild(acts);
      }
    }

    c.appendChild(el("h3", {class: "lin-h", text: "Waiting on a decision in Linear"}));
    var pending = [];
    rows.forEach(function (w) {
      (w.pending_approvals || []).forEach(function (item) { pending.push({item: item, ws: w.ws}); });
    });
    if (!pending.length) {
      c.appendChild(el("p", {class: "pj-note", text: "No decisions waiting in Linear."}));
    } else {
      var pList = el("ul", {class: "lin-pending-list", "aria-label": "Decisions waiting in Linear"});
      pending.forEach(function (p) { pList.appendChild(linPendingRow(p.item, p.ws)); });
      c.appendChild(pList);
    }
    return c;
  }

  function linearSpec() {
    if (!linLoading && (linAt === 0 || Date.now() - linAt > LIN_STALE_MS)) loadLinear();
    var data = linPayload();
    if (!data || !data.workstreams || !data.workstreams.length) return null;
    if (!linFilter(data.workstreams, state.ws).length) return null;
    return ["linear", keyOf([linSeq, state.ws, readDisclosed("linear-recent"), state.view]), linearCard, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.linear = linearSpec;
  global.linearSpec = linearSpec;
  global.linearCard = linearCard;
  global.linFilter = linFilter;
  global.linTotals = linTotals;
  global.linSyncState = linSyncState;
  global.linSignIn = linSignIn;
  global.linScale = linScale;
})(window);
