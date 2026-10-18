/* Page card: Planned view (?view=planned).
   Start-readiness per planned workstream (depends-on, gate freshness with
   stale markers, approvals pending, tasks, Ready pill), the "Gated but
   live" section for active workstreams held by an unmet owner/sign-off
   gate, and the slot mount point for the dependency chart.
   Gate freshness mirrors dash/orchestrate.py stale_gates: a gate is STALE
   when fresh_hours is set and confirmed_at is missing or older than that
   many hours; gates without fresh_hours never go stale. Planned
   workstreams' plan pages stay reachable through the Plan buttons.
   Reads: state.wss, state.wssAll, state.plans, state.approvals.
   Exports (on window.AdPage.cards, also kept as plain globals):
   plannedSpecs, readinessOf, gateStale, gatedLive, PLANNED_SLOTS.
   Needs: el, fmtN, openPlan, keyOf, registryFor (plain globals). */
(function (global) {
  "use strict";
  /* Slot mount points for the dependency chart (shared with the
     project home, which also mounts it). */
  var PLANNED_SLOTS = ["exec-depgraph"];

  /* Pure: one gate task's staleness at nowS (epoch seconds). */
  function gateStale(task, nowS) {
    var fresh = task && task.fresh_hours;
    if (typeof fresh !== "number" || !(fresh > 0)) return false;
    var confirmed = Date.parse(task.confirmed_at || "") / 1000;
    if (!isFinite(confirmed)) return true;
    return (nowS - confirmed) > fresh * 3600;
  }

  /* Pure: start-readiness for one planned row. plan is the /api/plan body
     (or null), approvalsAll the ?ws=all blocks, statesById the upper-cased
     id to lifecycle-state map, nowS epoch seconds. */
  function readinessOf(row, plan, approvalsAll, statesById, nowS) {
    var deps = row.depends_on || [];
    var unmetDeps = deps.filter(function (d) {
      var s = (statesById || {})[String(d).toUpperCase()] || "active";
      return s !== "done" && s !== "archived";
    });
    var gates = [];
    ((plan && plan.groups) || []).forEach(function (g) {
      (g.tasks || []).forEach(function (t) { if (t && t.kind === "gate") gates.push(t); });
    });
    var unmetGates = gates.filter(function (t) { return t.status !== "done"; });
    var staleGates = unmetGates.filter(function (t) { return gateStale(t, nowS); });
    var pending = 0;
    (approvalsAll || []).forEach(function (b) {
      if (!b || b.ws !== row.ws) return;
      (b.items || []).forEach(function (it) { if (it && it.status === "pending") pending++; });
    });
    var pr = row.progress || {done: 0, total: 0};
    var ready = !unmetDeps.length && !unmetGates.length && !pending;
    var reason = ready ? "dependencies met, gates met, no approvals pending"
      : unmetDeps.length ? "waiting on " + unmetDeps.join(", ")
      : staleGates.length ? staleGates.length + " stale " + (staleGates.length === 1 ? "gate" : "gates")
      : unmetGates.length ? unmetGates.length + " unmet " + (unmetGates.length === 1 ? "gate" : "gates")
      : pending + " " + (pending === 1 ? "approval" : "approvals") + " pending";
    var depText = deps.length ? deps.join(", ") : "none";
    if (unmetDeps.length) depText += " (" + unmetDeps.length + " unmet)";
    var gateText = gates.length
      ? gates.length + (gates.length === 1 ? " gate" : " gates") + (staleGates.length ? ", " + staleGates.length + " STALE" : ", all fresh")
      : "none";
    return {
      depends: deps, unmetDeps: unmetDeps, depText: depText,
      gates: gates.length, unmetGates: unmetGates.length, staleGates: staleGates.length, gateText: gateText,
      gateTitle: gates.map(function (t) { return (t.id || "gate") + " — " + (t.status === "done" ? "\u2713 met" : gateStale(t, nowS) ? "STALE" : "waiting"); }).join("\n"),
      pending: pending, done: pr.done || 0, total: pr.total || 0,
      ready: ready, reason: reason,
      waitText: deps.length ? "waits on: " + deps.join(", ") : "waits on: nothing — " + reason
    };
  }

  /* Pure: active rows held by an unmet owner/sign-off gate. rows are the
     workstream rows to scan, plansByWs the /api/plan bodies by id. */
  function gatedLive(rows, plansByWs, nowS) {
    var out = [];
    (rows || []).forEach(function (row) {
      var plan = (plansByWs || {})[row.ws];
      var held = [];
      ((plan && plan.groups) || []).forEach(function (g) {
        (g.tasks || []).forEach(function (t) {
          if (!t || t.kind !== "gate" || t.status === "done") return;
          var sig = [t.signal, t.title, t.step, t.id].join(" ");
          if (/owner|sign-?off/i.test(sig)) held.push(t);
        });
      });
      if (held.length) out.push({ws: row.ws, name: row.name || "", gates: held, progress: row.progress || null});
    });
    return out;
  }

  function secHead(title, sub) {
    var h = el("div", {class: "pj-head"});
    h.appendChild(el("h2", {text: title}));
    if (sub) h.appendChild(el("p", {class: "sub", text: sub}));
    return h;
  }

  function plannedShelfRows() {
    var seen = {}, out = [];
    (state.wss || []).forEach(function (w) {
      var key = String(w.ws).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      if (ADT.wsStateOf(w) === "planned") out.push(w);
    });
    (state.bundles || []).forEach(function (b) {
      if (!b || b.kind === "program" || b.id === "OVERVIEW") return;
      var key = String(b.id).toUpperCase();
      if (seen[key]) return;
      if ((b.state || "active") !== "planned") return;
      seen[key] = 1;
      out.push({ws: b.id, name: b.title || "", progress: null, depends_on: b.depends_on || [], state: "planned"});
    });
    return out.slice().sort(function (x, y) { return wsNum(x) - wsNum(y); });
  }
  function readinessTable() {
    var c = el("div", {class: "card pj-ready", "data-nav": "Start readiness"});
    c.appendChild(secHead("Start readiness", "Can we start it? Dependencies, gates, and approvals per planned workstream."));
    var rows = plannedShelfRows();
    if (!rows.length) {
      c.appendChild(el("p", {class: "pj-note", text: "No planned workstreams: every bundle has a tab."}));
      return c;
    }
    var states = {};
    (state.wssAll && state.wssAll.length ? state.wssAll : state.wss || []).forEach(function (w) { states[String(w.ws).toUpperCase()] = ADT.wsStateOf(w); });
    (state.bundles || []).forEach(function (b) { if (b && b.id && !states[String(b.id).toUpperCase()]) states[String(b.id).toUpperCase()] = b.state || "active"; });
    var nowS = Date.now() / 1000;
    var table = el("table", {class: "pj-table", "aria-label": "Start readiness"});
    var head = el("tr");
    ["WS", "Depends on", "Gates", "Approvals", "Tasks", "Ready?", "Action"].forEach(function (t, i) {
      head.appendChild(el("th", {class: i === 4 ? "n" : "", text: t}));
    });
    table.appendChild(el("thead", {}, [head]));
    var body = el("tbody");
    rows.forEach(function (w) {
      var r = readinessOf(w, state.plans[w.ws], state.approvals, states, nowS);
      var tr = el("tr");
      var wsCell = el("td");
      wsCell.appendChild(el("b", {text: w.ws}));
      if (w.name) wsCell.appendChild(document.createTextNode(" " + w.name));
      tr.appendChild(wsCell);
      tr.appendChild(el("td", {text: r.depText}));
      tr.appendChild(el("td", {text: r.gateText, title: r.gateTitle || r.gateText}));
      tr.appendChild(el("td", {text: r.pending ? r.pending + " pending" : "none"}));
      tr.appendChild(el("td", {class: "n", text: r.done + "/" + r.total}));
      var readyCell = el("td");
      readyCell.appendChild(el("span", {class: "pj-pill " + (r.ready ? "ok" : "wait"), text: r.ready ? "\u2713 Ready" : "\u25CB Not yet", title: r.reason}));
      readyCell.appendChild(document.createTextNode(" " + r.reason));
      tr.appendChild(readyCell);
      /* Start lives beside readiness: the same primary the management
         table offers for planned rows. Guarded: no cell content when
         the manage module is absent. */
      var actCell = el("td");
      if (typeof primaryFor === "function" && typeof runDescriptor === "function") {
        (function (cell, rowW) {
          var full = null;
          ((state.wssAll || []).concat(state.wss || [])).forEach(function (x) {
            if (String(x.ws).toUpperCase() === String(rowW.ws).toUpperCase()) full = x;
          });
          var prow = {ws: rowW.ws, name: rowW.name || "", state: (full && ADT.wsStateOf(full)) || "planned",
            progress: (full && full.progress) || rowW.progress || null, allowed_actions: (full && full.allowed_actions) || []};
          var desc = null;
          try { desc = primaryFor(prow); } catch (e) { desc = null; }
          if (desc) {
            var tag = rowW.ws + (rowW.name ? " (" + rowW.name + ")" : "");
            var sb = el("button", {type: "button", class: "arch-btn", text: desc.label});
            sb.setAttribute("aria-label", desc.label + " " + tag);
            sb.title = desc.title;
            sb.addEventListener("click", function (ev) { ev.stopPropagation(); runDescriptor(desc, rowW.ws, tag, cell, function () {}); });
            cell.appendChild(sb);
          } else {
            cell.appendChild(document.createTextNode("\u2014"));
          }
        })(actCell, w);
      } else {
        actCell.appendChild(document.createTextNode("\u2014"));
      }
      tr.appendChild(actCell);
      body.appendChild(tr);
    });
    table.appendChild(body);
    c.appendChild(table);
    var acts = el("div", {class: "prog-acts"});
    rows.forEach(function (w) {
      var bp = el("button", {type: "button", text: "Plan " + w.ws, title: "Open the " + w.ws + " plan page"});
      bp.addEventListener("click", function () { openPlan(w.ws); });
      acts.appendChild(bp);
    });
    c.appendChild(acts);
    return c;
  }

  function gatedLiveSection() {
    var actives = (state.wss || []).filter(function (w) { return ADT.wsStateOf(w) === "active"; });
    var held = gatedLive(actives, state.plans, Date.now() / 1000);
    if (!held.length) return null;
    var c = el("div", {class: "card pj-gated", "data-nav": "Gated but live"});
    c.appendChild(secHead("Gated but live", "Active with an unmet owner or sign-off gate — the gate holds the work."));
    held.forEach(function (h) {
      var pr = h.progress || {done: 0, total: 0, pct: 0};
      var row = el("div", {class: "prog-row s-active"});
      var name = el("div", {class: "prog-name"});
      var idline = el("div", {class: "prog-idline"});
      idline.appendChild(el("b", {text: h.ws}));
      idline.appendChild(el("span", {text: h.name}));
      name.appendChild(idline);
      name.appendChild(el("span", {class: "prog-pill active", text: "\u25CF Gated"}));
      row.appendChild(name);
      var bar = el("div", {class: "prog-bar", role: "img", "aria-label": h.ws + " " + (pr.done || 0) + " of " + (pr.total || 0) + " tasks done"});
      if (pr.total) {
        var fill = el("span");
        fill.style.width = (100 * (pr.done || 0) / pr.total) + "%";
        fill.style.background = "var(--warning)";
        bar.appendChild(fill);
      }
      row.appendChild(bar);
      row.appendChild(el("span", {class: "prog-pct", text: pr.total ? (pr.pct || 0) + "%" : "\u2014"}));
      var meta = el("span", {class: "prog-meta"});
      meta.appendChild(el("b", {text: (pr.done || 0) + "/" + (pr.total || 0)}));
      meta.appendChild(document.createTextNode(" tasks \u00B7 held by " + h.gates.map(function (g) { return g.signal || g.title || g.id; }).join("; ")));
      row.appendChild(meta);
      var acts = el("div", {class: "prog-acts"});
      var bp = el("button", {type: "button", text: "Plan"});
      bp.addEventListener("click", function () { openPlan(h.ws); });
      acts.appendChild(bp);
      row.appendChild(acts);
      c.appendChild(row);
    });
    return c;
  }

  /* The slot key folds in the entry's own spec key (loading, failed, or
     data state); otherwise a card whose data lands after the slot mounts
     never rebuilds on its own completion redraw. Mirrors project-home. */
  function slotSection(id, entry) {
    function innerKey() {
      if (!entry) return null;
      try {
        var sp = entry.spec(state, state.snap || {});
        return (sp && sp[1] !== undefined) ? sp[1] : null;
      } catch (e) { return null; }
    }
    return [id, keyOf([id, entry ? entry.id : null, state.seq, innerKey()]), function () {
      var host = el("div", {class: "pj-slot", id: id, "data-slot": id});
      if (entry) {
        var sp = entry.spec(state, state.snap || {});
        if (sp && sp[2]) {
          var inner = sp[2]();
          if (inner) host.appendChild(inner);
        }
      }
      return host;
    }, ""];
  }

  /* Spec list for drawMain: readiness table, gated-but-live, chart slots,
     then any other scope-"planned" registry entry as its own section. */
  function plannedSpecs(passedState, snap) {
    var specs = [];
    var entries = (typeof registryFor === "function" ? registryFor("planned") : []) || [];
    var bySlot = {};
    entries.forEach(function (e) { if (e.slot) bySlot[e.slot] = e; });
    specs.push(slotSection("exec-depgraph", bySlot["exec-depgraph"] || null));
    specs.push(["planned-ready", keyOf([(state.wss || []).map(function (w) { return [w.ws, w.progress, w.depends_on]; }), state.bundles, state.plans, state.approvals]), function () { return readinessTable(); }, ""]);
    specs.push(["planned-gated", keyOf([(state.wss || []).map(function (w) { return [w.ws, w.progress]; }), state.plans]), function () {
      var node = gatedLiveSection();
      return node || el("div", {class: "pj-empty-gated"});
    }, ""]);
    PLANNED_SLOTS.forEach(function (id) { if (id !== "exec-depgraph") specs.push(slotSection(id, bySlot[id] || null)); });
    entries.forEach(function (e) {
      if (e.slot) return;
      specs.push([e.id, keyOf([e.id, state.seq]), function () {
        var sp = e.spec(state, snap);
        return (sp && sp[2]) ? sp[2]() : el("div");
      }, "margin-top:14px"]);
    });
    return specs;
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.planned = plannedSpecs;
  AdPage.PLANNED_SLOTS = PLANNED_SLOTS;
  global.plannedSpecs = plannedSpecs;
  global.readinessOf = readinessOf;
  global.gateStale = gateStale;
  global.gatedLive = gatedLive;
  global.PLANNED_SLOTS = PLANNED_SLOTS;
})(window);
