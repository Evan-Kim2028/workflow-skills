/* Page card: running and finished run tables.
   Renders the Running now and Finished (24 h) tables plus the row-key
   and truncation-subtitle helpers drawMain's registry entries use. Status
   is colour plus symbol plus word, verdicts are glyph-plus-word pills, the
   model cell carries the family swatch, numbers are tabular, rows hover,
   focus by keyboard, tooltip exact values, and open the drawer on
   click or Enter. Running now keeps the full count in its header
   ("Running now · N") and groups rows workstream -> section -> label:
   one workstream header per workstream on the overview (collapsible with
   a chevron), one section header per plan section (name, agent count,
   and the section's tasks-done progress when the plan is at hand), agent
   rows newest-first underneath, sections in plan order with Unsectioned
   last. Collapsed it shows the top 5 agents most-recently-active-first
   (freshest section on top) with their headers; "Show all N running
   agents" expands to the plan-ordered full list; headers with zero
   visible agents stay hidden while collapsed. A small Group by toggle
   (Section | None) sits above each table, default Section, persisted per
   scope in sessionStorage behind try/catch; None renders a flat table.
   Toggle, disclosure and chevron clicks rebuild the card in place (no
   drawMain), and every render reads the persisted state, so ticks and
   re-renders never collapse anything and nothing below reflows.
   Reads: nothing at load; rows are passed in (state.ws for the scope key,
   state.plan / state.plans for section order and progress).
   Exports (on window.AdPage.cards, also kept as plain globals):
   runsTable, statusEl (also read by static/core/drawer.js), verdictPill,
   runRow, finishedSub, RUNNING_TOP_N, runningOrder, runningVisible,
   runningToggleLabel, groupRuns, visibleGroups (plus the sessionStorage
   scope helpers on cards).
   Needs: el, tierKey, modelName, fullName, colorOf, tokCell, tokTotal,
   openRun, showTip, hideTip, fmtN, fmtDur (plain globals). */
