/* Page card: Cross-workstream dependency graph (?view=project + ?view=planned).
   Layered SVG DAG from GET /api/project/graph (../fixtures/graph.json with
   ?fixtures): nodes are workstreams in columns by depth from roots,
   arrows from depends_on, gate signals, and deferred moved_to links. Met
   edges are solid green, waiting dashed muted, stale amber dashed. Hover
   or pin a node to dim the rest and highlight its upstream (amber) and
   downstream (blue) chains, with each edge's condition text in the side
   panel. Archived and dropped nodes hide behind a "show archived" toggle.
   Read-only: one GET fetch with a 30 s cache, never a write.
   Reads: state.view, state.seq (plus its own graph fetch).
   Exports (on window.AdPage.cards, also kept as plain globals):
   depGraphSpec, depLayout, depUpstream, depDownstream, depStateMark,
   depEdgeMark, depShort.
   Needs: el, s, fmtN, showTip, hideTip, keyOf, openPlan, FIX (plain globals). */
(function (global) {
  "use strict";
  var NODE_W = 200, NODE_H = 68, GAP_X = 56, GAP_Y = 14, PAD = 16;
  var NARROW_BELOW = 620;
  var GRAPH_TTL = 30000;
  var ARCH_KEY = "agent-dash-depgraph-arch";
  var depCache = {data: null, at: 0, pending: null, failed: false};
  var depPin = null;
  var depShowArch = false;
  try { depShowArch = localStorage.getItem(ARCH_KEY) === "1"; } catch (e) {}

  /* Pure: short "WS12" sort key (numeric part, then the raw id). */
  function depNum(id) {
    var m = /(\d+)/.exec(String(id || ""));
    return m ? +m[1] : 9999;
  }

  /* Pure: title clipped to one pill line (never a tooltip: the full name
     stays in the hover text and the side panel). */
  function depShort(title, n) {
    var text = String(title == null ? "" : title);
    if (text.length <= n) return text;
    return text.slice(0, Math.max(0, n - 1)) + "\u2026";
  }

  /* Pure: state as glyph plus word (never color alone), with the CSS class
     for its stroke. */
  function depStateMark(state) {
    if (state === "done") return {text: "\u2713 Done", cls: "done"};
    if (state === "planned") return {text: "\u25CB Planned", cls: "planned"};
    if (state === "paused") return {text: "\u275A\u275A Paused", cls: "paused"};
    if (state === "archived") return {text: "\u2713 Archived", cls: "archived"};
    if (state === "dropped") return {text: "\u2715 Dropped", cls: "dropped"};
    return {text: "\u25CF Active", cls: "active"};
  }

  /* Pure: edge status as glyph plus word (never color alone), with the CSS
     class for its stroke. */
  function depEdgeMark(status) {
    if (status === "met") return {text: "\u2713 met", cls: "e-met"};
    if (status === "stale") return {text: "\u26A0 stale", cls: "e-stale"};
    return {text: "\u25CB waiting", cls: "e-waiting"};
  }

  /* Pure: transitive upstream (prerequisites) of one id, excluding itself. */
  function depUpstream(edges, id) {
    var preds = {};
    (edges || []).forEach(function (e) {
      if (!e || e.from == null || e.to == null) return;
      (preds[e.to] = preds[e.to] || {})[e.from] = true;
    });
    var out = {}, stack = Object.keys(preds[id] || {});
    while (stack.length) {
      var next = stack.pop();
      if (next === id || out[next]) continue;
      out[next] = true;
      Object.keys(preds[next] || {}).forEach(function (p) { if (!out[p]) stack.push(p); });
    }
    return out;
  }

  /* Pure: transitive downstream (waiters) of one id, excluding itself. */
  function depDownstream(edges, id) {
    var succs = {};
    (edges || []).forEach(function (e) {
      if (!e || e.from == null || e.to == null) return;
      (succs[e.from] = succs[e.from] || {})[e.to] = true;
    });
    var out = {}, stack = Object.keys(succs[id] || {});
    while (stack.length) {
      var next = stack.pop();
      if (next === id || out[next]) continue;
      out[next] = true;
      Object.keys(succs[next] || {}).forEach(function (p) { if (!out[p]) stack.push(p); });
    }
    return out;
  }

  /* Pure: deterministic layered layout. Columns by node depth from roots;
     no force simulation. Wide mode lays columns left to right; below
     NARROW_BELOW the columns stack as full-width rows, one node per row.
     Every node box is on a disjoint grid slot, so boxes never overlap at
     any node count. Returns {nodes: [{id, x, y, w, h, depth, col}],
     edges: [{..., d}], width, height, cols, narrow}. */
  function depLayout(graph, opts) {
    var width = (opts && opts.width) || 1100;
    var showArch = !!(opts && opts.showArch);
    var nodes = ((graph && graph.nodes) || []).filter(function (n) {
      if (showArch) return true;
      return n.state !== "archived" && n.state !== "dropped";
    });
    var visible = {};
    nodes.forEach(function (n) { visible[n.id] = true; });
    var edges = ((graph && graph.edges) || []).filter(function (e) {
      return e && visible[e.from] && visible[e.to];
    });
    var depths = [];
    nodes.forEach(function (n) { if (depths.indexOf(n.depth || 0) < 0) depths.push(n.depth || 0); });
    depths.sort(function (a, b) { return a - b; });
    if (!depths.length) depths.push(0);
    var cols = depths.map(function (d) {
      return nodes.filter(function (n) { return (n.depth || 0) === d; }).sort(function (a, b) {
        if (depNum(a.id) !== depNum(b.id)) return depNum(a.id) - depNum(b.id);
        return String(a.id) < String(b.id) ? -1 : 1;
      });
    });
    var narrow = width < NARROW_BELOW;
    var placed = [];
    var totalW, totalH;
    if (!narrow) {
      var colH = cols.map(function (c) { return c.length ? c.length * (NODE_H + GAP_Y) - GAP_Y : 0; });
      var peak = Math.max.apply(null, colH.concat([0]));
      totalW = PAD * 2 + cols.length * (NODE_W + GAP_X) - GAP_X;
      totalH = PAD * 2 + peak;
      cols.forEach(function (c, i) {
        var y = PAD + Math.round((peak - colH[i]) / 2);
        c.forEach(function (n) {
          placed.push({id: n.id, x: PAD + i * (NODE_W + GAP_X), y: y, w: NODE_W, h: NODE_H, depth: n.depth || 0, col: i});
          y += NODE_H + GAP_Y;
        });
      });
    } else {
      var full = Math.max(NODE_W, width - PAD * 2);
      var yCur = PAD;
      cols.forEach(function (c, i) {
        c.forEach(function (n) {
          placed.push({id: n.id, x: PAD, y: yCur, w: full, h: NODE_H, depth: n.depth || 0, col: i});
          yCur += NODE_H + GAP_Y;
        });
      });
      totalW = PAD * 2 + full;
      totalH = yCur - GAP_Y + PAD;
    }
    var byId = {};
    placed.forEach(function (p) { byId[p.id] = p; });
    var drawn = edges.map(function (e) {
      var a = byId[e.from], b = byId[e.to];
      var d = "";
      if (a && b) {
        var x1, y1, x2, y2, dx, dy;
        if (!narrow) {
          x1 = a.x + a.w; y1 = a.y + Math.round(a.h / 2);
          x2 = b.x; y2 = b.y + Math.round(b.h / 2);
        } else {
          x1 = a.x + Math.round(a.w / 2); y1 = a.y + a.h;
          x2 = b.x + Math.round(b.w / 2); y2 = b.y;
        }
        dx = x2 - x1; dy = y2 - y1;
        if (Math.abs(dx) >= Math.abs(dy)) {
          var kx = Math.min(90, Math.max(24, Math.round(Math.abs(dx) / 2)));
          d = "M " + x1 + " " + y1 + " C " + (x1 + kx) + " " + y1 + ", " + (x2 - kx) + " " + y2 + ", " + x2 + " " + y2;
        } else {
          var ky = Math.min(90, Math.max(24, Math.round(Math.abs(dy) / 2)));
          d = "M " + x1 + " " + y1 + " C " + x1 + " " + (y1 + ky) + ", " + x2 + " " + (y2 - ky) + ", " + x2 + " " + y2;
        }
      }
      return {from: e.from, to: e.to, kind: e.kind, status: e.status, condition: e.condition, d: d};
    });
    return {nodes: placed, edges: drawn, width: Math.max(totalW, 1), height: Math.max(totalH, 1), cols: cols.length, narrow: narrow};
  }

  /* The graph payload, fetched once per 30 s (fixtures file with
     ?fixtures). A failed scope reads "request failed" instead of zeros;
     an older server without the endpoint reads as unavailable. */
  function depLoad() {
    if (depCache.pending) return;
    if (depCache.data && Date.now() - depCache.at < GRAPH_TTL) return;
    if (depCache.failed && Date.now() - depCache.at < GRAPH_TTL) return;
    var url = (typeof FIX !== "undefined" && FIX) ? "../fixtures/graph.json" : "/api/project/graph";
    depCache.pending = true;
    fetch(url, {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (j) {
      depCache.data = (j && j.nodes) ? j : null;
      depCache.failed = !depCache.data;
      depCache.at = Date.now();
      depCache.pending = null;
      if (global.drawMain && (state.view === "project" || state.view === "planned")) global.drawMain();
    }).catch(function () {
      depCache.data = null;
      depCache.failed = true;
      depCache.pending = null;
      if (global.drawMain && (state.view === "project" || state.view === "planned")) global.drawMain();
    });
  }

  function depTitles() {
    var map = {};
    ((depCache.data && depCache.data.nodes) || []).forEach(function (n) { map[n.id] = n.title || n.id; });
    return map;
  }

  function depName(id, titles) {
    return (titles[id] || id) + " (" + id + ")";
  }

  /* Pure: the task line for a node: counts from the server progress
     object (never recomputed), "all deferred" when the plan holds only
     deferred tasks, "no plan" when no progress object arrived. */
  function depTaskLine(pr) {
    if (pr && pr.total) return fmtN(pr.done || 0) + "/" + fmtN(pr.total) + " (" + fmtN(pr.pct || 0) + "%)";
    if (pr && pr.counts && pr.counts.deferred) return "all deferred";
    return "no plan";
  }

  /* Pure hover-text builder for the chart's own takeaway line. */
  function depAriaTip(n, met, waiting, stale) {
    return "Cross-workstream dependency graph: " + fmtN(n) + " workstreams, " + fmtN(met) + " dependencies met, " + fmtN(waiting) + " waiting, " + fmtN(stale) + " stale";
  }

  function depTipRows(node, upCount, downCount) {
    var pr = node.progress || {};
    var mark = depStateMark(node.state);
    var rows = [
      {k: "State", v: mark.text},
      {k: "Tasks", v: depTaskLine(node.progress)},
      {k: "Live agents", v: node.live_agents ? fmtN(node.live_agents) : "none"}
    ];
    if (node.milestone) rows.push({k: "Milestone", v: node.milestone});
    rows.push({k: "Chains", v: fmtN(upCount) + " upstream \u00B7 " + fmtN(downCount) + " downstream"});
    return rows;
  }

  function depDrawEdges(svg, layout, titles) {
    var g = s("g", {class: "dep-edges"});
    layout.edges.forEach(function (e) {
      var mark = depEdgeMark(e.status);
      var p = s("path", {d: e.d || "", class: "dep-edge " + mark.cls, "data-from": e.from, "data-to": e.to,
        tabindex: "0", role: "img",
        "aria-label": depName(e.from, titles) + " to " + depName(e.to, titles) + ": " + (e.condition || e.kind) + " \u2014 " + mark.text});
      p.addEventListener("mousemove", function (ev) {
        showTip(ev, depName(e.from, titles) + " \u2192 " + depName(e.to, titles), [
          {k: e.kind, v: e.condition || "no condition recorded"},
          {k: "Status", v: mark.text}
        ]);
      });
      p.addEventListener("mouseleave", hideTip);
      p.addEventListener("focus", function (ev) {
        showTip({clientX: 16, clientY: 16}, depName(e.from, titles) + " \u2192 " + depName(e.to, titles), [
          {k: e.kind, v: e.condition || "no condition recorded"},
          {k: "Status", v: mark.text}
        ]);
      });
      p.addEventListener("blur", hideTip);
      g.appendChild(p);
    });
    svg.appendChild(g);
  }

  function depDrawNodes(svg, layout, panel) {
    var byId = {};
    ((depCache.data && depCache.data.nodes) || []).forEach(function (n) { byId[n.id] = n; });
    var g = s("g", {class: "dep-nodes"});
    layout.nodes.forEach(function (p) {
      var node = byId[p.id] || {id: p.id, title: p.id, state: "active", progress: null, live_agents: 0};
      var mark = depStateMark(node.state);
      var up = depUpstream(layout.edges, p.id), down = depDownstream(layout.edges, p.id);
      var upCount = Object.keys(up).length, downCount = Object.keys(down).length;
      var pr = node.progress || {};
      var node_ = s("g", {class: "dep-node st-" + mark.cls, "data-ws": p.id, tabindex: "0", role: "button",
        "aria-label": depName(p.id, depTitles()) + ", " + mark.text + ", " + depTaskLine(node.progress) + ", " +
        (node.live_agents ? node.live_agents + " live agents" : "no live agents")});
      var rect = s("rect", {x: p.x, y: p.y, width: p.w, height: p.h, rx: 12, class: "dep-box"});
      node_.appendChild(rect);
      var title = s("text", {x: p.x + 12, y: p.y + 22, class: "dep-title"});
      title.textContent = depShort(node.title || p.id, 24);
      node_.appendChild(title);
      var st = s("text", {x: p.x + 12, y: p.y + 40, class: "dep-state"});
      st.textContent = mark.text;
      node_.appendChild(st);
      if (pr.total) {
        var bar = s("rect", {x: p.x + 12, y: p.y + 48, width: p.w - 24, height: 5, rx: 2.5, class: "dep-track"});
        node_.appendChild(bar);
        var fill = s("rect", {x: p.x + 12, y: p.y + 48, width: Math.max(0, Math.min(p.w - 24, (p.w - 24) * (pr.pct || 0) / 100)), height: 5, rx: 2.5, class: "dep-fill"});
        node_.appendChild(fill);
      }
      if (node.live_agents) {
        var dot = s("circle", {cx: p.x + p.w - 14, cy: p.y + 14, r: 5, class: "dep-live"});
        node_.appendChild(dot);
      }
      node_.addEventListener("mouseenter", function (ev) {
        depFocus(p.id, svg, panel);
        showTip(ev, depName(p.id, depTitles()), depTipRows(node, upCount, downCount));
      });
      node_.addEventListener("mousemove", function (ev) {
        showTip(ev, depName(p.id, depTitles()), depTipRows(node, upCount, downCount));
      });
      node_.addEventListener("mouseleave", function () {
        hideTip();
        depFocus(depPin, svg, panel);
      });
      node_.addEventListener("click", function () {
        depPin = (depPin === p.id) ? null : p.id;
        depFocus(depPin, svg, panel);
        depPanel(panel, depPin);
      });
      node_.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          depPin = (depPin === p.id) ? null : p.id;
          depFocus(depPin, svg, panel);
          depPanel(panel, depPin);
        } else if (ev.key === "Escape") {
          depPin = null;
          depFocus(null, svg, panel);
          depPanel(panel, null);
        }
      });
      g.appendChild(node_);
    });
    svg.appendChild(g);
  }

  /* Pure-ish DOM effect: dim everything but the focus id and its chains.
     Upstream nodes and edges read amber, downstream blue; a null id
     clears the highlight (falling back to the pin). */
  function depFocus(id, svg, panel) {
    var edges = (depCache.data && depCache.data.edges) || [];
    var keep = {};
    if (id) {
      keep[id] = "self";
      var up = depUpstream(edges, id), down = depDownstream(edges, id);
      Object.keys(up).forEach(function (k) { keep[k] = "up"; });
      Object.keys(down).forEach(function (k) { keep[k] = "down"; });
    }
    Array.prototype.forEach.call(svg.querySelectorAll(".dep-node"), function (node_) {
      var ws = node_.getAttribute("data-ws");
      node_.classList.remove("dim", "hl-up", "hl-down", "hl-self");
      if (!id) return;
      if (!keep[ws]) node_.classList.add("dim");
      else if (keep[ws] !== "self") node_.classList.add(keep[ws] === "up" ? "hl-up" : "hl-down");
      else node_.classList.add("hl-self");
    });
    Array.prototype.forEach.call(svg.querySelectorAll(".dep-edge"), function (path) {
      var a = path.getAttribute("data-from"), b = path.getAttribute("data-to");
      path.classList.remove("dim", "hl-up", "hl-down");
      if (!id) return;
      var inUp = (keep[a] === "up" || a === id) && (keep[b] === "up" || b === id);
      var inDown = (keep[a] === "down" || a === id) && (keep[b] === "down" || b === id);
      if (inUp && !inDown) path.classList.add("hl-up");
      else if (inDown && !inUp) path.classList.add("hl-down");
      else if (!inUp && !inDown) path.classList.add("dim");
    });
  }

  /* Side panel: the focused node's chains with each edge's condition text
     (names first, ids in parentheses) and its met/waiting/stale state. */
  function depPanel(panel, id) {
    panel.innerHTML = "";
    var titles = depTitles();
    var edges = (depCache.data && depCache.data.edges) || [];
    if (!id) {
      panel.appendChild(el("p", {class: "dep-hint", text: "Hover or pin a node to trace its chains: upstream blockers read amber, downstream waiters blue."}));
      return;
    }
    var node = null;
    ((depCache.data && depCache.data.nodes) || []).forEach(function (n) { if (n.id === id) node = n; });
    var mark = depStateMark(node ? node.state : "active");
    panel.appendChild(el("div", {class: "dep-sel", text: depName(id, titles) + " \u2014 " + mark.text}));
    [["up", "Blocked by (upstream)", "dep-up"], ["down", "Blocking (downstream)", "dep-down"]].forEach(function (def_) {
      var dir = def_[0];
      var list = edges.filter(function (e) { return dir === "up" ? e.to === id : e.from === id; })
        .sort(function (a, b) {
          var x = dir === "up" ? a.from : a.to, y = dir === "up" ? b.from : b.to;
          return x < y ? -1 : x > y ? 1 : 0;
        });
      var box = el("div", {class: "dep-chain " + def_[2]});
      box.appendChild(el("h4", {text: def_[1] + " (" + fmtN(list.length) + ")"}));
      if (!list.length) box.appendChild(el("p", {class: "dep-hint", text: dir === "up" ? "Nothing blocks it." : "Nothing waits on it."}));
      list.forEach(function (e) {
        var other = dir === "up" ? e.from : e.to;
        var em = depEdgeMark(e.status);
        var row = el("div", {class: "dep-erow " + em.cls});
        row.appendChild(el("div", {class: "dep-ename", text: depName(other, titles)}));
        row.appendChild(el("div", {class: "dep-econd", text: (e.condition || e.kind) + " \u2014 " + em.text}));
        var acts = el("div", {class: "prog-acts"});
        var bp = el("button", {type: "button", text: "Plan " + other, title: "Open the " + other + " plan page"});
        bp.addEventListener("click", function () { openPlan(other); });
        acts.appendChild(bp);
        row.appendChild(acts);
        box.appendChild(row);
      });
      panel.appendChild(box);
    });
  }

  function depLegend() {
    var lg = el("div", {class: "legend dep-legend"});
    lg.appendChild(el("span", {text: "\u2713 met \u2014 solid green"}));
    lg.appendChild(el("span", {text: "\u25CB waiting \u2014 dashed muted"}));
    lg.appendChild(el("span", {text: "\u26A0 stale \u2014 amber dashed"}));
    lg.appendChild(el("span", {text: "upstream amber \u00B7 downstream blue"}));
    lg.appendChild(el("span", {text: "\u25CF live agents"}));
    return lg;
  }

  function depGraphCard() {
    var c = el("div", {class: "card dep-card", "data-nav": "Dependencies"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Dependencies"}));
    head.appendChild(el("p", {class: "sub", text: "Cross-workstream waits: depends_on, gate signals, and moved tasks."}));
    c.appendChild(head);
    if (!depCache.data) depLoad();
    if (!depCache.data) {
      c.appendChild(el("p", {class: "pj-note", text: depCache.failed ? "Dependency graph unavailable (request failed)." : "Loading the dependency graph\u2026"}));
      return c;
    }
    var graph = depCache.data;
    var bar = el("div", {class: "dep-bar"});
    var toggle = el("button", {type: "button", class: "dep-toggle", "aria-pressed": depShowArch ? "true" : "false",
      title: "Archived and dropped workstreams hide by default; this also shows dropped ones", text: "show archived"});
    toggle.addEventListener("click", function () {
      depShowArch = !depShowArch;
      try { localStorage.setItem(ARCH_KEY, depShowArch ? "1" : "0"); } catch (e) {}
      toggle.setAttribute("aria-pressed", depShowArch ? "true" : "false");
      if (global.drawMain) global.drawMain();
    });
    bar.appendChild(toggle);
    var counts = {met: 0, waiting: 0, stale: 0};
    (graph.edges || []).forEach(function (e) { if (counts[e.status] != null) counts[e.status]++; });
    bar.appendChild(el("span", {class: "dep-counts", text: fmtN((graph.nodes || []).length) + " workstreams \u00B7 " + fmtN(counts.met) + " met \u00B7 " + fmtN(counts.waiting) + " waiting \u00B7 " + fmtN(counts.stale) + " stale"}));
    c.appendChild(bar);
    var host = document.getElementById("main");
    var w = (host && host.clientWidth) || 1100;
    var layout = depLayout(graph, {width: Math.min(w, 1400), showArch: depShowArch});
    var body = el("div", {class: "dep-body"});
    var svg = s("svg", {viewBox: "0 0 " + layout.width + " " + layout.height, class: "dep-svg",
      role: "img", "aria-label": depAriaTip((graph.nodes || []).length, counts.met, counts.waiting, counts.stale), "data-diagram": "depgraph"});
    svg.style.width = "100%";
    svg.style.height = "auto";
    /* A nearly empty graph (a new dashboard with one workstream) keeps its
       natural node size instead of scaling one node to the full width. */
    svg.style.maxWidth = layout.width + "px";
    var panel = el("aside", {class: "dep-panel", "aria-live": "polite"});
    depDrawEdges(svg, layout, depTitles());
    depDrawNodes(svg, layout, panel);
    depFocus(depPin, svg, panel);
    body.appendChild(svg);
    body.appendChild(panel);
    c.appendChild(body);
    depPanel(panel, depPin);
    c.appendChild(depLegend());
    if ((graph.cycles || []).length) {
      var names = depTitles();
      var line = graph.cycles.map(function (cy) {
        return cy.map(function (id) { return (names[id] || id) + " (" + id + ")"; }).join(" \u2192 ");
      }).join("; ");
      c.appendChild(el("p", {class: "dep-cycles", text: "Cycle" + (graph.cycles.length === 1 ? "" : "s") + ": " + line}));
    }
    return c;
  }

  /* Registry spec shared by the Project home and Planned slots
     (exec-depgraph on both views). */
  function depGraphSpec(passedState, snap) {
    var host = (typeof document !== "undefined" && document.getElementById("main")) || null;
    var w = (host && host.clientWidth) || 1100;
    return ["depgraph", keyOf([state.seq,
      depCache.data ? depCache.data.generated_at : (depCache.failed ? "failed" : "loading"),
      depShowArch, depPin, Math.floor(w / 120)]), function () { return depGraphCard(); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.depgraph = depGraphSpec;
  global.depGraphSpec = depGraphSpec;
  global.depLayout = depLayout;
  global.depUpstream = depUpstream;
  global.depDownstream = depDownstream;
  global.depStateMark = depStateMark;
  global.depEdgeMark = depEdgeMark;
  global.depShort = depShort;
  global.depTaskLine = depTaskLine;
  global.depTipRows = depTipRows;
  global.depAriaTip = depAriaTip;
  AdPage.cards.depTaskLine = depTaskLine;
  AdPage.cards.depTipRows = depTipRows;
  AdPage.cards.depAriaTip = depAriaTip;
})(window);
