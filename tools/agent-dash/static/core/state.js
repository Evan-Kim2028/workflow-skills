/* Page core: page state, route, and tab plumbing (not card-specific).
   Exports (on window.AdPage, also kept as plain globals): WINDOW_HOURS and
   state (the one page state object, mutated in place, never reassigned);
   isClipMode, isClipVertical (clip query flags); wsNum, shown
   (workstream-list helpers); canonWs, readRoute, planIdFor, queryWs,
   pushRoute, applyChrome, scrollHash (route read/write and shell chrome);
   isProjectView, projectLanding (project-view helpers); selectTab,
   selectView, showCurrent, openPlan, openApprovals (view switching);
   tabFilter, tabDrag, tabDragPending, tabClickGuard,
   currentOrder, onTabDragStart, onTabDragOver, finishTabDrag, onTabDragEnd,
   onTabKey, filterCtl, renderTabs, viewCounts, updateViewNav (the All |
   Active | Completed filter and drag order, remembered per viewer, plus
   the Project | Current | Planned | Archive nav pills); planUrl, go,
   since, wsParam.
   Needs: window.ADT (static/tabs.js), window.ADC (static/tokencache.js), the
   globals the exec cache and refresh files keep (execCache helpers, api),
   and progressFor + drawMain, which stay in the page script and are read as
   plain globals at call time. */
