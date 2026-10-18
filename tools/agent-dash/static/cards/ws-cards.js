/* Page card: Where we are, workstream cards, Overview footer data.
   Renders the program panel (one row per workstream with server plan
   progress), the per-workstream cards on the overview, and owns the
   planSum/planPrompt caches plus the progressFor fallback the tab bar
   reads. window.DashProgram mounts the panel for embedding.
   Reads: state.wss, state.snap, state.plan; window.ADT.
   Exports (on window.AdPage.cards, also kept as plain globals):
   programPanel, wsCards, wsCard, planProg, loadPlanSums, progressFor;
   planSum and planPrompt are shared by accessor (reassigned on load).
   Needs: el, api, shown, go, openPlan, copyBtn, renderTabs, fmtN, fmtUsd,
   ADT (plain globals). */
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
  function loadPlanSums() {
    return Promise.all(shown(state.wss).map(function (w) {
      return api("/api/plan", {ws: w.ws}).then(function (p) {
        planSum[w.ws] = planProg(w, p); planPrompt[w.ws] = p && p.start_prompt;
      }).catch(function () {});
    })).then(function () { renderTabs(); });
  }
  /* "Where we are": one row per workstream with status, % done from its plan, live agents and quick actions. Top of both Overview pages. */
  function programPanel() {
    var c = el("div", {class: "card program", "data-nav": "Where we are"});
    c.appendChild(el("div", {class: "prog-h"}, [el("h2", {text: "Where we are"}), el("span", {class: "sub", text: "Every workstream's plan, live. % done counts merged tasks; deferred tasks are left out."})]));
    var all = {done: 0, tot: 0};
    shown(state.wss).forEach(function (w) {
      var pr = planSum[w.ws]; if (!pr) return;
      var pc = pr.counts, tot = pr.total, pct = pr.pct;
      all.done += pr.done; all.tot += tot;
      var st = ADT.isCompleted(pr) ? ["done", "Done \u2713"] : (pr.done + (pc.wip || 0) + (pc.prog || 0) + (pc.pr || 0) || w.live_agents) ? ["active", "In progress"] : ["todo", "Not started"];
      var row = el("div", {class: "prog-row s-" + st[0]});
      row.appendChild(el("div", {class: "prog-name"}, [el("b", {text: w.ws}), el("span", {text: w.name || ""})]));
      row.appendChild(el("span", {class: "prog-pill " + st[0], text: st[1]}));
      var bar = el("div", {class: "prog-bar", role: "img", "aria-label": w.ws + " " + pct + "% done"});
      [["done", "var(--good)"], ["prog", "var(--s5)"], ["pr", "var(--warning)"]].forEach(function (p) { if (!pc[p[0]] || !tot) return; var sp = el("span"); sp.style.width = (100 * pc[p[0]] / tot) + "%"; sp.style.background = p[1]; bar.appendChild(sp); });
      row.appendChild(bar);
      row.appendChild(el("b", {class: "prog-pct", text: pct + "%"}));
      row.appendChild(el("span", {class: "prog-meta", text: pr.done + "/" + tot + " tasks" + (pc.wip ? " \u00B7 " + pc.wip + " in progress" : "") + (pc.prog ? " \u00B7 " + pc.prog + " on prod" : "") + (pc.pr ? " \u00B7 " + pc.pr + " to merge" : "") + (w.live_agents ? " \u00B7 \u25CF " + w.live_agents + " live" : "")}));
      var acts = el("div", {class: "prog-acts"});
      var bp = el("button", {text: "Plan"}); bp.addEventListener("click", function () { openPlan(w.ws); }); acts.appendChild(bp);
      var bl = el("button", {text: "Live"}); bl.addEventListener("click", function () { if (window.DashNav) window.DashNav.openExec(w.ws); }); acts.appendChild(bl);
      if (planPrompt[w.ws]) acts.appendChild(copyBtn(planPrompt[w.ws]));
      row.appendChild(acts);
      c.appendChild(row);
    });
    if (all.tot) c.insertBefore(el("div", {class: "prog-total", text: "Overall: " + all.done + " of " + all.tot + " tasks done across " + Object.keys(planSum).length + " workstreams"}), c.children[1]);
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
    if (clickable) attrs.onclick = function () { go(id); };
    var card = el("div", attrs);
    var top = el("div", {class: "top"}, [el("span", {class: "id", text: id}), el("span", {class: "st " + (live ? "running" : "done")}, [el("i"), live ? live + " running" : "idle"])]);
    card.appendChild(top); card.appendChild(el("div", {style: "font-size:13px;color:var(--ink-2)", text: name || ""}));
    var bar = el("div", {class: "bar", role: "img", "aria-label": done + " of " + tot + " plan tasks done"});
    [["done", "var(--good)"], ["prog", "var(--s5)"], ["pr", "var(--warning)"]].forEach(function (p) { if (!pc[p[0]] || !tot) return; var sp = el("span"); sp.style.width = (100 * pc[p[0]] / tot) + "%"; sp.style.background = p[1]; bar.appendChild(sp); });
    card.appendChild(bar);
    card.appendChild(el("div", {style: "font:12px var(--mono);color:var(--muted)", title: "From the workstream plan: " + done + " done, " + (pc.prog || 0) + " merged waiting on prod, " + (pc.pr || 0) + " waiting to merge, " + (pc.todo || 0) + " not started" + (pc.deferred ? ", " + pc.deferred + " deferred (not counted)" : ""), text: done + "/" + tot + " tasks (" + pct + "%) \u00B7 " + fmtN(tokens) + " tok \u00B7 " + fmtUsd(cost)}));
    return card;
  }
  /* Overview per-workstream figures come from the same snapshot the tiles read,
     so each card shows exactly what that workstream's own tab shows. */
  function wsCards() {
    var c = el("div", {class: "grid g-tiles"});
    var byWs = {};
    ((state.snap && state.snap.by_workstream) || []).forEach(function (r) { byWs[r.ws] = r; });
    shown(state.wss).forEach(function (w) {
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