(function (global) {
  "use strict";
  /* Status is colour plus symbol plus word, never colour alone: the dot
     carries the colour, the glyph carries the shape, the word names the
     state. Glyphs match the Where-we-are pills and the timeline key. */
  function statusSym(st) {
    if (st === "running") return "\u25CF";
    if (st === "done" || st === "finished") return "\u2713";
    if (st === "killed" || st === "died_early") return "\u2715";
    return "\u25CB";
  }
  function statusEl(r) {
    var st = r.status || "unknown";
    var word = st === "died_early" ? "died early" : st;
    return el("span", {class: "st " + st, title: "status: " + word}, [el("i"), statusSym(st) + " " + word]);
  }

  /* Verdict pills: colour plus glyph plus word. The raw verdict stays in
     the title so nothing exact is lost. */
  function verdictPill(v) {
    if (v === "PASS" || v === "APPROVE" || v === "APPROVE_WITH_NITS") return ["pass", "\u2713 pass"];
    if (v === "PARTIAL") return ["part", "\u25D0 partial"];
    if (v === "REQUEST_CHANGES") return ["rev", "\u2715 changes asked"];
    if (v === "BLOCKED") return ["rev", "\u2715 blocked"];
    return ["none", "\u2014 no verdict"];
  }
  function verdictEl(v) {
    var p = verdictPill(v);
    return el("span", {class: "vpill " + p[0], title: v == null ? "no verdict recorded" : String(v), text: p[1]});
  }

  /* Exact-value tooltip rows for a run row: full token digits plus the
     compact cell text's inputs. */
  function tipRows(r) {
    var t = r.tokens || {};
    return [
      {k: "Model", v: fullName(tierKey(r))},
      {k: "Elapsed", v: fmtDur(r.elapsed_s)},
      {k: "Tokens", v: String(tokTotal(t))},
      {k: "Status", v: (r.status || "unknown") + (r.verdict ? " \u00B7 " + r.verdict : "")}
    ];
  }

  /* Running now shows the top agents by default with a show-all
     disclosure (owner ask). Most recently active first: rows carry no
     finer activity stamp than started_at (the server already sends them
     newest-first), so this is a stable sort by started_at descending that
     keeps the incoming order on ties. */
  var RUNNING_TOP_N = 5;
  function newestFirst(rows) {
    return (rows || []).map(function (r, i) { return {r: r, i: i}; }).sort(function (a, b) {
      var d = (b.r.started_at || 0) - (a.r.started_at || 0);
      return d || (a.i - b.i);
    }).map(function (x) { return x.r; });
  }
  function runningOrder(runs) {
    return newestFirst(runs);
  }
  /* The visible slice: the top agents collapsed, every agent expanded. */
  function runningVisible(runs, showAll) {
    var ordered = runningOrder(runs);
    return showAll ? ordered : ordered.slice(0, RUNNING_TOP_N);
  }
  /* The disclosure button names agents, never a bare count: "Show all 12
     running agents" collapsed, "Show top 5 agents" expanded. */
  function runningToggleLabel(total, showAll) {
    return showAll
      ? "Show top " + RUNNING_TOP_N + " agents"
      : "Show all " + total + " running agents";
  }
  /* Expanded state persists for the session, per scope (all vs one
     workstream), default collapsed. sessionStorage may throw (private
     mode), so every touch is guarded. */
  function runningScopeKey(scope) { return "agent-dash-running:" + (scope || "all"); }
  function readRunningExpanded(scope) {
    try {
      return window.sessionStorage.getItem(runningScopeKey(scope)) === "all";
    } catch (e) { return false; }
  }
  function writeRunningExpanded(scope, showAll) {
    try {
      if (showAll) window.sessionStorage.setItem(runningScopeKey(scope), "all");
      else window.sessionStorage.removeItem(runningScopeKey(scope));
    } catch (e) {}
  }

  /* Group-by toggle state (Section | None), per list and scope, default
     Section. Same try/catch guard: private mode never breaks the render. */
  function groupScopeKey(list, scope) { return "agent-dash-group:" + list + ":" + (scope || "all"); }
  function readGroupMode(list, scope) {
    try {
      return window.sessionStorage.getItem(groupScopeKey(list, scope)) === "none" ? "none" : "section";
    } catch (e) { return "section"; }
  }
  function writeGroupMode(list, scope, mode) {
    try {
      if (mode === "none") window.sessionStorage.setItem(groupScopeKey(list, scope), "none");
      else window.sessionStorage.removeItem(groupScopeKey(list, scope));
    } catch (e) {}
  }
  /* Workstream chevron state on the overview (collapsed ids), per list
     and scope, default expanded. Guarded like every other touch. */
  function wsCollapsedKey(list, scope) { return "agent-dash-wscollapsed:" + list + ":" + (scope || "all"); }
  function readWsCollapsed(list, scope) {
    try {
      var raw = window.sessionStorage.getItem(wsCollapsedKey(list, scope));
      if (!raw) return {};
      var out = {};
      String(raw).split(",").forEach(function (w) { if (w) out[w] = 1; });
      return out;
    } catch (e) { return {}; }
  }
  function writeWsCollapsed(list, scope, map) {
    try {
      var ids = Object.keys(map || {}).filter(function (w) { return map[w]; });
      if (ids.length) window.sessionStorage.setItem(wsCollapsedKey(list, scope), ids.join(","));
      else window.sessionStorage.removeItem(wsCollapsedKey(list, scope));
    } catch (e) {}
  }

  /* Pure: group run rows workstream -> section -> rows.
     rows arrive newest-first; each may carry the server section fields
     (section name, section_index in plan order; null when unmapped).
     planSections orders the sections: {WS: [names]} on the overview, a
     plain [names] array on a workstream tab, or null to derive the order
     from the server section_index values (already plan order).
     mode "section" groups by section; mode "none" returns one flat
     section per workstream (name null: the renderer draws no header).
     Returns [{ws, sections: [{name, index, count, rows}]}]: workstreams
     freshest-first, sections in plan order with Unsectioned last, rows
     newest-first inside a section, count the agents in it. */
  function planOrderFor(ws, planSections) {
    if (Array.isArray(planSections)) return planSections;
    if (planSections && typeof planSections === "object" && Array.isArray(planSections[ws])) return planSections[ws];
    return null;
  }
  function groupRuns(rows, planSections, mode) {
    var flat = mode === "none";
    var sorted = newestFirst(rows);
    var byWs = {}, wsOrder = [];
    sorted.forEach(function (r) {
      var ws = (r && r.workstream) || "Unassigned";
      if (!byWs[ws]) { byWs[ws] = []; wsOrder.push(ws); }
      byWs[ws].push(r);
    });
    /* Freshest workstream first: each list is newest-first, so the head
       decides. Stable on ties (first-seen wins). */
    wsOrder.sort(function (a, b) {
      var d = ((byWs[b][0] && byWs[b][0].started_at) || 0) - ((byWs[a][0] && byWs[a][0].started_at) || 0);
      return d;
    });
    return wsOrder.map(function (ws) {
      var list = byWs[ws];
      if (flat) return {ws: ws, sections: [{name: null, index: -1, count: list.length, rows: list}]};
      var want = planOrderFor(ws, planSections);
      var buckets = {}, seen = [];
      list.forEach(function (r) {
        var name = (r && typeof r.section === "string" && r.section) ? r.section : null;
        var key = name || "";
        if (!buckets[key]) {
          buckets[key] = {name: name, index: null, rows: [], first: seen.length};
          seen.push(key);
        }
        buckets[key].rows.push(r);
        if (name && buckets[key].index == null && r && typeof r.section_index === "number") buckets[key].index = r.section_index;
      });
      var secs = seen.map(function (k) { return buckets[k]; });
      function rank(s) {
        if (s.name == null) return [1e9, 0, s.first];
        if (want) {
          var p = want.indexOf(s.name);
          if (p >= 0) return [p, 0, s.first];
        }
        if (s.index != null) return [1e6 + s.index, 0, s.first];
        return [2e6, 0, s.first];
      }
      secs.sort(function (a, b) {
        var ra = rank(a), rb = rank(b);
        return (ra[0] - rb[0]) || (ra[1] - rb[1]) || (ra[2] - rb[2]);
      });
      return {
        ws: ws,
        sections: secs.map(function (s) { return {name: s.name, index: s.index, count: s.rows.length, rows: s.rows}; })
      };
    });
  }
  /* Pure: the collapsed slice across grouped lists. Expanded returns the
     groups untouched (plan order); collapsed keeps the first n agents
     most-recently-active-first, regrouped so the freshest section (and
     the freshest workstream) tops the table. Headers with zero visible
     agents never survive: only groups holding a kept agent are returned. */
  function visibleGroups(groups, showAll, n) {
    n = n || RUNNING_TOP_N;
    if (showAll) return groups;
    var items = [], seq = 0;
    (groups || []).forEach(function (g) {
      (g.sections || []).forEach(function (s) {
        (s.rows || []).forEach(function (r) { items.push({ws: g.ws, name: s.name, index: s.index, r: r, i: seq++}); });
      });
    });
    items.sort(function (a, b) { return ((b.r.started_at || 0) - (a.r.started_at || 0)) || (a.i - b.i); });
    var kept = items.slice(0, n);
    var out = [], byWs = {};
    kept.forEach(function (it) {
      var g = byWs[it.ws];
      if (!g) { g = byWs[it.ws] = {ws: it.ws, sections: [], bySec: {}}; out.push(g); }
      var key = it.name || "";
      var s = g.bySec[key];
      if (!s) { s = g.bySec[key] = {name: it.name, index: it.index, count: 0, rows: []}; g.sections.push(s); }
      s.rows.push(it.r);
      s.count = s.rows.length;
    });
    out.forEach(function (g) { delete g.bySec; });
    return out;
  }

  /* Section titles in plan order for a scope: the workstream tab reads
     its plan body; the overview reads whatever plan bodies are at hand
     (project view preloads them) and groupRuns derives the rest from the
     server section_index. Sections the plan does not name still order by
     index; Unsectioned always trails. */
  function planSectionsFor(scope) {
    try {
      if (scope && scope !== "all") {
        var p = (typeof state !== "undefined" && state && state.plan) || null;
        if (p && p.ws === scope && Array.isArray(p.groups)) {
          return p.groups.map(function (g) { return (g && (g.title || g.id)) || ""; }).filter(Boolean);
        }
        return null;
      }
      var plans = (typeof state !== "undefined" && state && state.plans) || null;
      if (!plans) return null;
      var out = {}, any = false;
      Object.keys(plans).forEach(function (ws) {
        var body = plans[ws];
        if (body && Array.isArray(body.groups)) {
          out[ws] = body.groups.map(function (g) { return (g && (g.title || g.id)) || ""; }).filter(Boolean);
          any = true;
        }
      });
      return any ? out : null;
    } catch (e) { return null; }
  }
  /* Tasks-done progress for a section header, when the plan body is at
     hand (workstream tab always; overview only for preloaded plans).
     Null when unknown: the header then shows the agent count alone. */
  function sectionProgress(ws, name) {
    try {
      var body = null;
      if (typeof state !== "undefined" && state) {
        if (state.plan && state.plan.ws === ws) body = state.plan;
        else if (state.plans && state.plans[ws]) body = state.plans[ws];
      }
      if (!body || !Array.isArray(body.groups)) return null;
      for (var i = 0; i < body.groups.length; i++) {
        var g = body.groups[i] || {};
        if ((g.title || g.id) !== name) continue;
        var done = 0, total = 0;
        ((g.tasks) || []).forEach(function (t) {
          var st = (t && t.status) || "todo";
          if (st === "deferred") return;
          total++;
          if (st === "done") done++;
        });
        return {done: done, total: total};
      }
      return null;
    } catch (e) { return null; }
  }

  /* One agent row, label first, then model, elapsed, output, tokens,
     status. The workstream cell renders only in flat overview tables,
     where no workstream header names it. */
  function agentRowEl(r, showWs) {
    var k = tierKey(r);
    var tr = el("tr", {class: "clk", tabindex: "0",
      onclick: function () { openRun(r); },
      onkeydown: function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openRun(r); } },
      onmousemove: function (ev) { showTip(ev, r.label || r.run_id, tipRows(r)); },
      onmouseleave: function () { hideTip(); }});
    tr.appendChild(el("td", null, [el("span", {class: "lbl", text: r.label || r.run_id})]));
    var tier = el("span", {class: "tier", title: r.model || ""}); var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k); tier.appendChild(sw); tier.appendChild(el("span", {class: "mfam-n", text: ADM.FAM[ADM.famOf(k)].name})); tier.appendChild(el("span", {class: "mmod-n", text: modelName(k)}));
    tr.appendChild(el("td", null, [tier]));
    if (showWs) tr.appendChild(el("td", null, [el("span", {class: "ws", text: r.workstream || "–"})]));
    tr.appendChild(el("td", {class: "n hidephone", text: fmtDur(r.elapsed_s)}));
    var ob = el("td", {class: "n hidephone", text: fmtN(r.out_bytes) + "B"}); if (r.out_growing) ob.appendChild(el("span", {class: "grow", text: "▲"})); tr.appendChild(ob);
    tr.appendChild(tokCell(r));
    tr.appendChild(el("td", null, [statusEl(r), r.verdict ? verdictEl(r.verdict) : null]));
    return tr;
  }
  /* A finished row keeps the historic Model-first columns plus the new
     Section column after WS: the tab names the section, the overview
     names "Workstream / Section". Unmapped rows read Unsectioned. */
  function sectionCell(r, scope) {
    var sec = (r && typeof r.section === "string" && r.section) ? r.section : "Unsectioned";
    var text = (scope === "all") ? ((r.workstream || "–") + " / " + sec) : sec;
    return el("td", null, [el("span", {class: "sec", text: text})]);
  }
  function finishedRowEl(r, scope) {
    var k = tierKey(r);
    var tr = el("tr", {class: "clk", tabindex: "0",
      onclick: function () { openRun(r); },
      onkeydown: function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openRun(r); } },
      onmousemove: function (ev) { showTip(ev, r.label || r.run_id, tipRows(r)); },
      onmouseleave: function () { hideTip(); }});
    var tier = el("span", {class: "tier", title: r.model || ""}); var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k); tier.appendChild(sw); tier.appendChild(el("span", {class: "mfam-n", text: ADM.FAM[ADM.famOf(k)].name})); tier.appendChild(el("span", {class: "mmod-n", text: modelName(k)}));
    tr.appendChild(el("td", null, [tier]));
    tr.appendChild(el("td", null, [el("span", {class: "lbl", text: r.label || r.run_id})]));
    tr.appendChild(el("td", null, [el("span", {class: "ws", text: r.workstream || "–"})]));
    tr.appendChild(sectionCell(r, scope));
    tr.appendChild(el("td", {class: "n hidephone", text: fmtDur(r.elapsed_s)}));
    var ob = el("td", {class: "n hidephone", text: fmtN(r.out_bytes) + "B"}); if (r.out_growing) ob.appendChild(el("span", {class: "grow", text: "▲"})); tr.appendChild(ob);
    tr.appendChild(tokCell(r));
    tr.appendChild(el("td", null, [statusEl(r), r.verdict ? verdictEl(r.verdict) : null]));
    return tr;
  }
  /* The small Group by toggle above a table: two real buttons with
     aria-pressed, Section default. Clicking persists and rebuilds the
     card in place (no drawMain, no flicker). */
  function groupToggle(list, scope, mode, rebuild) {
    var bar = el("div", {class: "grp-toggle", role: "group", "aria-label": "Group agents by"});
    bar.appendChild(el("span", {class: "grp-cap", text: "Group by"}));
    [["section", "Section"], ["none", "None"]].forEach(function (pair) {
      var b = el("button", {type: "button", class: "grp-btn", "aria-pressed": mode === pair[0] ? "true" : "false", text: pair[1]});
      b.addEventListener("click", function () {
        if (readGroupMode(list, scope) === pair[0]) return;
        writeGroupMode(list, scope, pair[0]);
        rebuild();
      });
      bar.appendChild(b);
    });
    return bar;
  }
  function sectionHead(name, count, ws, span) {
    var label = (name == null ? "Unsectioned" : name) + " \u00B7 " + count + " agent" + (count === 1 ? "" : "s");
    if (name != null) {
      var prog = sectionProgress(ws, name);
      if (prog) label += " \u00B7 " + prog.done + " of " + prog.total + " done";
    }
    var tr = el("tr", {class: "sec-h"});
    var td = el("td", {colspan: String(span)});
    td.appendChild(el("span", {class: "sec-n", text: label}));
    tr.appendChild(td);
    return tr;
  }
  function wsHead(ws, count, collapsed, span, onToggle) {
    var tr = el("tr", {class: "ws-h"});
    var td = el("td", {colspan: String(span)});
    var b = el("button", {type: "button", class: "ws-chev", "aria-expanded": collapsed ? "false" : "true", "aria-label": (collapsed ? "Expand " : "Collapse ") + ws + " agents"});
    b.appendChild(el("span", {class: "chev", "aria-hidden": "true", text: collapsed ? "\u25B8" : "\u25BE"}));
    b.appendChild(el("span", {class: "ws-n", text: ws + " \u00B7 " + count + " agent" + (count === 1 ? "" : "s")}));
    b.addEventListener("click", function () { onToggle(ws); });
    td.appendChild(b);
    tr.appendChild(td);
    return tr;
  }

  function runsTable(title, sub, runs, empty, pager) {
    var attrs = {class: "card", "data-nav": title};
    if (title === "Running now") attrs.id = "exec-running";
    else if (title === "Finished (24 h)") { attrs.id = "exec-finished"; attrs["data-nav"] = "Finished (24 h)"; }
    var c = el("div", attrs);
    var scope = (typeof state !== "undefined" && state && state.ws) || "all";
    if (title === "Running now") buildRunning(c, title, sub, runs || [], empty, scope);
    else buildFinished(c, title, sub, runs || [], empty, pager, scope);
    return c;
  }
  function runningHead(showWs) {
    var cols = ["Label", "Model"];
    if (showWs) cols.push("WS");
    cols.push("Elapsed", "Output", "Tokens", "Status");
    return el("thead", null, [el("tr", null, cols.map(function (h, i) {
      var right = h === "Elapsed" || h === "Output" || h === "Tokens";
      return el("th", {scope: "col", class: right ? "hidephone" : "", style: right ? "text-align:right" : ""}, [h]);
    }))]);
  }
  /* Running now, grouped workstream -> section -> label. The card rebuilds
     itself in place on every toggle (disclosure, group-by, chevron), and
     every full render reads the persisted state, so ticks never collapse
     anything. */
  function buildRunning(c, title, sub, runs, empty, scope) {
    function build() {
      c.innerHTML = "";
      var heading = title + " \u00B7 " + runs.length;
      c.appendChild(el("h2", {text: heading}));
      c.appendChild(el("p", {class: "sub", text: sub}));
      if (!runs.length) { c.appendChild(el("div", {class: "empty", text: empty})); return; }
      var mode = readGroupMode("running", scope);
      c.appendChild(groupToggle("running", scope, mode, build));
      var ordered = runningOrder(runs);
      var groups = groupRuns(ordered, planSectionsFor(scope), mode);
      var showAll = readRunningExpanded(scope);
      var vis = (runs.length > RUNNING_TOP_N) ? visibleGroups(groups, showAll, RUNNING_TOP_N) : groups;
      var collapsed = readWsCollapsed("running", scope);
      var flat = mode === "none";
      var showWs = flat && scope === "all";
      var span = showWs ? 7 : 6;
      var t = el("table", {"aria-label": title}, [runningHead(showWs)]);
      var tb = el("tbody", {id: "exec-running-rows"});
      vis.forEach(function (g) {
        var gCount = g.sections.reduce(function (a, s) { return a + s.count; }, 0);
        var isCollapsed = scope === "all" && !!collapsed[g.ws];
        if (scope === "all") {
          tb.appendChild(wsHead(g.ws, gCount, isCollapsed, span, function (ws) {
            var cur = readWsCollapsed("running", scope);
            if (cur[ws]) delete cur[ws];
            else cur[ws] = 1;
            writeWsCollapsed("running", scope, cur);
            build();
          }));
        }
        if (isCollapsed) return;
        g.sections.forEach(function (s) {
          if (!flat) tb.appendChild(sectionHead(s.name, s.count, g.ws, span));
          s.rows.forEach(function (r) { tb.appendChild(agentRowEl(r, showWs)); });
        });
      });
      t.appendChild(tb);
      c.appendChild(el("div", {class: "twrap"}, [t]));
      if (runs.length > RUNNING_TOP_N) {
        /* Progressive disclosure below the table: a real button that
           rebuilds the card in place between the top 5 and everything.
           No drawMain, so ticks and re-renders never collapse it and
           nothing below reflows. */
        var moreBtn = el("button", {type: "button", class: "runmore", "aria-expanded": showAll ? "true" : "false", "aria-controls": "exec-running-rows", text: runningToggleLabel(runs.length, showAll)});
        moreBtn.addEventListener("click", function () {
          writeRunningExpanded(scope, !readRunningExpanded(scope));
          build();
        });
        c.appendChild(moreBtn);
      }
    }
    build();
  }
  /* Finished agents, one page at a time: a Section column plus the same
     Group by toggle, headers inside the page only (never reordered
     across pages). The shared pager footer is untouched. */
  function buildFinished(c, title, sub, rows, empty, pager, scope) {
    function build() {
      c.innerHTML = "";
      c.appendChild(el("h2", {text: title}));
      c.appendChild(el("p", {class: "sub", text: sub}));
      if (!rows.length) { c.appendChild(el("div", {class: "empty", text: empty})); return; }
      var mode = readGroupMode("finished", scope);
      c.appendChild(groupToggle("finished", scope, mode, build));
      var groups = groupRuns(newestFirst(rows), planSectionsFor(scope), mode);
      var flat = mode === "none";
      var t = el("table", {"aria-label": title}, [el("thead", null, [el("tr", null, ["Model", "Label", "WS", "Section", "Elapsed", "Output", "Tokens", "Status"].map(function (h) {
        var right = h === "Elapsed" || h === "Output" || h === "Tokens";
        return el("th", {scope: "col", class: right ? "hidephone" : "", style: right ? "text-align:right" : ""}, [h]);
      }))])]);
      var tb = el("tbody");
      groups.forEach(function (g) {
        if (scope === "all" && groups.length > 1) {
          var gCount = g.sections.reduce(function (a, s) { return a + s.count; }, 0);
          var wtr = el("tr", {class: "ws-h"});
          var wtd = el("td", {colspan: "8"});
          wtd.appendChild(el("span", {class: "ws-n", text: g.ws + " \u00B7 " + gCount + " agent" + (gCount === 1 ? "" : "s")}));
          wtr.appendChild(wtd);
          tb.appendChild(wtr);
        }
        g.sections.forEach(function (s) {
          if (!flat) tb.appendChild(sectionHead(s.name, s.count, g.ws, 8));
          s.rows.forEach(function (r) { tb.appendChild(finishedRowEl(r, scope)); });
        });
      });
      t.appendChild(tb);
      c.appendChild(el("div", {class: "twrap"}, [t]));
      /* The finished tables page through the shared pager (Execution overall
         plus each workstream tab); Running now keeps its own disclosure. */
      if (pager) c.appendChild(pager);
    }
    build();
  }
  var runRow = function (r) { return [r.run_id, r.status, r.verdict, r.out_bytes, r.tokens, r.context_tokens, Math.floor((r.elapsed_s || 0) / 15)]; };
  /* Finished subtitle names the truncation: "Newest 50 of 1,452" when the
     window holds more finished runs than the table shows, so the hidden gap
     is visible instead of silently omitted. */
  function finishedSub(snap) {
    var shown = (snap && snap.recent_shown != null) ? snap.recent_shown : (snap.recent || []).length;
    var total = (snap.counts && snap.counts.finished_in_range != null)
      ? snap.counts.finished_in_range
      : (snap.recent_total != null ? snap.recent_total : shown);
    if (total > shown) return "Newest " + shown + " of " + total + " \u00B7 Newest first, ended in the last 24 h.";
    return "Newest first, ended in the last 24 h.";
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.runsTable = runsTable;
  AdPage.cards.finishedSub = finishedSub;
  AdPage.cards.RUNNING_TOP_N = RUNNING_TOP_N;
  AdPage.cards.runningOrder = runningOrder;
  AdPage.cards.runningVisible = runningVisible;
  AdPage.cards.runningToggleLabel = runningToggleLabel;
  AdPage.cards.runningScopeKey = runningScopeKey;
  AdPage.cards.readRunningExpanded = readRunningExpanded;
  AdPage.cards.writeRunningExpanded = writeRunningExpanded;
  AdPage.cards.groupRuns = groupRuns;
  AdPage.cards.visibleGroups = visibleGroups;
  AdPage.cards.readGroupMode = readGroupMode;
  AdPage.cards.writeGroupMode = writeGroupMode;
  AdPage.cards.newestFirst = newestFirst;
  global.runsTable = runsTable;
  global.statusEl = statusEl;
  global.verdictPill = verdictPill;
  global.runRow = runRow;
  global.finishedSub = finishedSub;
  global.RUNNING_TOP_N = RUNNING_TOP_N;
  global.runningOrder = runningOrder;
  global.runningVisible = runningVisible;
  global.runningToggleLabel = runningToggleLabel;
  global.groupRuns = groupRuns;
  global.visibleGroups = visibleGroups;
})(window);
