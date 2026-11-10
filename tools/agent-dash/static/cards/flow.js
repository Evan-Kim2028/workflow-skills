/* Page card: Execution flow (read-only).
   One compact stacked bar of where the work sits (todo, in progress,
   waiting to merge, on prod, deferred) plus the top blockers, each with
   a copyable next command. Reads GET /api/flow, which folds the cached
   /api/state progress counts and the latest doctor run; the card never
   rescans a plan and never runs a check.
   Registered for the overview and workstream scopes over one spec, so a
   workstream tab shows that workstream's queues and blockers.
   Exports (on window.AdPage.cards, also kept as plain globals):
   flowSpec, flowCard, flowQueues, flowBlockers, flowCommand, flowTakeaway.
   Needs: el, s, axes, fmtN, keyOf, api, state, drawMain, secCache,
   discloseRows, discloseButton, discloseRedraw, readDisclosed, copyCmd,
   showTip, hideTip (plain globals). */
(function (global) {
  "use strict";
  var FLOW_URL = "/api/flow";
  var FLOW_SEC = "flow";
  var FLOW_TTL_MS = 30000;
  /* Task-state colours, the same vocabulary the plan map teaches: not
     started grey, working now blue, waiting to merge amber, on prod
     magenta, deferred violet. Every segment carries its word in the
     table under the bar, so colour is never the only carrier. */
  var FLOW_COLORS = ["var(--muted)", "var(--s1)", "var(--warning)", "var(--s5)", "var(--s7)"];
  var flowLoading = false;
  var flowAt = 0;

  function flowPayload() {
    var f = state.flow;
    return (f && typeof f === "object") ? f : null;
  }

  function flowUnavailable() {
    return {queues: [], blockers: [], workstreams: [], totals: {}, empty: true, unavailable: true};
  }

  function redrawFlow() {
    try {
      if (typeof secCache !== "undefined" && secCache) delete secCache[FLOW_SEC];
    } catch (e) {}
    if (typeof drawMain === "function") drawMain();
  }

  function loadFlow() {
    if (flowLoading) return;
    flowLoading = true;
    api(FLOW_URL, {}).then(function (j) {
      flowLoading = false;
      flowAt = Date.now();
      state.flow = (j && typeof j === "object") ? j : flowUnavailable();
      redrawFlow();
    }).catch(function () {
      flowLoading = false;
      flowAt = Date.now();
      state.flow = flowUnavailable();
      redrawFlow();
    });
  }

  /* Pure: the queue rows for one scope. The overview reads the server's
     project sums; a workstream tab reads that workstream's own counts. */
  function flowQueues(payload, ws) {
    var base = (payload && payload.queues) || [];
    var counts = null;
    if (ws && ws !== "all") {
      var hit = ((payload && payload.workstreams) || []).filter(function (row) { return row && row.id === ws; })[0];
      counts = (hit && hit.counts) || {};
    }
    return base.map(function (row, i) {
      var n = counts ? (+counts[row.key] || 0) : (+row.count || 0);
      return {key: row.key, label: row.label, count: n, color: FLOW_COLORS[i] || "var(--muted)"};
    });
  }

  /* Pure: blockers for one scope, worst first (the server already sorted). */
  function flowBlockers(payload, ws) {
    var rows = (payload && payload.blockers) || [];
    if (ws && ws !== "all") rows = rows.filter(function (row) { return row && row.ws === ws; });
    return rows;
  }

  /* Pure: the command a blocker offers, generated server-side. */
  function flowCommand(blocker) {
    if (!blocker) return "";
    return blocker.next || blocker.command || "";
  }

  /* Pure: the chart's text takeaway, compact numbers only. */
  function flowTakeaway(rows, total) {
    var parts = (rows || []).filter(function (row) { return row.count > 0; }).map(function (row) {
      return fmtN(row.count) + " " + row.label.toLowerCase();
    });
    if (!parts.length) return "No tasks in any queue.";
    return fmtN(total) + " tasks in flight: " + parts.join(", ") + ".";
  }

  function sevWord(sev) {
    if (sev === "critical") return "\u2715 Critical";
    if (sev === "info") return "\u25CB Info";
    return "\u26A0 Warn";
  }

  function flowBar(rows, total) {
    var W = 640, H = 62, L = 8, T = 8, ph = 20, pw = W - L - 8;
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, class: "fl-svg", role: "img", "aria-label": flowTakeaway(rows, total)});
    var x = function (v) { return L + (total > 0 ? pw * v / total : 0); };
    var cum = 0;
    rows.forEach(function (row) {
      if (!(row.count > 0)) return;
      var x0 = x(cum), x1 = x(cum + row.count);
      var rect = s("rect", {x: x0, y: T, width: Math.max(0, x1 - x0 - 2), height: ph, rx: 2, fill: row.color, class: "fl-seg"});
      var tip = function (ev) { showTip(ev, row.label, [{k: "Tasks", v: fmtN(row.count), c: row.color}]); };
      rect.addEventListener("mouseover", tip);
      rect.addEventListener("mousemove", tip);
      rect.addEventListener("mouseout", hideTip);
      svg.appendChild(rect);
      cum += row.count;
    });
    axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: null,
      xTicks: [{x: L, label: "0"}, {x: L + pw / 2, label: fmtN(total / 2)}, {x: L + pw, label: fmtN(total), anchor: "end"}],
      xTitle: "tasks"});
    return svg;
  }

  function flowTable(rows) {
    var t = el("table", {class: "fl-table"});
    var head = el("tr", {});
    head.appendChild(el("th", {text: "Queue"}));
    head.appendChild(el("th", {class: "num", text: "Tasks"}));
    t.appendChild(el("thead", {}, [head]));
    var body = el("tbody", {});
    rows.forEach(function (row) {
      var tr = el("tr", {});
      var td = el("td", {});
      var sw = el("i", {class: "fl-sw"});
      sw.style.background = row.color;
      td.appendChild(sw);
      td.appendChild(document.createTextNode(row.label));
      tr.appendChild(td);
      tr.appendChild(el("td", {class: "num", text: fmtN(row.count)}));
      body.appendChild(tr);
    });
    t.appendChild(body);
    return t;
  }

  function flowBlockerRow(blocker) {
    var li = el("li", {class: "fl-blocker"});
    var sev = blocker.severity || "warn";
    li.appendChild(el("span", {class: "fl-sev " + sev, text: sevWord(sev)}));
    if (blocker.ws) li.appendChild(el("span", {class: "fl-ws", text: blocker.ws}));
    li.appendChild(el("span", {class: "fl-count", text: fmtN(blocker.count) + " \u00D7"}));
    li.appendChild(el("span", {class: "fl-title", text: blocker.title || blocker.code}));
    var cmd = flowCommand(blocker);
    if (cmd) {
      var b = el("button", {type: "button", class: "fl-cmd", title: "Copy this command"});
      b.textContent = cmd;
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (typeof copyCmd === "function") copyCmd(cmd);
      });
      li.appendChild(b);
    }
    return li;
  }

  function flowCard() {
    var payload = flowPayload() || {};
    var ws = state.ws;
    var c = el("div", {class: "card fl-card", id: "exec-flow", "data-nav": "Execution flow"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Execution flow"}));
    head.appendChild(el("p", {class: "sub", text: ws && ws !== "all"
      ? "Where this workstream's tasks sit, and what blocks them."
      : "Where the work sits across every workstream, and what blocks it."}));
    c.appendChild(head);
    if (payload.unavailable) {
      c.appendChild(el("p", {class: "pj-note", text: "Execution flow is unavailable right now. Retry in a moment."}));
      return c;
    }
    if (payload.empty) {
      c.appendChild(el("p", {class: "pj-note", text: "No active workstreams."}));
      return c;
    }
    var rows = flowQueues(payload, ws);
    var total = rows.reduce(function (sum, row) { return sum + row.count; }, 0);
    if (!total) {
      c.appendChild(el("p", {class: "pj-note", text: "No tasks in any queue."}));
    } else {
      c.appendChild(flowBar(rows, total));
      c.appendChild(flowTable(rows));
    }
    c.appendChild(el("h3", {class: "fl-h3", text: "Top blockers"}));
    var blockers = flowBlockers(payload, ws);
    if (!blockers.length) {
      c.appendChild(el("p", {class: "pj-note", text: "No open blockers."}));
      return c;
    }
    var info = discloseRows(blockers, "flow-blockers", "blockers");
    var list = el("ul", {class: "fl-blockers", id: "fl-blockers"});
    info.rows.forEach(function (b) { list.appendChild(flowBlockerRow(b)); });
    c.appendChild(list);
    var more = discloseButton(info, "fl-blockers", discloseRedraw(FLOW_SEC, "fl-blockers"));
    if (more) {
      var acts = el("div", {class: "fl-acts"});
      acts.appendChild(more);
      c.appendChild(acts);
    }
    return c;
  }

  function flowSpec(passedState, snap) {
    var payload = flowPayload();
    if (!payload) {
      if (!flowLoading) loadFlow();
      return null;
    }
    if (!flowLoading && Date.now() - flowAt > FLOW_TTL_MS) loadFlow();
    return ["flow", keyOf([payload, state.ws, readDisclosed("flow-blockers")]), flowCard, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.flow = flowSpec;
  global.flowSpec = flowSpec;
  global.flowCard = flowCard;
  global.flowQueues = flowQueues;
  global.flowBlockers = flowBlockers;
  global.flowCommand = flowCommand;
  global.flowTakeaway = flowTakeaway;
})(window);
