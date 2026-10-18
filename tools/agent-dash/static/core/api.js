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
  var FIXMAP = {"/api/snapshot": "snapshot", "/api/runs": "runs", "/api/usage": "usage-model-hour", "/api/concurrency": "concurrency", "/api/sessions": "sessions", "/api/workstreams": "workstreams", "/api/project": "project", "/api/projects": "projects", "/api/archive": "archive", "/api/health": "health", "/api/reconcile": "reconcile"};
  /* Hidden-tab pause: stream ticks and background warmups cost server
     work and renders nobody sees, so they no-op while hidden; the next
     visible tick refreshes. */
  function tabHidden() {
    try { return typeof document !== "undefined" && !!document.hidden; }
    catch (e) { return false; }
  }
  /* Conditional refetch: the server tags every 200 with an ETag and
     answers 304 when the entry is unchanged, so idle ticks cost headers
     only. Bodies wait here keyed by URL (bounded); a 304 replays the
     stored body, a lost entry refetches unconditionally. */
  var condTag = {};
  var condBody = {};
  var condOrder = [];
  function condSave(url, tag, body) {
    if (!tag) return body;
    if (condTag[url] === undefined) condOrder.push(url);
    condTag[url] = tag;
    condBody[url] = body;
    while (condOrder.length > 24) {
      var old = condOrder.shift();
      if (old !== url) { delete condTag[old]; delete condBody[old]; }
    }
    return body;
  }
  function condFetch(url, tag) {
    var headers = {};
    if (tag) headers["If-None-Match"] = tag;
    return fetch(url, {headers: headers});
  }
  function condRead(url, r) {
    var tag = (r.headers && r.headers.get) ? r.headers.get("ETag") : null;
    return r.json().then(function (j) { return condSave(url, tag, j); });
  }
  /* Store clocks that move without changing any rendered number: the
     poll sequence (process samples and counts bump it every cycle), the
     data sequence stamp, the last-write stamp, and the serve time. */
  var SNAP_CLOCKS = {now: 1, seq: 1, data_seq: 1, last_data_ts: 1};
  /* Per-run telemetry sampled on every poll: RSS, the per-request
     elapsed clock, and output byte counters. These jitter constantly
     while agents are alive without any data write landing, so the
     project views (whose sections key on aggregates, not per-second
     run telemetry) strip them and idle instead of redrawing on every
     tick. The exec path keeps them, so live run rows still repaint
     there; the on-demand run drawer always reads live state, so it
     stays exact everywhere. */
  var RUN_CLOCKS = {rss_kb: 1, elapsed_s: 1, out_bytes: 1, err_bytes: 1, out_growing: 1};
  function snapSig(j, extra) {
    /* Render signature: the snapshot minus its volatile clocks. Equal
       signatures mean an identical render, so stream ticks that only move
       the store clocks skip drawMain instead of rebuilding sections. Any
       real change (counts, totals, usage, run lists) still changes the
       signature and draws. */
    if (!j || typeof j !== "object") return null;
    try {
      return JSON.stringify(j, function (k, v) { return (SNAP_CLOCKS[k] || (extra && extra[k])) ? 0 : v; });
    } catch (e) { return null; }
  }
  function api(path, params) {
    var qs = new URLSearchParams(params || {}).toString();
    var url = path + (qs ? "?" + qs : "");
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
    return condFetch(url, condTag[url]).then(function (r) {
      if (r.status === 304) {
        if (condBody[url]) return JSON.parse(JSON.stringify(condBody[url]));
        return condFetch(url, null).then(function (r2) {
          if (!r2.ok) throw new Error(r2.status);
          return condRead(url, r2);
        });
      }
      if (!r.ok) throw new Error(r.status);
      return condRead(url, r);
    });
  }
  /* POST through the same layer (archive/restore: the one write action
     the page performs). Always sends X-AgentDash-Action: 1, which the
     server requires alongside its local Host/Origin checks. Fixture
     mode (?fixtures) never writes, so it rejects there and the caller
     falls back to the copy-command button. Rejects with the server's
     {status, error, code, hint} for a toast, never silent. */
  function apiPost(path, body) {
    if (FIX) return Promise.reject({status: 403, error: "fixture preview is read-only", code: "read_only", hint: "Archive from the live dashboard, or run: python3 agent_dash.py ws archive <ID>"});
    return fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-AgentDash-Action": "1"}, body: JSON.stringify(body || {})}).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) { var err = (j && typeof j === "object") ? j : {error: "request failed " + r.status}; err.status = r.status; throw err; }
        return j;
      });
    });
  }
  window.DashApi = api;
  window.DashApiPost = apiPost;
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
    var projP = api("/api/project", {}).then(function (j) { state.project = (j && typeof j === "object") ? j : {name: null}; state.actionsArchive = !!(j && j.actions && j.actions.archive); }).catch(function () { state.project = {name: null}; state.actionsArchive = false; });
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
  /* quiet (tick-driven) refreshes skip the draw when nothing render-
     relevant changed, exactly like the exec tick path skips drawMain on
     an equal snapshot signature. Explicit refreshes (boot, interval,
     archive action) always draw. The signature covers everything the
     draw reads: the clock-stripped snapshot plus archive, approvals,
     bundles, rows, project and plans. */
  function projSig() {
    try {
      return snapSig(state.snap, RUN_CLOCKS) + "\u0000" + JSON.stringify([
        state.archive, state.approvals, state.bundles,
        state.wss, state.project, state.plans
      ]);
    } catch (e) { return null; }
  }
  function refreshProject(quiet) {
    if (window.__adReplayOn) return Promise.resolve();
    var group = state.groupBy;
    var reqSince = since();
    var gen = ++execGen;
    refreshActive++;
    var finished = false;
    var jobs = [
      api("/api/snapshot", {ws: "all", since: reqSince, usage_group: group}).catch(function () { return null; }),
      api("/api/runs", {since: reqSince, limit: 5000}).then(function (j) { return j.runs || []; }).catch(function () { return []; }),
      api("/api/concurrency", {since: reqSince, step: 60}).then(function (j) { return j.points || []; }).catch(function () { return []; }),
      loadNav(),
      api("/api/archive", {}).then(function (j) { return j; }).catch(function () { return null; }),
      api("/api/approvals", {ws: "all"}).then(approvalList).catch(function () { return []; })
    ];
    return Promise.all(jobs).then(function (parts) {
      finished = true; refreshDone();
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
      state.snapSig = snapSig(parts[0]);
      state.conc = parts[2];
      state.archive = (parts[4] && parts[4].archive) ? parts[4] : {archive: [], totals: {workstreams: 0, tokens: 0, cost_usd: 0.0, runs: 0}};
      state.approvals = parts[5];
      return loadProjectPlans().then(function () {
        if (gen !== execGen || !isProjectView()) return;
        renderTabs();
        var sig = projSig();
        var same = !!quiet && sig != null && state.projSig != null && sig === state.projSig;
        state.projSig = sig;
        if (!same) {
          drawMain();
          window.__adSettled = "project-" + state.view + "|24|" + state.seq;
        }
        setLive(true);
        updateStale();
        var foot = document.getElementById("foot");
        if (foot) foot.textContent = "Updated " + new Date().toLocaleTimeString() + (FIX ? " \u00B7 fixture data" : "") + " \u00B7 local, 127.0.0.1";
      });
    }).catch(function () { if (!finished) { finished = true; refreshDone(); } if (gen === execGen && isProjectView()) { showSnapError("all"); setLive(false); } });
  }
  /* Muted footer line: the refresh time only. Reconcile and doctor output
     stay in the CLI and the API by owner decision, never on the page. */
  function refreshFoot() {
    var foot = document.getElementById("foot");
    if (!foot || state.view !== "exec" || state.ws !== "all") return;
    foot.textContent = "Updated " + new Date().toLocaleTimeString() + (FIX ? " · fixture data" : "") + " · local, 127.0.0.1";
  }
  /* Warm every other tab's Execution data in the background, one tab at a time, so a first switch draws from cache instantly. Entries are keyed by tab and group (one fixed 24 h window) with the window captured up front, and fresh entries are not refetched. */
  var warming = false;
  function warmTabs() {
    /* Never compete with the foreground tab: a warmup started over a
       running boot burst starves it (tens of full folds queue behind),
       and an abandoned sweep still burns server folds after a tab
       switch. The next turn retries once the page is idle. */
    if (warming || FIX || isProjectView() || tabHidden() || refreshBusy()) return;
    var group = state.groupBy, reqSince = since();
    var list = ["all"].concat(shown(state.wss).map(function (w) { return w.ws; })).filter(function (ws) { return ws !== state.ws; });
    if (!list.length) return;
    warming = true;
    var step = function (i) {
      if (i >= list.length) { warming = false; return; }
      /* Yield to the foreground tab at every step: a burst (boot, switch,
         slow tick) owns the server while it runs; the warmup resumes
         where it left off instead of queueing folds behind it. */
      if (refreshBusy() || tabHidden()) { setTimeout(function () { step(i); }, 500); return; }
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
  /* Full refreshes in flight: background ticks, warmups and the slow
     interval never stack another burst on top of one already running.
     A skipped tick is harmless (its seq is stale by the time the running
     refresh lands); the interval and the warmup retry on their next turn. */
  var refreshActive = 0;
  function refreshBusy() { return refreshActive > 0; }
  function refreshDone() { if (refreshActive > 0) refreshActive--; }
  function refreshAll() {
    if (window.__adReplayOn) return Promise.resolve();
    var ws = state.ws;
    var key = reqKey();
    var gen = ++execGen;
    refreshActive++;
    var finished = false;
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
      finished = true; refreshDone();
      if (gen !== execGen || !ADC.applies(key, state)) return;
      /* Landing can flip exec to a project view mid-flight (boot no
         longer waits for nav): hand off instead of drawing exec under
         it. The project refresh reuses the same cached responses. */
      if (isProjectView()) { refreshProject(); return; }
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
      state.snapSig = snapSig(parts[0]);
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
        else { var foot = document.getElementById("foot"); if (foot) foot.textContent = "Updated " + new Date().toLocaleTimeString() + (FIX ? " · fixture data" : "") + " · local, 127.0.0.1"; }
      }
    }).catch(function () { if (!finished) { finished = true; refreshDone(); } if (gen === execGen && ADC.applies(key, state)) { showSnapError(ws); setLive(false); } });
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
      if (tabHidden()) return;
      try {
        var d = JSON.parse(ev.data);
        var nowS = d.now || Date.now() / 1000;
        if (d.last_poll != null) state.lastPoll = d.last_poll;
        if (d.last_data_ts != null) state.lastDataTs = d.last_data_ts;
        if (state.lastSeqChange === 0) state.lastSeqChange = nowS;
        if (d.seq !== state.seq) { state.lastSeqChange = nowS; if (!refreshBusy()) { if (isProjectView()) refreshProject(true); else refreshSnapshot(); } }
        else updateStale(nowS);
      } catch (e) {}
      setLive(true);
      updateStale();
    });
    es.onerror = function () { setLive(false); };
  }
  /* One snapshot fetch at a time: ticks keep arriving every 2 s, and
     without this a slow server stacks a fetch per tick behind the one
     already in flight (each eventually folds server-side), turning a
     stall into a pile-on. A skipped tick is harmless: the next tick, or
     the full refresh already running, carries a newer seq. */
  var snapInflight = false;
  /* Stream ticks refresh through the same one-response path: the snapshot
     carries the usage buckets, and totals plus buckets apply together. */
  function refreshSnapshot() {
    if (window.__adReplayOn) return;
    if (tabHidden()) return;
    if (snapInflight || refreshBusy()) return;
    snapInflight = true;
    var ws = state.ws, key = reqKey();
    api("/api/snapshot", {ws: ws, since: since(), usage_group: groupFor()}).then(function (j) {
      snapInflight = false;
      if (!ADC.applies(key, state)) return;
      clearSnapError();
      var sig = snapSig(j);
      if (sig !== null && sig === state.snapSig) {
        if (j.seq !== state.seq) state.lastSeqChange = Date.now() / 1000;
        state.seq = j.seq;
        if (j.last_data_ts != null) state.lastDataTs = j.last_data_ts;
        updateStale();
        return;
      }
      state.snapSig = sig;
      if (j.seq !== state.seq) state.lastSeqChange = Date.now() / 1000;
      if (j.last_data_ts != null) state.lastDataTs = j.last_data_ts;
      state.snap = j; state.seq = j.seq;
      state.usage = Array.isArray(j.usage) ? j.usage : [];
      var hit = execCache[key];
      if (hit) { hit.snap = j; hit.usage = state.usage; }
      if (window.DashCache) window.DashCache.snap[ws] = j;
      if (state.view === "exec") drawMain();
      updateStale();
    }).catch(function () { snapInflight = false; showSnapError(ws); if (state.view === "exec") drawMain(); });
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.FIXMAP = FIXMAP;
  AdPage.tabHidden = tabHidden;
  AdPage.snapSig = snapSig;
  AdPage.api = api;
  AdPage.apiPost = apiPost;
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
  AdPage.refreshBusy = refreshBusy;
  AdPage.setLive = setLive;
  AdPage.stream = stream;
  AdPage.refreshSnapshot = refreshSnapshot;
  global.FIXMAP = FIXMAP;
  global.tabHidden = tabHidden;
  global.snapSig = snapSig;
  global.api = api;
  global.apiPost = apiPost;
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
  global.refreshBusy = refreshBusy;
  global.setLive = setLive;
  global.stream = stream;
  global.refreshSnapshot = refreshSnapshot;
})(window);