(function (global) {
  "use strict";
  /* Clip mode frames the plan progress map and the run timeline as one promo
     composition: page chrome hides and overlays carry the story. */
  function isClipMode() { return Q.has("clip"); }
  function isClipVertical() { var v = (Q.get("clip") || "").toLowerCase(); return v === "4x5" || v === "v" || v === "vertical"; }
  var WINDOW_HOURS = 24;
  var state = {view: "exec", ws: "all", snap: null, runs: [], usage: [], conc: [], wss: [], wssAll: [], bundles: [], plan: null, plans: {}, project: null, archive: null, landed: false, seq: -1, viewSeq: -1, groupBy: "model", approvals: [], snapError: null, lastSeqChange: 0, lastPoll: null, lastDataTs: null};
  var VIEWS = ["plan", "exec", "project", "planned", "archive"];
  /* Project views (Project home, Planned, Archive) render from the same
     all-scope data beside the exec tabs; "current" is the exec alias. */
  function isProjectView() { return state.view === "project" || state.view === "planned" || state.view === "archive"; }
  /* Default landing: Project home when /api/project names the project or
     any workstream has left the active state; otherwise today's Overview. */
  function projectLanding(project, rows) {
    if (project && project.name) return "project";
    var list = rows || [];
    for (var i = 0; i < list.length; i++) {
      if (ADT.wsStateOf(list[i]) !== "active") return "project";
    }
    return "exec";
  }
  function wsNum(w) { var m = /(\d+)/.exec(w.ws || ""); return m ? +m[1] : 999; }
  function shown(list) { return list.filter(function (w) { return w.plan; }).sort(function (x, y) { return wsNum(x) - wsNum(y); }); }
  function canonWs(ws) {
    if (!ws || String(ws).toLowerCase() === "all" || String(ws).toLowerCase() === "overview") return "all";
    if (/^ws\d+$/i.test(ws)) return String(ws).toUpperCase();
    return String(ws);
  }
  function readRoute() {
    var q = new URLSearchParams(location.search);
    var view = q.get("view");
    if (view === "current") view = "exec";
    var path = location.pathname;
    if (VIEWS.indexOf(view) < 0) view = (path === "/plan" || /plan\.html$/.test(path)) ? "plan" : "exec";
    return {view: view, ws: canonWs(q.get("ws") || "")};
  }
  function planIdFor(ws) {
    if (ws && ws !== "all") return ws;
    var program = null;
    (state.bundles || []).forEach(function (b) { if (!program && (b.kind === "program" || b.id === "OVERVIEW")) program = b; });
    if (program) return program.id;
    return (state.bundles && state.bundles.length) ? state.bundles[0].id : "OVERVIEW";
  }
  function queryWs() {
    if (state.view === "plan") return planIdFor(state.ws);
    return state.ws === "all" ? "all" : state.ws;
  }
  function pushRoute(hash) {
    var u = new URL(location.href);
    var path = location.pathname;
    if (path === "/plan" || /\/plan\.html$/.test(path)) u.pathname = /plan\.html$/.test(path) ? path.replace(/plan\.html$/, "index.html") : "/";
    u.searchParams.set("view", state.view);
    u.searchParams.set("ws", queryWs());
    if (FIX) u.searchParams.set("fixtures", "");
    u.hash = hash || "";
    var next = u.pathname + u.search + u.hash;
    if (next === location.pathname + location.search + location.hash) return;
    history.pushState({view: state.view, ws: state.ws}, "", u);
  }
  function applyChrome() {
    var plan = state.view === "plan";
    document.getElementById("exec-view").hidden = plan;
    document.getElementById("plan-view").hidden = !plan;
    document.getElementById("view-plan").setAttribute("aria-pressed", plan ? "true" : "false");
    document.getElementById("view-exec").setAttribute("aria-pressed", plan ? "false" : "true");
    /* The header names the project once /api/project answers; without a
       project.json it falls back to Agent dashboard. */
    var proj = (state.project && state.project.name) || "Agent dashboard";
    document.getElementById("title").textContent = proj;
    var names = {plan: "Plan", exec: "Execution", project: "Project", planned: "Planned", archive: "Archive"};
    document.title = (names[state.view] || "Execution") + " \u00B7 " + proj;
    updateViewNav();
  }
  function scrollHash() {
    if (!location.hash) return;
    var node = document.querySelector(location.hash);
    if (node && node.scrollIntoView) node.scrollIntoView();
  }
  function showCurrent() {
    if (window.__adReplayOn && window.__adReplayExit) window.__adReplayExit(true);
    window.__adReplayOn = false;
    applyChrome();
    renderTabs();
    if (isProjectView()) {
      showLoading();
      refreshProject();
      return;
    }
    if (state.view === "exec") {
      var hit = getCache();
      if (hit) {
        applyHit(hit);
        drawMain();
      } else {
        showLoading();
      }
      refreshAll();
      return;
    }
    var id = planIdFor(state.ws);
    loadNav().then(function () {
      var job = window.PlanView ? PlanView.show(id) : null;
      if (job && job.then) job.then(function (ok) { setLive(ok !== false); scrollHash(); });
    });
  }
  function selectTab(ws) {
    ws = canonWs(ws);
    if (ws === state.ws) return;
    state.ws = ws;
    pushRoute();
    showCurrent();
  }
  function selectView(view) {
    if (VIEWS.indexOf(view) < 0) return;
    if (state.view === view) return;
    state.view = view;
    pushRoute();
    showCurrent();
  }
  function openPlan(ws) {
    state.ws = canonWs(ws);
    state.view = "plan";
    pushRoute("");
    showCurrent();
  }
  function openApprovals(ws) {
    state.ws = canonWs(ws);
    state.view = "plan";
    pushRoute(String(planIdFor(state.ws)).toLowerCase() + "-approvals");
    showCurrent();
  }
  /* Tabs list the workstreams that keep one: active, paused, or done
     (planned bundles appear in the Planned view, never as tabs; archived
     and dropped live on the Archive shelf). The All | Active | Completed
     filter keeps working over these rows with its identical completed
     rule. */
  function tabRows() {
    var seen = {}, rows = [];
    var byId = {};
    /* State lookups read the all-states list first: a bundle whose row
       left the default scope (archived, dropped) still resolves to its
       real state here instead of the active default. */
    (state.wssAll || []).forEach(function (w) { byId[String(w.ws).toUpperCase()] = w; });
    (state.wss || []).forEach(function (w) { byId[String(w.ws).toUpperCase()] = w; });
    shown(state.wss || []).forEach(function (w) {
      var key = String(w.ws).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      rows.push({ws: w.ws, name: w.ws + (w.name ? " " + w.name : ""), running: w.live_agents, progress: progressFor(w.ws), state: ADT.wsStateOf(w)});
    });
    (state.bundles || []).forEach(function (b) {
      if (!b || b.kind === "program" || b.id === "OVERVIEW") return;
      var key = String(b.id).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      var hit = byId[key];
      rows.push({ws: b.id, name: b.id + (b.title ? " " + b.title : ""), running: 0, progress: progressFor(b.id), state: ADT.wsStateOf(hit)});
    });
    rows = rows.filter(function (r) { return ADT.isTabState(r.state); });
    rows.sort(function (a, b) { return wsNum(a) - wsNum(b); });
    return [{ws: "all", name: state.view === "plan" ? "Overview" : "Overall", progress: null}].concat(rows);
  }
  /* Tab filter (All | Active | Completed) and drag order, remembered per
     viewer. The overview tab stays pinned first and is not draggable. A
     re-render while a drag is in progress is deferred to dragend so the
     dragged tab is never lost to a refresh. */
  var tabFilter = ADT.loadMode();
  var tabDrag = null, tabDragPending = false, tabClickGuard = 0;
  function currentOrder() {
    var t = document.getElementById("tabs");
    if (!t) return [];
    var ids = [];
    [].forEach.call(t.querySelectorAll("button.tab[data-ws]"), function (b) {
      var id = String(b.getAttribute("data-ws")).toUpperCase();
      if (id !== "ALL" && ids.indexOf(id) < 0) ids.push(id);
    });
    return ids;
  }
  function onTabDragStart(ev, ws) {
    tabDrag = {id: String(ws).toUpperCase(), after: false};
    try { ev.dataTransfer.setData("text/plain", String(ws)); ev.dataTransfer.effectAllowed = "move"; } catch (e) {}
  }
  function onTabDragOver(ev, btn) {
    if (!tabDrag) return;
    ev.preventDefault();
    try { ev.dataTransfer.dropEffect = "move"; } catch (e) {}
    var r = btn.getBoundingClientRect();
    tabDrag.after = (ev.clientX - r.left) > r.width / 2;
    var t = document.getElementById("tabs");
    [].forEach.call(t.querySelectorAll(".drop-before,.drop-after"), function (n) { n.classList.remove("drop-before", "drop-after"); });
    btn.classList.add(tabDrag.after ? "drop-after" : "drop-before");
  }
  function finishTabDrag(targetWs, after) {
    if (!tabDrag || !tabDrag.id) return;
    var ids = currentOrder().filter(function (id) { return id !== tabDrag.id; });
    var at = ids.indexOf(String(targetWs).toUpperCase());
    ids.splice(at < 0 ? ids.length : at + (after ? 1 : 0), 0, tabDrag.id);
    ADT.saveOrder(ids);
    tabDrag = null;
    tabDragPending = false;
    tabClickGuard = Date.now() + 400;
    renderTabs();
  }
  function onTabDragEnd() {
    if (!tabDrag) return;
    tabDrag = null;
    tabDragPending = false;
    tabClickGuard = Date.now() + 400;
    renderTabs();
  }
  function onTabKey(ev, ws) {
    if (!ev.altKey || !ev.shiftKey) return;
    var left = ev.key === "ArrowLeft", right = ev.key === "ArrowRight";
    if (!left && !right) return;
    ev.preventDefault();
    ADT.saveOrder(ADT.move(currentOrder(), ws, left ? -1 : 1));
    renderTabs();
    var btn = document.getElementById("tabs").querySelector('button.tab[data-ws="' + ws + '"]');
    if (btn) btn.focus();
  }
  function filterCtl(counts) {
    var wrap = el("span", {class: "tabfilter", role: "group", "aria-label": "Filter workstream tabs"});
    [["all", "All"], ["active", "Active"], ["completed", "Completed"]].forEach(function (m) {
      var b = el("button", {type: "button", "aria-pressed": String(tabFilter === m[0]), title: "Show " + m[0] + " workstream tabs", onclick: function () { tabFilter = m[0]; ADT.saveMode(m[0]); renderTabs(); }}, [m[1] + " " + fmtN(counts[m[0]])]);
      wrap.appendChild(b);
    });
    wrap.appendChild(el("button", {type: "button", class: "tabreset", title: "Restore the default tab order", onclick: function () { ADT.saveOrder([]); renderTabs(); }}, ["Reset order"]));
    return wrap;
  }
  /* The pinned bar chrome entry every refresh path calls. The tab
     strip is gone: renderBar (static/core/switcher.js) paints Overall,
     the selected chip, the switcher button, and the pins. The completed
     count below still reads the shared completed rule, so the filter
     and the Where-we-are status cannot drift from the bar. */
  function renderTabs() {
    if (typeof renderBar === "function") renderBar();
    var done = 0;
    tabRows().forEach(function (w) {
      if (w.ws !== "all" && ADT.isCompleted(w.progress)) done++;
    });
    var sw = document.getElementById("ws-switcher");
    if (sw) sw.setAttribute("data-completed", String(done));
    updateViewNav();
  }
  /* Header nav pills (Project | Current | Planned | Archive) beside the
     Plan | Execution switch. Planned and Archive carry their counts from
     the all-states workstream list; the active pill marks the view. */
  function viewCounts() {
    var rows = (state.wssAll && state.wssAll.length ? state.wssAll : state.wss) || [];
    var planned = 0, archive = 0;
    rows.forEach(function (w) {
      var st = ADT.wsStateOf(w);
      if (st === "planned") planned++;
      else if (st === "archived" || st === "dropped") archive++;
    });
    if ((!state.wssAll || !state.wssAll.length) && state.archive && state.archive.totals) {
      var n = +(state.archive.totals.workstreams || 0);
      if (n > archive) archive = n;
    }
    return {planned: planned, archive: archive};
  }
  function updateViewNav() {
    var nav = document.getElementById("view-nav");
    if (!nav) return;
    var counts = viewCounts();
    var defs = [["project", "Project", null], ["exec", "Current", null], ["planned", "Planned", counts.planned], ["archive", "Archive", counts.archive]];
    nav.innerHTML = "";
    defs.forEach(function (d) {
      var label = d[1] + (d[2] == null ? "" : " " + fmtN(d[2]));
      var attrs = {type: "button", "data-view": d[0], title: d[1] + " view", onclick: function () { selectView(d[0]); }};
      if (state.view === d[0]) attrs["aria-current"] = "page";
      nav.appendChild(el("button", attrs, [label]));
    });
  }
  function planUrl(ws) {
    var id = canonWs(ws);
    var q = new URLSearchParams(location.search);
    q.set("view", "plan");
    q.set("ws", id === "all" ? "OVERVIEW" : id);
    return "?" + q.toString();
  }
  function go(ws) { selectTab(ws); }
  /* Window start on the same 30 s grid the server keys (and tags) on: a
     per-second since makes every tick and sweep URL novel, so the
     conditional layer never matches and every request refetches and
     recomputes. A 24 h window moving by seconds is invisible in the sums. */
  function since() { return Math.floor(Date.now() / 30000) * 30 - WINDOW_HOURS * 3600; }
  function wsParam() { return state.ws === "all" ? {} : {ws: state.ws}; }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.WINDOW_HOURS = WINDOW_HOURS;
  AdPage.state = state;
  AdPage.isClipMode = isClipMode;
  AdPage.isClipVertical = isClipVertical;
  AdPage.isProjectView = isProjectView;
  AdPage.projectLanding = projectLanding;
  AdPage.wsNum = wsNum;
  AdPage.shown = shown;
  AdPage.canonWs = canonWs;
  AdPage.readRoute = readRoute;
  AdPage.planIdFor = planIdFor;
  AdPage.queryWs = queryWs;
  AdPage.pushRoute = pushRoute;
  AdPage.applyChrome = applyChrome;
  AdPage.scrollHash = scrollHash;
  AdPage.selectTab = selectTab;
  AdPage.selectView = selectView;
  AdPage.showCurrent = showCurrent;
  AdPage.openPlan = openPlan;
  AdPage.openApprovals = openApprovals;
  AdPage.tabRows = tabRows;
  AdPage.tabFilter = tabFilter;
  AdPage.tabDrag = tabDrag;
  AdPage.tabDragPending = tabDragPending;
  AdPage.tabClickGuard = tabClickGuard;
  AdPage.currentOrder = currentOrder;
  AdPage.onTabDragStart = onTabDragStart;
  AdPage.onTabDragOver = onTabDragOver;
  AdPage.finishTabDrag = finishTabDrag;
  AdPage.onTabDragEnd = onTabDragEnd;
  AdPage.onTabKey = onTabKey;
  AdPage.filterCtl = filterCtl;
  AdPage.renderTabs = renderTabs;
  AdPage.viewCounts = viewCounts;
  AdPage.updateViewNav = updateViewNav;
  AdPage.planUrl = planUrl;
  AdPage.go = go;
  AdPage.since = since;
  AdPage.wsParam = wsParam;
  global.WINDOW_HOURS = WINDOW_HOURS;
  global.state = state;
  global.isClipMode = isClipMode;
  global.isClipVertical = isClipVertical;
  global.isProjectView = isProjectView;
  global.projectLanding = projectLanding;
  global.wsNum = wsNum;
  global.shown = shown;
  global.canonWs = canonWs;
  global.readRoute = readRoute;
  global.planIdFor = planIdFor;
  global.queryWs = queryWs;
  global.pushRoute = pushRoute;
  global.applyChrome = applyChrome;
  global.scrollHash = scrollHash;
  global.selectTab = selectTab;
  global.selectView = selectView;
  global.showCurrent = showCurrent;
  global.openPlan = openPlan;
  global.openApprovals = openApprovals;
  global.tabRows = tabRows;
  global.tabFilter = tabFilter;
  global.tabDrag = tabDrag;
  global.tabDragPending = tabDragPending;
  global.tabClickGuard = tabClickGuard;
  global.currentOrder = currentOrder;
  global.onTabDragStart = onTabDragStart;
  global.onTabDragOver = onTabDragOver;
  global.finishTabDrag = finishTabDrag;
  global.onTabDragEnd = onTabDragEnd;
  global.onTabKey = onTabKey;
  global.filterCtl = filterCtl;
  global.renderTabs = renderTabs;
  global.viewCounts = viewCounts;
  global.updateViewNav = updateViewNav;
  global.planUrl = planUrl;
  global.go = go;
  global.since = since;
  global.wsParam = wsParam;
})(window);
