/* Page card: Project home (?view=project).
   The project landing per the mockups: phases strip from /api/project,
   the 24 h tiles (the shared tiles card, never a fork), Current
   workstreams (the Where-we-are rows for active/paused only, through the
   ws-cards helpers with a state filter), a Planned shelf, Recently done
   with Report links and clipboard Archive buttons, and empty slot
   containers where chart workers mount by scope (see registry.js).
   Reads: state.project, state.wss, state.wssAll, state.snap, state.archive,
   state.approvals, state.plans; planSum through programPanel.
   Exports (on window.AdPage.cards, also kept as plain globals):
   projectHomeSpecs, reportFor, showToast, copyCmd, PROJECT_SLOTS.
   Needs: el, fmtN, fmtUsd, fmtDur, tiles, programPanel, readinessOf,
   openPlan, keyOf, ADT (plain globals). */
(function (global) {
  "use strict";
  /* Slot mount points for chart cards. A registry entry with scope
     "project" and a slot naming one of these renders inside it; the rest
     render as their own sections after the slots, in registry order. */
  var PROJECT_SLOTS = ["exec-burnup", "exec-health", "exec-depgraph"];

  /* Pure: the closing-report basename the archive payload exposes for one
     id, or null. The payload only ever carries the basename, never a path
     or a body, so the Report link goes to the plan view. */
  function reportFor(id, archive) {
    var cards = (archive && archive.archive) || [];
    var want = String(id == null ? "" : id).toUpperCase();
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i] || {};
      if (String(card.id || card.ws || "").toUpperCase() === want) return card.report || null;
    }
    return null;
  }

  /* Confirmation toast for clipboard copies. One shared node, reused by
     the planned and archive cards. */
  function showToast(text) {
    var t = document.getElementById("toast");
    if (!t) {
      t = el("div", {id: "toast", role: "status"});
      document.body.appendChild(t);
    }
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(showToast._h);
    showToast._h = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }

  /* Read-only copy: the exact CLI command reaches the clipboard (with a
     textarea fallback) and a toast confirms. Never fetches, never writes. */
  function copyCmd(cmd) {
    var done = function () { showToast("Copied: " + cmd); };
    var fallback = function () {
      var ta = document.createElement("textarea");
      ta.value = cmd;
      ta.setAttribute("readonly", "readonly");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      try {
        document.execCommand("copy");
        done();
      } catch (e) {
        showToast("Copy failed — run this: " + cmd);
      }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(cmd).then(done, fallback);
    else fallback();
  }
  function copyBtn(cmd, label) {
    var b = el("button", {type: "button", class: "pj-copy", title: "Copy the exact command (read-only: nothing runs)", text: label});
    b.addEventListener("click", function (ev) { ev.stopPropagation(); copyCmd(cmd); });
    return b;
  }

  function secHead(title, sub) {
    var h = el("div", {class: "pj-head"});
    h.appendChild(el("h2", {text: title}));
    if (sub) h.appendChild(el("p", {class: "sub", text: sub}));
    return h;
  }

  /* Phases strip from /api/project phases (each already carries its
     rolled-up state). Status is colour plus symbol plus word. */
  function phasesSection(project) {
    var c = el("div", {class: "card pj-phases", "data-nav": "Phases"});
    c.appendChild(secHead("Phases", "From project.json — gate-based, no dates."));
    var phases = (project && project.phases) || [];
    if (!phases.length) {
      c.appendChild(el("p", {class: "pj-note", text: project && project.name ? "No phases listed in project.json yet." : "No project file yet: add project.json beside the bundles to name the project and its phases."}));
      return c;
    }
    var strip = el("div", {class: "pj-phase-strip"});
    phases.forEach(function (p) {
      var st = p.state || "planned";
      var mark = st === "done" ? "\u2713 Done" : st === "active" ? "\u25CF Active" : "\u25CB Planned";
      var d = el("div", {class: "pj-phase " + st, title: (p.title || p.id || "Phase") + " — " + mark});
      d.appendChild(el("div", {class: "pj-phase-name", text: (st === "done" ? "\u2713 " : st === "active" ? "\u25CF " : "\u25CB ") + (p.title || p.id || "Phase")}));
      if (p.summary) d.appendChild(el("div", {class: "pj-phase-goal", text: p.summary}));
      if (p.gate) d.appendChild(el("div", {class: "pj-phase-gate", text: "Gate: " + p.gate}));
      strip.appendChild(d);
    });
    c.appendChild(strip);
    return c;
  }

  /* The 24 h tiles, reused verbatim, plus the lifetime line (archived
     work stays in the lifetime totals by construction). */
  function tilesSection(snap) {
    var c = el("div", {class: "card pj-tiles", "data-nav": "Last 24 hours"});
    c.appendChild(secHead("Last 24 hours", "Rolling 24 h window, same fields as the execution tiles."));
    c.appendChild(tiles(snap));
    var all = (snap && snap.totals_all_time) || {};
    var line = "Lifetime " + fmtN(ADC.billed(all.tokens || {})) + " tokens \u00B7 " + fmtUsd(all.cost_usd || 0) + " — archived work included";
    c.appendChild(el("p", {class: "pj-note", text: line}));
    return c;
  }

  function wsState(row) { return ADT.wsStateOf(row); }
  function byWs(rows) {
    return (rows || []).slice().sort(function (x, y) { return wsNum(x) - wsNum(y); });
  }
  function statesById() {
    var map = {};
    (state.wssAll && state.wssAll.length ? state.wssAll : state.wss || []).forEach(function (w) { map[String(w.ws).toUpperCase()] = ADT.wsStateOf(w); });
    (state.bundles || []).forEach(function (b) {
      if (!b || !b.id) return;
      var key = String(b.id).toUpperCase();
      if (!map[key]) map[key] = b.state || "active";
    });
    return map;
  }
  function shelfRows(want) {
    var seen = {}, out = [];
    (state.wss || []).forEach(function (w) {
      var key = String(w.ws).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      if (wsState(w) === want) out.push(w);
    });
    (state.bundles || []).forEach(function (b) {
      if (!b || b.kind === "program" || b.id === "OVERVIEW") return;
      var key = String(b.id).toUpperCase();
      if (seen[key]) return;
      var st = b.state || "active";
      if (st !== want) return;
      seen[key] = 1;
      out.push({ws: b.id, name: b.title || "", progress: (typeof progressFor === "function" ? progressFor(b.id) : null), depends_on: b.depends_on || [], state: st});
    });
    return byWs(out);
  }
  function unplannedRows() {
    var have = {};
    (state.bundles || []).forEach(function (b) { if (b && b.id) have[String(b.id).toUpperCase()] = 1; });
    var out = [];
    (state.wss || []).forEach(function (w) {
      if (!w || w.ws === "unassigned") return;
      if (have[String(w.ws).toUpperCase()]) return;
      if (w.plan) return;
      var runs = (w.finished_in_range || 0) + (w.live_agents || 0);
      var active = runs > 0 || (w.tokens || 0) > 0 || (w.cost_usd || 0) > 0 || !!w.last_activity;
      if (!active) return;
      out.push(w);
    });
    return byWs(out);
  }
  function suggestTitle(id) {
    var up = String(id || "").toUpperCase();
    if (up === "TOOLS") return "Shared tooling";
    if (up === "PLANNING") return "Planning lane";
    return id + " work";
  }
  function taskCounts(row) {
    var pr = row.progress || {done: 0, total: 0};
    return {done: pr.done || 0, total: pr.total || 0};
  }

  /* Planned shelf: one card per planned workstream with its waits-on and
     its ready/not-yet pill (glyph plus word). Readiness comes from the
     shared planned helper, never recomputed here. */
  function plannedShelf() {
    var c = el("div", {class: "pj-shelf", "data-nav": "Planned"});
    var rows = shelfRows("planned");
    var states = statesById();
    var nowS = Date.now() / 1000;
    rows.forEach(function (w) {
      var r = readinessOf(w, state.plans[w.ws], state.approvals, states, nowS);
      var tc = taskCounts(w);
      var card = el("div", {class: "card pj-planned"});
      card.appendChild(el("div", {class: "pj-wn", text: w.ws}));
      card.appendChild(el("div", {class: "pj-wt", text: w.name || ""}));
      var bar = el("div", {class: "prog-bar", role: "img", "aria-label": w.ws + " " + tc.done + " of " + tc.total + " tasks done"});
      if (tc.total) {
        var fill = el("span");
        fill.style.width = (100 * tc.done / tc.total) + "%";
        fill.style.background = "var(--muted)";
        bar.appendChild(fill);
      }
      card.appendChild(bar);
      card.appendChild(el("div", {class: "pj-meta", text: tc.done + "/" + tc.total + " tasks"}));
      /* The pill-line reason below already names the wait (or its absence);
         a second waits line would repeat it. */
      var pill = el("span", {class: "pj-pill " + (r.ready ? "ok" : "wait"), text: r.ready ? "\u2713 Ready" : "\u25CB Not yet", title: r.reason});
      var line = el("div", {class: "pj-pill-line"});
      line.appendChild(pill);
      line.appendChild(el("span", {class: "pj-reason", text: r.reason}));
      card.appendChild(line);
      var acts = el("div", {class: "prog-acts"});
      var bp = el("button", {type: "button", text: "Plan"});
      bp.addEventListener("click", function () { openPlan(w.ws); });
      acts.appendChild(bp);
      card.appendChild(acts);
      c.appendChild(card);
    });
    if (!rows.length) c.appendChild(el("p", {class: "pj-note", text: "No planned workstreams: every bundle has a tab."}));
    return c;
  }

  /* Recently done: done but not archived, each with its tasks, tokens,
     cost, a Plan button, a Report link when the archive payload exposes
     the closing-report basename, and an Archive button that copies the
     exact CLI command (read-only, never runs it). */
  function doneRows() {
    var c = el("div", {class: "card pj-done", "data-nav": "Recently done"});
    c.appendChild(secHead("Recently done", "Done, not yet archived — archive after the closing report."));
    var rows = shelfRows("done");
    if (!rows.length) {
      c.appendChild(el("p", {class: "pj-note", text: "Nothing waiting to archive."}));
      return c;
    }
    rows.forEach(function (w) {
      var tc = taskCounts(w);
      var row = el("div", {class: "prog-row s-done"});
      var name = el("div", {class: "prog-name"});
      var idline = el("div", {class: "prog-idline"});
      idline.appendChild(el("b", {text: w.ws}));
      idline.appendChild(el("span", {text: w.name || ""}));
      name.appendChild(idline);
      name.appendChild(el("span", {class: "prog-pill done", text: "\u2713 Done"}));
      name.appendChild(el("span", {class: "prog-pill ready", text: "\u2713 Ready to archive", title: "Plan is 100% — archive after the closing report"}));
      row.appendChild(name);
      var bar = el("div", {class: "prog-bar", role: "img", "aria-label": w.ws + " " + tc.done + " of " + tc.total + " tasks done"});
      if (tc.total) {
        var fill = el("span");
        fill.style.width = "100%";
        fill.style.background = "var(--good)";
        bar.appendChild(fill);
      }
      row.appendChild(bar);
      row.appendChild(el("span", {class: "prog-pct", text: tc.total ? "100%" : "\u2014"}));
      var meta = el("span", {class: "prog-meta"});
      meta.appendChild(el("b", {text: tc.done + "/" + tc.total}));
      meta.appendChild(document.createTextNode(" tasks \u00B7 " + fmtN(w.tokens_all_time || 0) + " tokens all-time \u00B7 " + fmtUsd(w.cost_usd_all_time || 0)));
      row.appendChild(meta);
      var acts = el("div", {class: "prog-acts"});
      var bp = el("button", {type: "button", text: "Plan"});
      bp.addEventListener("click", function () { openPlan(w.ws); });
      acts.appendChild(bp);
      var rep = reportFor(w.ws, state.archive);
      if (rep) {
        var rl = el("button", {type: "button", title: "Closing report " + rep + " is in the bundle; opening the plan view", text: "Report"});
        rl.addEventListener("click", function () { openPlan(w.ws); });
        acts.appendChild(rl);
      }
      acts.appendChild(copyBtn("python3 agent_dash.py ws archive " + w.ws, "Archive"));
      row.appendChild(acts);
      c.appendChild(row);
    });
    return c;
  }

  function unplannedStrip() {
    var rows = unplannedRows();
    if (!rows.length) return null;
    var c = el("div", {class: "card pj-unplanned", "data-nav": "Unplanned activity"});
    c.appendChild(secHead("Unplanned activity", "To-do: each id below has runs but no plan bundle yet."));
    rows.forEach(function (w) {
      var n = (w.finished_in_range || 0) + (w.live_agents || 0);
      var label = "Active without a plan: " + (w.name ? w.ws + " " + w.name : w.ws);
      var line = el("div", {class: "prog-row s-unplanned"});
      var name = el("div", {class: "prog-name"});
      name.appendChild(el("span", {text: label}));
      var sub = w.live_agents ? w.live_agents + " live" : n ? n + " runs" : fmtN(w.tokens || 0) + " tokens";
      name.appendChild(el("span", {class: "prog-meta", text: " · " + sub}));
      line.appendChild(name);
      var acts = el("div", {class: "prog-acts"});
      acts.appendChild(copyBtn("python3 agent_dash.py ws new " + w.ws + " \"" + suggestTitle(w.ws) + "\"", "Copy ws new"));
      line.appendChild(acts);
      c.appendChild(line);
    });
    return c;
  }

  /* Empty slot mount points. Chart workers register one file plus one
     registry line (scope "project", slot naming one of PROJECT_SLOTS);
     the slot renders their card, and stays an empty container until one
     registers. */
  /* The slot key folds in the entry's own spec key (which carries its
     load state: loading, failed, or generated_at). Without it a card whose
     data lands after the slot mounts — the dependency graph fetched one —
     never rebuilds its slot on its own completion redraw. */
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

  /* Spec list for drawMain, in mockup order: phases, tiles, Current,
     Planned shelf, Recently done, chart slots, then any other
     scope-"project" registry entry as its own section. Entries naming a
     slot mount there. */
  function projectHomeSpecs(passedState, snap) {
    var specs = [];
    var entries = registryFor("project") || [];
    var bySlot = {};
    entries.forEach(function (e) { if (e.slot) bySlot[e.slot] = e; });
    specs.push(slotSection("exec-depgraph", bySlot["exec-depgraph"] || null));
    specs.push(["project-phases", keyOf([state.project]), function () { return phasesSection(state.project); }, ""]);
    specs.push(["project-tiles", keyOf([snap && snap.totals, snap && snap.totals_all_time, state.ws, state.snapError]), function () { return tilesSection(snap); }, ""]);
    specs.push(["project-current", keyOf([(state.wss || []).map(function (w) { return [w.ws, w.live_agents, w.tokens, w.cost_usd]; }), planSum]), function () {
      var c = el("div", {class: "card pj-current", "data-nav": "Current"});
      c.appendChild(secHead("Current", "Active and paused workstreams — execution lives here."));
      c.appendChild(programPanel(["active", "paused"], true));
      return c;
    }, ""]);
    specs.push(["project-planned", keyOf([(state.wss || []).map(function (w) { return [w.ws, w.progress, w.depends_on]; }), state.bundles, state.plans, state.approvals]), function () {
      var wrap = el("div", {class: "pj-planned-wrap"});
      wrap.appendChild(secHead("Planned", "No tab yet — each names what it waits on."));
      wrap.appendChild(plannedShelf());
      var unplanned = unplannedStrip();
      if (unplanned) wrap.appendChild(unplanned);
      return wrap;
    }, ""]);
    specs.push(["project-done", keyOf([(state.wss || []).map(function (w) { return [w.ws, w.progress, w.tokens, w.cost_usd]; }), state.archive]), function () {
      return doneRows();
    }, ""]);
    PROJECT_SLOTS.forEach(function (id) { if (id !== "exec-depgraph") specs.push(slotSection(id, bySlot[id] || null)); });
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
  AdPage.cards.projectHome = projectHomeSpecs;
  AdPage.PROJECT_SLOTS = PROJECT_SLOTS;
  global.projectHomeSpecs = projectHomeSpecs;
  global.reportFor = reportFor;
  global.showToast = showToast;
  global.copyCmd = copyCmd;
  global.PROJECT_SLOTS = PROJECT_SLOTS;
})(window);
