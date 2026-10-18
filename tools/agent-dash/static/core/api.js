/* Page core: fetch, fixtures, stream, and refresh orchestration.
   Exports (on window.AdPage, also kept as plain globals): FIXMAP and api
   (api is also window.DashApi, used by the plan view; with ?fixtures every
   path reads ../fixtures/*.json instead of the network); approvalList
   (approvals-response normalizer); loadNav, refreshFoot (tab-list and footer
   refresh); resolveLanding (first-load default view); refreshProject,
   loadProjectPlans (the all-scope project-view refresh paths); warming,
   warmTabs (background per-tab warmup); showSnapError,
   clearSnapError, renderErrBanner (loud fetch failures); STALE_AFTER_S,
   updateStale (tick-freshness badge); refreshAll, setLive, stream,
   refreshSnapshot (the one-response refresh paths).
   Needs: FIX from el.js; WINDOW_HOURS, state, since, wsParam, groupFor,
   reqKey, getCache, setCache, execCache from the state/cache files;
   window.ADC; renderTabs, applyChrome, projectLanding, isProjectView from
   state.js; planSum, planPrompt, planProg from the ws-cards file; drawMain,
   which stays in the page script and is read as a plain global at call time. */
(function (global) {
  "use strict";
  var FIXMAP = {"/api/snapshot": "snapshot", "/api/runs": "runs", "/api/usage": "usage-model-hour", "/api/concurrency": "concurrency", "/api/sessions": "sessions", "/api/workstreams": "workstreams", "/api/project": "project", "/api/archive": "archive", "/api/health": "health", "/api/reconcile": "reconcile"};
  function api(path, params) {
    var qs = new URLSearchParams(params || {}).toString();
    if (FIX) {
      if (path === "/api/approvals") {
        return fetch("../fixtures/approvals.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (j) {
          var wanted = params && params.ws;
          if (wanted && wanted !== "all") {
            var hit = (j.approvals || []).filter(function (row) { return row.ws === wanted; })[0];
            return hit || {ws: wanted, decided_at: null, decided_by: null, items: []};
          }
          return j;
        });
      }
      if (path === "/api/snapshot") {
        var sbase = "snapshot";
        if (params && params.ws && params.ws !== "all") sbase = "snapshot-" + params.ws;
        return fetch("../fixtures/" + sbase + ".json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (snap) {
          if (params && params.usage_group && !Array.isArray(snap.usage)) {
            var ubase = params.usage_group === "workstream" ? "usage-workstream-hour" : "usage-model-hour";
            return fetch("../fixtures/" + ubase + ".json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (u) {
              snap.usage = u.buckets || [];
              return snap;
            });
          }
          return snap;
        });
      }
      var base = FIXMAP[path] || path.replace("/api/", "");
      if (path === "/api/usage" && params && params.group === "workstream") base = "usage-workstream-hour";
      if (path === "/api/workstreams" && params && params.state) base = "workstreams-all";
      if (path === "/api/plan") base = "plan-" + params.ws;
      if (path === "/api/replay") base = "replay-" + params.ws;
      return fetch("../fixtures/" + base + ".json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }
    return fetch(path + (qs ? "?" + qs : "")).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  window.DashApi = api;
  function approvalList(j) {
    if (!j) return [];
    if (j.approvals) return j.approvals;
    if (j.items) return [j];
    return [];
  }
  /* Every async response applies only when it still matches the current tab
     and grouping (one fixed 24 h window); a late arrival from another scope
     is dropped. */
  function loadNav() {
    var bundlesP = fetch(FIX ? "../fixtures/bundles.json" : "/api/bundles", {cache: "no-store"}).then(function (r) { return r.ok ? r.json() : {bundles: []}; }).then(function (j) { state.bundles = j.bundles || []; }).catch(function () {});
    var wsP = api("/api/workstreams", {since: since()}).then(function (j) { state.wss = j.workstreams || []; }).catch(function () {});
    /* Project identity (missing project.json means name null: the header
       falls back to Agent dashboard) and the all-states list behind the
       Planned/Archive pill counts. Both are best-effort on older servers. */
    var projP = api("/api/project", {}).then(function (j) { state.project = (j && typeof j === "object") ? j : {name: null}; }).catch(function () { state.project = {name: null}; });
    var allP = api("/api/workstreams", {since: since(), state: "all"}).then(function (j) { state.wssAll = j.workstreams || []; }).catch(function () { state.wssAll = []; });
    return Promise.all([bundlesP, wsP, projP, allP]).then(function () { renderTabs(); applyChrome(); resolveLanding(); });
  }
  /* First-load default view, once per page load: Project home when the
     project names itself or any workstream left the active state,
     otherwise today's Overview. An explicit ?view= always wins. */
  function resolveLanding() {
    if (state.landed) return;
    state.landed = true;
    var explicit = false;
    try { explicit = new URLSearchParams(location.search).has("view"); } catch (e) { explicit = true; }
    if (explicit) return;
    var next = projectLanding(state.project, state.wss);
    if (next && next !== state.view) {
      state.view = next;
      pushRoute();
    }
  }
  /* All-scope refresh for the Project, Planned, and Archive views: the
     tiles reuse the same all-scope snapshot the exec Overview reads, the
     cards read the all-states workstreams plus plans, archive, and the
     approvals roll-up. Tiles and usage apply together or not at all. */
  function loadProjectPlans() {
    var ids = [];
    (state.wss || []).forEach(function (w) { if (w.plan && ids.indexOf(w.ws) < 0) ids.push(w.ws); });
    return Promise.all(ids.map(function (id) {
      return api("/api/plan", {ws: id}).then(function (p) {
        state.plans[id] = (p && p.groups) ? p : null;
        planSum[id] = planProg(null, p);
        planPrompt[id] = p && p.start_prompt;
      }).catch(function () {});
    })).then(function () { renderTabs(); });
  }
  function refreshProject() {
    if (window.__adReplayOn) return Promise.resolve();
    var group = state.groupBy;
    var reqSince = since();
    var gen = ++execGen;
    var jobs = [
      api("/api/snapshot", {ws: "all", since: reqSince, usage_group: group}).catch(function () { return null; }),
      api("/api/runs", {since: reqSince, limit: 5000}).then(function (j) { return j.runs || []; }).catch(function () { return []; }),
      api("/api/concurrency", {since: reqSince, step: 60}).then(function (j) { return j.points || []; }).catch(function () { return []; }),
      loadNav(),
      api("/api/archive", {}).then(function (j) { return j; }).catch(function () { return null; }),
      api("/api/approvals", {ws: "all"}).then(approvalList).catch(function () { return []; })
    ];
    return Promise.all(jobs).then(function (parts) {
      if (gen !== execGen || !isProjectView()) return;
      if (!parts[0]) {
        showSnapError("all");
        if (isProjectView()) { drawMain(); setLive(false); }
        return;
      }
      clearSnapError();
      if (parts[0].seq !== state.seq) state.lastSeqChange = Date.now() / 1000;
      if (parts[0].last_data_ts != null) state.lastDataTs = parts[0].last_data_ts;
      state.snap = parts[0];
      state.seq = parts[0].seq;
      state.runs = parts[1];
      state.usage = Array.isArray(parts[0].usage) ? parts[0].usage : [];
      state.conc = parts[2];
      state.archive = (parts[4] && parts[4].archive) ? parts[4] : {archive: [], totals: {workstreams: 0, tokens: 0, cost_usd: 0.0, runs: 0}};
      state.approvals = parts[5];
      return loadProjectPlans().then(function () {
        if (gen !== execGen || !isProjectView()) return;
        renderTabs();
        drawMain();
        window.__adSettled = "project-" + state.view + "|24|" + state.seq;
        setLive(true);
        updateStale();
        var foot = document.getElementById("foot");
        if (foot) foot.textContent = "Updated " + new Date().toLocaleTimeString() + (FIX ? " \u00B7 fixture data" : "") + " \u00B7 read-only, 127.0.0.1";
      });
    }).catch(function () { if (gen === execGen && isProjectView()) { showSnapError("all"); setLive(false); } });
  }
  /* Muted footer line: the reconcile check for the fixed 24 h window. Status color always ships with a word and symbol. */
  function refreshFoot() {
    var foot = document.getElementById("foot");
    if (!foot || state.view !== "exec" || state.ws !== "all") return;
    api("/api/reconcile", {hours: WINDOW_HOURS}).then(function (j) {
      if (state.view !== "exec" || state.ws !== "all") return;
      var bad = (j.mismatches || [])[0], n = (j.rows || []).length;
      var base = "Updated " + new Date().toLocaleTimeString() + (FIX ? " · fixture data" : "") + " · read-only, 127.0.0.1";
      foot.textContent = "";
      foot.appendChild(document.createTextNode(base + " · "));
      var mark = el("span", {text: bad ? "\u2717 " + bad : "\u2713 Numbers reconcile across " + n + " workstreams"});
      mark.setAttribute("style", "color:" + (bad ? "var(--critical)" : "var(--good)"));
      foot.appendChild(mark);
    }).catch(function () {});
  }
  /* Warm every other tab's Execution data in the background, one tab at a time, so a first switch draws from cache instantly. Entries are keyed by tab and group (one fixed 24 h window) with the window captured up front, and fresh entries are not refetched. */
  var warming = false;
  function warmTabs() {
    if (warming || FIX || isProjectView()) return;
    var group = state.groupBy, reqSince = Math.floor(Date.now() / 1000 - WINDOW_HOURS * 3600);
    var list = ["all"].concat(shown(state.wss).map(function (w) { return w.ws; })).filter(function (ws) { return ws !== state.ws; });
    if (!list.length) return;
    warming = true;
    var step = function (i) {
      if (i >= list.length) { warming = false; return; }
      var ws = list[i], wsArg = ws === "all" ? {} : {ws: ws}, wsGroup = ws === "all" ? group : "model";
      var key = ADC.key(ws, wsGroup);
      if (execCache[key] && ADC.fresh(execCache[key], Date.now(), ADC.TTL_MS)) { setTimeout(function () { step(i + 1); }, 150); return; }
      Promise.all([
        api("/api/snapshot", {ws: ws, since: reqSince, usage_group: wsGroup}).catch(function () { return null; }),
        api("/api/runs", Object.assign({since: reqSince, limit: 5000}, wsArg)).then(function (j) { return j.runs || []; }).catch(function () { return []; }),
        api("/api/concurrency", Object.assign({since: reqSince, step: 60}, wsArg)).then(function (j) { return j.points || []; }).catch(function () { return []; }),
        ws !== "all" ? api("/api/plan", {ws: ws}).then(function (j) { return j && j.groups ? j : null; }).catch(function () { return null; }) : Promise.resolve(null),
        api("/api/approvals", {ws: ws === "all" ? "all" : ws}).then(approvalList).catch(function () { return []; })
      ]).then(function (p) {
        if (p[0]) setCache(key, {snap: p[0], runs: p[1], usage: Array.isArray(p[0].usage) ? p[0].usage : [], conc: p[2], plan: p[3], seq: p[0].seq, approvals: p[4]});
      }).then(function () { setTimeout(function () { step(i + 1); }, 150); });
    };
    step(0);
  }
  /* A failed fetch is never a silent zero: the banner names the scope and
     the tiles carry data-failed-scope, so "request failed" reads differently
     from "0 in 24 h". A cached scope keeps its last numbers under the banner. */
  function showSnapError(ws) {
    state.snapError = "request failed for " + ws + " \u2014 showing last known data";
    renderErrBanner();
  }
  function clearSnapError() {
    state.snapError = null;
    renderErrBanner();
  }
  function renderErrBanner() {
    var b = document.getElementById("err-banner");
    if (!b) return;
    if (state.snapError) {
      b.hidden = false;
      if (b.textContent !== state.snapError) b.textContent = state.snapError;
      b.setAttribute("data-failed-scope", state.ws);
    } else {
      b.hidden = true;
      if (b.textContent !== "") b.textContent = "";
      b.removeAttribute("data-failed-scope");
    }
  }
  /* Freshness: the tick carries seq + last_poll. The seq drives refetches;
     when it stays frozen for 3 poll intervals with an open SSE stream the
     page badges "stale" instead of reading "live" on old data. A fresh poll
     with an unchanged seq (healthy idle) stays live. */
  var STALE_AFTER_S = 6;
  function updateStale(nowS) {
    var badge = document.getElementById("stale-badge");
    var txt = document.getElementById("live-txt");
    if (!badge || !txt) return;
    if (!state.lastSeqChange) { badge.hidden = true; return; }
    nowS = nowS || Date.now() / 1000;
    var seqAge = nowS - state.lastSeqChange;
    var pollAge = state.lastPoll == null ? 0 : nowS - state.lastPoll;
    var stale = seqAge > STALE_AFTER_S && (state.lastPoll == null || pollAge > STALE_AFTER_S);
    badge.hidden = !stale;
    if (stale) txt.textContent = "stale";
    else if (txt.textContent === "stale") txt.textContent = "live";
  }
  /* Tiles and chart come from one snapshot response: totals and usage buckets
     are applied together or not at all, so they can never disagree. */
  function refreshAll() {
    if (window.__adReplayOn) return Promise.resolve();
    var ws = state.ws;
    var key = reqKey();
    var gen = ++execGen;
    var wsArg = ws === "all" ? {} : {ws: ws};
    var group = groupFor();
    var reqSince = since();
    var jobs = [
      api("/api/snapshot", {ws: ws, since: reqSince, usage_group: group}).catch(function () { return null; }),
      api("/api/runs", Object.assign({since: reqSince, limit: 5000}, wsArg)).then(function (j) { return j.runs || []; }).catch(function () { return []; }),
      api("/api/concurrency", Object.assign({since: reqSince, step: 60}, wsArg)).then(function (j) { return j.points || []; }).catch(function () { return []; }),
      loadNav(),
      ws !== "all" ? api("/api/plan", {ws: ws}).then(function (j) { return j && j.groups ? j : null; }).catch(function () { return null; }) : Promise.resolve(null),
      api("/api/approvals", {ws: ws === "all" ? "all" : ws}).then(approvalList).catch(function () { return []; })
    ];
    return Promise.all(jobs).then(function (parts) {
      if (gen !== execGen || !ADC.applies(key, state)) return;
      if (!parts[0]) {
        showSnapError(ws);
        if (state.view === "exec") { drawMain(); setLive(false); }
        return;
      }
      clearSnapError();
      if (parts[0].seq !== state.seq) state.lastSeqChange = Date.now() / 1000;
      if (parts[0].last_data_ts != null) state.lastDataTs = parts[0].last_data_ts;
      state.snap = parts[0];
      state.seq = parts[0].seq;
      state.runs = parts[1];
      state.usage = Array.isArray(parts[0].usage) ? parts[0].usage : [];
      state.conc = parts[2];
      state.plan = parts[4];
      state.approvals = parts[5];
      setCache(key, {snap: state.snap, runs: state.runs, usage: state.usage, conc: state.conc, plan: state.plan, seq: state.seq, approvals: state.approvals});
      if (state.snap && window.DashCache) window.DashCache.snap[ws] = state.snap;
      renderTabs();
      if (state.view === "exec") {
        drawMain();
        window.__adSettled = state.ws + "|24|" + state.seq;
        setLive(true);
        updateStale();
        if (state.ws === "all") refreshFoot();
        else { var foot = document.getElementById("foot"); if (foot) foot.textContent = "Updated " + new Date().toLocaleTimeString() + (FIX ? " · fixture data" : "") + " · read-only, 127.0.0.1"; }
      }
    }).catch(function () { if (gen === execGen && ADC.applies(key, state)) { showSnapError(ws); setLive(false); } });
  }
  function setLive(ok) {
    document.getElementById("live-dot").className = "dot" + (ok ? " on" : "");
    document.getElementById("live-txt").textContent = ok ? "live" : "offline";
    if (!ok) { var badge = document.getElementById("stale-badge"); if (badge) badge.hidden = true; }
    else updateStale();
  }
  function stream() {
    if (FIX || !window.EventSource) return;
    var es = new EventSource("/api/stream");
    es.addEventListener("tick", function (ev) {
      try {
        var d = JSON.parse(ev.data);
        var nowS = d.now || Date.now() / 1000;
        if (d.last_poll != null) state.lastPoll = d.last_poll;
        if (d.last_data_ts != null) state.lastDataTs = d.last_data_ts;
        if (state.lastSeqChange === 0) state.lastSeqChange = nowS;
        if (d.seq !== state.seq) { state.lastSeqChange = nowS; if (isProjectView()) refreshProject(); else refreshSnapshot(); }
        else updateStale(nowS);
      } catch (e) {}
      setLive(true);
      updateStale();
    });
    es.onerror = function () { setLive(false); };
  }
  /* Stream ticks refresh through the same one-response path: the snapshot
     carries the usage buckets, and totals plus buckets apply together. */
  function refreshSnapshot() {
    if (window.__adReplayOn) return;
    var ws = state.ws, key = reqKey();
    api("/api/snapshot", {ws: ws, since: since(), usage_group: groupFor()}).then(function (j) {
      if (!ADC.applies(key, state)) return;
      clearSnapError();
      if (j.seq !== state.seq) state.lastSeqChange = Date.now() / 1000;
      if (j.last_data_ts != null) state.lastDataTs = j.last_data_ts;
      state.snap = j; state.seq = j.seq;
      state.usage = Array.isArray(j.usage) ? j.usage : [];
      var hit = execCache[key];
      if (hit) { hit.snap = j; hit.usage = state.usage; }
      if (window.DashCache) window.DashCache.snap[ws] = j;
      if (state.view === "exec") drawMain();
      updateStale();
    }).catch(function () { showSnapError(ws); if (state.view === "exec") drawMain(); });
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.FIXMAP = FIXMAP;
  AdPage.api = api;
  AdPage.approvalList = approvalList;
  AdPage.loadNav = loadNav;
  AdPage.resolveLanding = resolveLanding;
  AdPage.loadProjectPlans = loadProjectPlans;
  AdPage.refreshProject = refreshProject;
  AdPage.refreshFoot = refreshFoot;
  AdPage.warming = warming;
  AdPage.warmTabs = warmTabs;
  AdPage.showSnapError = showSnapError;
  AdPage.clearSnapError = clearSnapError;
  AdPage.renderErrBanner = renderErrBanner;
  AdPage.STALE_AFTER_S = STALE_AFTER_S;
  AdPage.updateStale = updateStale;
  AdPage.refreshAll = refreshAll;
  AdPage.setLive = setLive;
  AdPage.stream = stream;
  AdPage.refreshSnapshot = refreshSnapshot;
  global.FIXMAP = FIXMAP;
  global.api = api;
  global.approvalList = approvalList;
  global.loadNav = loadNav;
  global.resolveLanding = resolveLanding;
  global.loadProjectPlans = loadProjectPlans;
  global.refreshProject = refreshProject;
  global.refreshFoot = refreshFoot;
  global.warming = warming;
  global.warmTabs = warmTabs;
  global.showSnapError = showSnapError;
  global.clearSnapError = clearSnapError;
  global.renderErrBanner = renderErrBanner;
  global.STALE_AFTER_S = STALE_AFTER_S;
  global.updateStale = updateStale;
  global.refreshAll = refreshAll;
  global.setLive = setLive;
  global.stream = stream;
  global.refreshSnapshot = refreshSnapshot;
})(window);
