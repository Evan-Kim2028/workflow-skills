/* Page card: Where we are, workstream cards, Overview footer data.
   Renders the program panel (one two-line row per workstream with the
   server plan progress, a segmented done/to-merge/on-prod/working bar, an
   always-visible key, and tokens/cost/live in the secondary line), the
   per-workstream cards on the overview, and owns the planSum/planPrompt
   caches plus the progressFor fallback the tab bar reads.
   programPanel(onlyStates, bare): the optional state list keeps rows in
   those lifecycle states; bare skips the outer card and head for embedding
   (the Project home wraps it in its own section).
   window.DashProgram mounts the panel for embedding.
   Reads: state.wss, state.snap, state.plan; window.ADT.
   Exports (on window.AdPage.cards, also kept as plain globals):
   programPanel, wsCards, wsCard, planProg, loadPlanSums, progressFor;
   planSum and planPrompt are shared by accessor (reassigned on load).
   Needs: el, api, shown, wsNum, go, openPlan, copyBtn, renderTabs, fmtN,
   fmtUsd, ADT (plain globals). */
(function (global) {
  "use strict";
  /* Tab progress comes from the /api/workstreams row (the same progress
     object /api/plan carries), falling back to the per-plan sums the
     "Where we are" panel loads. No plan data means not completed. */
  function progressFor(id) {
    var hit = null;
    (state.wss || []).forEach(function (w) {
      if (String(w.ws).toUpperCase() === String(id).toUpperCase()) hit = w.progress || null;
    });
    if (hit) return hit;
    return planSum[id] || null;
  }
  var planSum = {}, planPrompt = {};
  /* Plan progress is computed once on the server (/api/plan progress object);
     the page only reads progress.pct, progress.done and progress.total. */
  function planProg(w, p) {
    var pr = (p && p.progress) || {done: 0, total: 0, counts: {}, pct: 0};
    pr.counts = pr.counts || {};
    return pr;
  }
  /* One in-flight plan-sum load at a time: concurrent drawMain calls share
     it instead of stacking fetches, and an empty result (a new dashboard
     with no plans yet) still settles the promise so callers never retry
     in a tight loop. */
  var planSumInflight = null;
  function loadPlanSums() {
    if (planSumInflight) return planSumInflight;
    planSumInflight = Promise.all(shown(state.wss).map(function (w) {
      return api("/api/plan", {ws: w.ws}).then(function (p) {
        planSum[w.ws] = planProg(w, p); planPrompt[w.ws] = p && p.start_prompt;
      }).catch(function () {});
    })).then(function () { renderTabs(); }).then(function () { planSumInflight = null; }, function () { planSumInflight = null; });
    return planSumInflight;
  }
  /* Bar segments, in paint order: done green, to-merge amber,
     merged-waiting-on-prod magenta, working-now blue. */
  function progSegs(bar, pc, tot) {
    [["done", "var(--good)"], ["pr", "var(--warning)"], ["prog", "var(--s5)"], ["wip", "var(--s1)"]].forEach(function (seg) {
      if (!pc[seg[0]] || !tot) return;
      var sp = el("span");
      sp.style.width = (100 * pc[seg[0]] / tot) + "%";
      sp.style.background = seg[1];
      bar.appendChild(sp);
    });
  }
  /* Tokens/cost for a row come from the same snapshot the tiles read, so a
     row shows exactly what that workstream's own tab shows. */
  function wsUsage(id) {
    var hit = null;
    ((state.snap && state.snap.by_workstream) || []).forEach(function (r) { if (r.ws === id) hit = r; });
    return hit;
  }
  /* A row has activity when any run, token, or live-agent evidence exists.
     Planned rows with activity are the first-agent case: the state file
     still says planned, but work already started. */
  function hasActivity(w) {
    return !!(w && (w.live_agents || w.last_activity || w.tokens || w.cost_usd || w.finished_in_range || w.finished_today));
  }
  function firstRunAt(ws) {
    var best = null;
    ((state.runs) || []).forEach(function (r) {
      if (!r || r.workstream !== ws) return;
      var t = +r.started_at || +r.ended_at || 0;
      if (t > 0 && (best == null || t < best)) best = t;
    });
    return best;
  }
  function readyToArchive(w, pr) {
    if (!pr || !ADT.isCompleted(pr)) return false;
    var st = ADT.wsStateOf(w);
    return st === "active" || st === "paused" || st === "done";
  }
  /* "Where we are": one two-line row per workstream. First line: id, name
     and the right-aligned %; the status pill sits under the name. The
     segmented bar carries the done-of-total value in its accessible label;
     the secondary line names tokens, cost and live agents. The optional
     onlyStates list keeps rows in those lifecycle states (the Project
     home passes active/paused, Recently done passes done); without it
     every row renders, as before. A Current filter (active/paused) also
     keeps planned rows with activity, shown with a first-agent hint. */
  function programPanel(only, bare) {
    var c = el("div", {class: bare ? "program bare" : "card program", "data-nav": "Where we are"});
    if (!bare) c.appendChild(el("div", {class: "prog-h"}, [el("h2", {text: "Where we are"}), el("span", {class: "sub", text: "Plan progress per workstream \u00B7 deferred tasks leave every count"})]));
    var key = el("div", {class: "prog-legend", role: "note", "aria-label": "Progress bar key"});
    [["sw-done", "Done"], ["sw-pr", "To merge"], ["sw-prog", "On prod"], ["sw-wip", "Working"]].forEach(function (k) {
      key.appendChild(el("span", {class: "prog-key"}, [el("i", {class: "sw " + k[0]}), document.createTextNode(k[1])]));
    });
    c.appendChild(key);
    var rows = (state.wss || []).slice().sort(function (x, y) { return wsNum(x) - wsNum(y); });
    if (only) rows = rows.filter(function (w) {
      var st = ADT.wsStateOf(w);
      if (only.indexOf(st) >= 0) return true;
      if (st === "planned" && only.indexOf("active") >= 0 && hasActivity(w)) return true;
      return false;
    });
    var all = {done: 0, tot: 0, live: 0};
    rows.forEach(function (w) { all.live += w.live_agents || 0; });
    var cap = 10, expanded = programPanel._expanded || {};
    var capKey = (bare ? "bare-" : "full-") + (only || []).join(",");
    var showAll = !!expanded[capKey];
    var visible = (rows.length > cap && !showAll) ? rows.slice(0, cap) : rows;
    visible.forEach(function (w) {
      var pr = planSum[w.ws] || w.progress || null;
      var pc = pr ? pr.counts : {}, tot = pr ? pr.total : 0, pct = pr ? pr.pct : 0, done = pr ? pr.done : 0;
      if (pr) { all.done += done; all.tot += tot; }
      /* Status is colour plus symbol plus word, never colour alone. The
         shared completed rule (server progress object) decides Done; a
         paused workstream keeps its muted pill with glyph plus word even
         before its plan loads. */
      var st = ADT.isCompleted(pr) ? ["done", "\u2713 Done"] : ADT.wsStateOf(w) === "paused" ? ["paused", "\u23F8 Paused"] : !pr ? ["none", "No plan"] : (done + (pc.wip || 0) + (pc.prog || 0) + (pc.pr || 0) || w.live_agents) ? ["active", "\u25CF In progress"] : ["todo", "\u25CB Not started"];
      var use = wsUsage(w.ws);
      var tok = use ? use.tokens : w.tokens, cost = use ? use.cost_usd : w.cost_usd;
      var row = el("div", {class: "prog-row s-" + st[0]});
      var name = el("div", {class: "prog-name"});
      var idline = el("div", {class: "prog-idline"});
      idline.appendChild(el("b", {text: w.ws}));
      idline.appendChild(el("span", {text: w.name || ""}));
      name.appendChild(idline);
      name.appendChild(el("span", {class: "prog-pill " + st[0], text: st[1]}));
      if (ADT.wsStateOf(w) === "planned" && hasActivity(w)) {
        var first = firstRunAt(w.ws) || w.last_activity;
        if (first) name.appendChild(el("span", {class: "prog-hint", text: "first agent started " + fmtDate(first), title: "Runs exist but the bundle state is still planned — activate with: python3 agent_dash.py ws state " + w.ws + " active"}));
      }
      if (readyToArchive(w, pr)) name.appendChild(el("span", {class: "prog-pill ready", text: "\u2713 Ready to archive", title: "Plan is 100% — archive after the closing report"}));
      row.appendChild(name);
      var bar = el("div", {class: "prog-bar", role: "img", "aria-label": tot ? (w.ws + " " + done + " of " + tot + " tasks done (" + pct + "%)") : (w.ws + " has no plan")});
      if (tot) {
        progSegs(bar, pc, tot);
        bar.title = "Done " + done + " \u00B7 to merge " + (pc.pr || 0) + " \u00B7 on prod " + (pc.prog || 0) + " \u00B7 working " + (pc.wip || 0) + " \u00B7 not started " + (pc.todo || 0) + " of " + tot + " tasks";
      }
      row.appendChild(bar);
      row.appendChild(el("span", {class: "prog-pct", text: tot ? pct + "%" : "\u2014"}));
      /* The done-of-total value reads bold first, like the mockup; the
         rest of the secondary line stays muted mono. */
      var meta = el("span", {class: "prog-meta"});
      if (tot) {
        meta.appendChild(el("b", {text: done + "/" + tot}));
        meta.appendChild(document.createTextNode(" tasks \u00B7 " + (pc.pr || 0) + " to merge \u00B7 \u25CF " + (w.live_agents || 0) + " live \u00B7 " + fmtN(tok) + " \u00B7 " + fmtUsd(cost)));
      } else {
        meta.textContent = "\u25CF " + (w.live_agents || 0) + " live \u00B7 " + fmtN(tok) + " tokens \u00B7 " + fmtUsd(cost);
      }
      row.appendChild(meta);
      var acts = el("div", {class: "prog-acts"});
      var bp = el("button", {text: "Plan"}); bp.addEventListener("click", function () { openPlan(w.ws); }); acts.appendChild(bp);
      var bl = el("button", {text: "Live"}); bl.addEventListener("click", function () { if (window.DashNav) window.DashNav.openExec(w.ws); }); acts.appendChild(bl);
      /* The management-table primary (Mark done, Start, Resume path;
         Archive already renders beside it via archiveControl, so that
         one is skipped here). Guarded: nothing changes when the manage
         module is absent. */
      if (typeof primaryFor === "function" && typeof runDescriptor === "function" && typeof manageEnabled === "function") {
        (function (row, rowPr) {
          var prow = {ws: row.ws, name: row.name, state: ADT.wsStateOf(row), progress: rowPr, allowed_actions: row.allowed_actions || []};
          var desc = null;
          try { desc = primaryFor(prow); } catch (e) { desc = null; }
          if (!desc || desc.action === "archive") return;
          var tag = row.ws + (row.name ? " (" + row.name + ")" : "");
          var prim = el("button", {type: "button", class: "arch-btn", text: desc.label});
          prim.setAttribute("aria-label", desc.label + " " + tag);
          prim.title = desc.title;
          prim.addEventListener("click", function (ev) { ev.stopPropagation(); runDescriptor(desc, row.ws, tag, acts, function () {}); });
          acts.appendChild(prim);
        })(w, pr);
      }
      if (readyToArchive(w, pr)) {
        /* Real archive button when the server allows writes, else the
           copy-command button. The control names the workstream in its
           aria-label and disables itself while the POST is in flight. */
        if (typeof archiveControl === "function") acts.appendChild(archiveControl(w.ws, w.name));
        else {
          var ab = el("button", {type: "button", class: "pj-copy", title: "Copy the exact command (read-only: nothing runs)", text: "Archive"});
          (function (id) {
            ab.addEventListener("click", function (ev) {
              ev.stopPropagation();
              var cmd = "python3 agent_dash.py ws archive " + id;
              if (typeof copyCmd === "function") copyCmd(cmd);
              else if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(cmd);
            });
          })(w.ws);
          acts.appendChild(ab);
        }
      }
      if (planPrompt[w.ws]) acts.appendChild(copyBtn(planPrompt[w.ws]));
      row.appendChild(acts);
      c.appendChild(row);
    });
    if (rows.length > cap) {
      var toggle = el("button", {type: "button", class: "prog-more", text: showAll ? "Show less" : "Show " + (rows.length - cap) + " more", title: showAll ? "Collapse the list" : "Show the full list"});
      toggle.addEventListener("click", function () { expanded[capKey] = !showAll; programPanel._expanded = expanded; drawMain(); });
      c.appendChild(toggle);
    }
    /* Overall hero: summed server counts plus live agents. No % here: plan %
       is computed once on the server and the page never derives one from
       task counts itself. */
    var overall = el("div", {class: "prog-total prog-overall"});
    if (all.tot) {
      overall.appendChild(el("span", {class: "pct", text: all.done + " of " + all.tot}));
      var txt = el("span", {class: "txt"});
      txt.appendChild(el("b", {text: "tasks done"}));
      txt.appendChild(document.createTextNode(" across " + Object.keys(planSum).length + " workstreams \u00B7 "));
      txt.appendChild(el("b", {text: all.live + " agents live"}));
      overall.appendChild(txt);
    } else {
      overall.appendChild(el("span", {class: "txt", text: "Loading workstream plans\u2026"}));
      c.setAttribute("aria-busy", "true");
    }
    c.insertBefore(overall, key.nextSibling);
    if (!Object.keys(planSum).length) c.appendChild(el("div", {class: "skel"}));
    return c;
  }
  window.DashProgram = {mount: function (host) {
    var draw = function () { host.innerHTML = ""; host.appendChild(programPanel()); };
    draw();
    var go2 = function () { (state.wss.length ? Promise.resolve() : api("/api/workstreams").then(function (j) { state.wss = j.workstreams || []; })).then(loadPlanSums).then(draw).catch(function () {}); };
    go2();
    clearInterval(window.__adProgT); window.__adProgT = setInterval(function () { if (document.body.contains(host)) go2(); else clearInterval(window.__adProgT); }, 30000);
  }};
  function wsCard(id, name, live, tokens, cost, pr, clickable) {
    var pc = (pr && pr.counts) || {}, tot = (pr && pr.total) || 0, pct = (pr && pr.pct) || 0, done = (pr && pr.done) || 0;
    var attrs = {class: "card wsc", "data-wsc": id, "data-ws": "all", "data-hours": "24", "data-tokens": String(tokens || 0), "data-cost-usd": String(cost || 0)};
    if (clickable) {
      attrs.role = "button";
      attrs.tabindex = "0";
      attrs["aria-label"] = "Open the " + id + " execution tab";
      attrs.onclick = function () { go(id); };
      attrs.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(id); }
      };
    }
    var card = el("div", attrs);
    var top = el("div", {class: "top"}, [el("span", {class: "id", text: id}), el("span", {class: "st " + (live ? "running" : "done")}, [el("i"), live ? live + " running" : "idle"])]);
    card.appendChild(top); card.appendChild(el("div", {class: "wsc-name", text: name || ""}));
    var bar = el("div", {class: "bar", role: "img", "aria-label": done + " of " + tot + " plan tasks done"});
    if (tot) {
      progSegs(bar, pc, tot);
      bar.title = "Done " + done + " \u00B7 to merge " + (pc.pr || 0) + " \u00B7 on prod " + (pc.prog || 0) + " \u00B7 working " + (pc.wip || 0) + " of " + tot + " tasks";
    }
    card.appendChild(bar);
    var meta = el("div", {class: "wsc-meta"});
    meta.appendChild(el("div", {class: "wsc-plan", title: tot ? ("From the workstream plan: " + done + " done, " + (pc.prog || 0) + " merged waiting on prod, " + (pc.pr || 0) + " waiting to merge, " + (pc.todo || 0) + " not started" + (pc.deferred ? ", " + pc.deferred + " deferred (not counted)" : "")) : "No plan for this workstream", text: tot ? (done + "/" + tot + " tasks (" + pct + "%)") : "No plan"}));
    meta.appendChild(el("div", {class: "wsc-use", text: fmtN(tokens) + " tokens \u00B7 " + fmtUsd(cost)}));
    card.appendChild(meta);
    return card;
  }
  /* Overview per-workstream figures come from the same snapshot the tiles read,
     so each card shows exactly what that workstream's own tab shows. */
  function wsCards() {
    var c = el("div", {class: "grid g-tiles"});
    var byWs = {};
    ((state.snap && state.snap.by_workstream) || []).forEach(function (r) { byWs[r.ws] = r; });
    shown(state.wss).forEach(function (w) {
      if (!ADT.isTabState(ADT.wsStateOf(w))) return;
      var hit = byWs[w.ws];
      c.appendChild(wsCard(w.ws, w.name, w.live_agents, hit ? hit.tokens : w.tokens, hit ? hit.cost_usd : w.cost_usd, planSum[w.ws], true));
    });
    /* Runs with no workstream: the overview total visibly equals the sum. */
    (state.wss || []).forEach(function (w) {
      if (w.ws === "unassigned") {
        var hit2 = byWs[w.ws];
        c.appendChild(wsCard("Unassigned", "runs with no workstream", w.live_agents, hit2 ? hit2.tokens : w.tokens, hit2 ? hit2.cost_usd : w.cost_usd, null, false));
      }
    });
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.programPanel = programPanel;
  AdPage.cards.wsCards = wsCards;
  global.progressFor = progressFor;
  global.planProg = planProg;
  global.loadPlanSums = loadPlanSums;
  global.programPanel = programPanel;
  global.wsCard = wsCard;
  global.wsCards = wsCards;
  Object.defineProperty(global, "planSum", {
    configurable: true,
    get: function () { return planSum; },
    set: function (v) { planSum = v; }
  });
  Object.defineProperty(AdPage, "planSum", {
    configurable: true,
    get: function () { return planSum; },
    set: function (v) { planSum = v; }
  });
  Object.defineProperty(global, "planPrompt", {
    configurable: true,
    get: function () { return planPrompt; },
    set: function (v) { planPrompt = v; }
  });
  Object.defineProperty(AdPage, "planPrompt", {
    configurable: true,
    get: function () { return planPrompt; },
    set: function (v) { planPrompt = v; }
  });
})(window);
