/* Page card: plan progress map, including the replay.
   Renders the workstream plan DAG (groups in columns, task chips, dep
   and train arrows, legend) and owns the run-to-task map (TASKMAP,
   loadTaskmap, taskOf, planTaskIds) plus the whole replay: the rp*
   functions nested inside planCard share the one rpCursorTime playhead
   with the replay run-timeline card, and drive window.__adReplay
   (read by scripts/render_replay.mjs). Kept in this one file because
   the replay closes over planCard locals; the plan map redesign lands
   here next.
   Reads: state.plan, state.runs, state.approvals; window.ADC.
   Exports (on window.AdPage.cards, also kept as plain globals):
   planCard, loadTaskmap, taskOf, taskOfVerified, shortChipLabel, layoutOf,
   planTaskIds; TASKMAP by accessor.
   Needs: el, s, api, OUTCOME, drawMain, refreshAll, openRun,
   openApprovals, canonWs, copyBtn, countUp, fmtN, fmtDate, axes,
   timeTicks, warpTime, warpPos, showTip, hideTip, secCache, Q, FIX,
   ADM (plain globals). */
(function (global) {
  "use strict";
  var TASKMAP = {};
  function loadTaskmap() { return fetch(FIX ? "taskmap.json" : "/static/taskmap.json").then(function (r) { return r.ok ? r.json() : {}; }).then(function (j) { TASKMAP = j || {}; }).catch(function () {}); }
  function taskOf(r, ws) {
    var lab = r.label || "", ov = TASKMAP[ws] || {}, m;
    if (ov[lab]) return ov[lab];
    for (var k in ov) if (k.charAt(0) === "/" && k.length > 2) { try { if (new RegExp(k.slice(1, -1), "i").test(lab)) return ov[k]; } catch (e) {} }
    if (ws === "WS1") { m = /(?:^|[^a-z0-9])ws1[-._]?(\d+[a-z]?)/i.exec(lab) || /WS1\.(\d+[a-z]?)/.exec(lab); if (m) return m[1].toLowerCase(); }
    if (ws === "WS2") { m = /WS2\.([0-9]+[a-z0-9]*|[A-Z][0-9]*)/i.exec(lab) || /ws2[-._]([0-9]+[a-z0-9]*)/i.exec(lab); if (m) return "WS2." + (/^[a-z]/i.test(m[1]) ? m[1].toUpperCase() : m[1]); }
    if (ws === "WS7") { m = /\b([PS]0-\d+)\b/i.exec(lab); if (m) return m[1].toUpperCase(); m = /WS7\.([0-9a-z]+)/i.exec(lab) || /ws7[-._]([0-9a-z]+)/i.exec(lab); if (m) return "WS7." + m[1].toUpperCase(); }
    var ids = planTaskIds(ws);
    if (ids.length) {
      var lower = lab.toLowerCase(), best = null;
      ids.forEach(function (id) {
        var i = lower.indexOf(id.toLowerCase());
        if (i < 0) return;
        var before = i === 0 ? "" : lower.charAt(i - 1), after = lower.charAt(i + id.length);
        if ((before === "" || /[^a-z0-9]/.test(before)) && (after === "" || /[^a-z0-9]/.test(after)) && (!best || id.length > best.length)) best = id;
      });
      if (best) return best;
    }
    return null;
  }
  /* Phantom-task guard, mirroring dash/taskmatch.task_of_verified: a mapped
     id that names no task in this workstream's plan stays unmapped (it lands
     in the "running, mapped to no task" list instead of claiming a chip
     that does not exist, e.g. WS7.OPS). With no plan ids to check against,
     verification is impossible and the raw mapping stands. */
  function taskOfVerified(r, ws) {
    var t = taskOf(r, ws);
    if (t == null) return null;
    var ids = planTaskIds(ws);
    if (!ids.length) return t;
    return ids.indexOf(t) !== -1 ? t : null;
  }
  /* Chip display text from the map standard: the id with the WS<digits>.
     prefix stripped, capped at 14 chars with an end ellipsis. The full id
     stays in the chip tooltip. Mirrors dash/maplayout.capped_label. */
  function shortChipLabel(id) {
    var s = String(id).replace(/^WS\d+\./, "");
    return s.length > 14 ? s.slice(0, 13) + "…" : s;
  }
  /* The server-computed map layout (/api/plan layout object), or null when
     the server predates it: the map falls back to today's behaviour. */
  function layoutOf(plan) {
    var l = plan && plan.layout;
    return (l && typeof l === "object") ? l : null;
  }
  /* Task ids of a workstream's plan, so any workstream's labels map to chips (e.g. ws4-WS4.3-slug or ws4-3-slug when 3 is a task id). */
  function planTaskIds(ws) {
    var p = state.plan && state.plan.ws === ws ? state.plan : null;
    if (!p) return [];
    var out = [];
    (p.groups || []).forEach(function (g) { (g.tasks || []).forEach(function (t) { if (t.id != null && String(t.id).length > 0) out.push(String(t.id)); }); });
    return out;
  }

  /* plan DAG for a workstream */
  function planCard(plan, snap) {
    var live = {}, just = {}, unassigned = [], now = Date.now() / 1000;
    ((snap && snap.running) || []).forEach(function (r) { if (r.workstream && r.workstream !== plan.ws) return; var t = taskOfVerified(r, plan.ws); if (t) (live[t] = live[t] || []).push(r); else if (r.workstream === plan.ws) unassigned.push(r); });
    ((snap && snap.recent) || []).forEach(function (r) { if (r.ended_at && now - r.ended_at < 900) { var t = taskOfVerified(r, plan.ws); if (t) just[t] = r; } });
    var att = {}, seenRun = {};
    (state.runs || []).concat((snap && snap.running) || [], (snap && snap.recent) || []).forEach(function (r) {
      var id = r.run_id || r.label; if (!id || seenRun[id]) return; seenRun[id] = 1;
      if (r.workstream && r.workstream !== plan.ws) return; var t = taskOfVerified(r, plan.ws); if (!t) return;
      var a = att[t] = att[t] || {runs: 0, reviews: 0, sentBack: 0}; a.runs++;
      var isRev = /(^|[-_ ])(rev|review)/i.test(r.label || "") || /^(APPROVE|REQUEST_CHANGES)/.test(r.verdict || "");
      if (isRev) a.reviews++;
      if (r.verdict === "REQUEST_CHANGES" || r.verdict === "APPROVE_WITH_CHANGES" || r.verdict === "PARTIAL" || r.verdict === "BLOCKED") a.sentBack++;
    });
    function attText(a) { return a.runs + " run" + (a.runs > 1 ? "s" : "") + ": " + (a.runs - a.reviews) + " build, " + a.reviews + " review" + (a.reviews === 1 ? "" : "s") + (a.sentBack ? ", " + a.sentBack + " sent back (changes, partial or blocked)" : ""); }
    var nLive = Object.keys(live).reduce(function (a, k) { return a + live[k].length; }, 0);
    var c = el("div", {class: "card", id: "exec-progress", "data-nav": "Plan progress"}, [el("div", {style: "display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"}, [el("h2", {text: plan.ws + " plan progress" + (nLive ? " · " + nLive + " agent" + (nLive > 1 ? "s" : "") + " working" : "")}), (plan.start_prompt ? copyBtn(plan.start_prompt) : null), el("a", {class: "planbtn", href: planUrl(plan.ws), onclick: function (ev) { ev.preventDefault(); openPlan(plan.ws); }, text: "Open the " + plan.ws + " plan"})]), el("p", {class: "sub", text: "Each tile is a stage of the plan, left to right. The pill says where it stands; hover a tile's progress line for the exact counts, or a chip for what it waits on and unblocks."})]);
    var legendPop = el("div", {class: "pop"}, [el("b", {text: "What the chips mean"})]);
    var legendBtn = el("span", {class: "legend-tip", tabindex: "0", "aria-label": "Legend"}, ["Legend \u24D8", legendPop]);
    c.firstChild.appendChild(legendBtn);
    var workingIds = Object.keys(live);
    if (workingIds.length) {
      var wn = el("div", {class: "working-now"}, [el("b", {text: "\u25CF Working now (" + nLive + ")"})]);
      workingIds.forEach(function (tid) { live[tid].forEach(function (r) {
        var slug = String(r.label || "").replace(new RegExp("^" + plan.ws.toLowerCase() + "[-._]?", "i"), "").replace(tid, "").replace(/^[-._]+/, "");
        var pill = el("button", {class: "wn-pill", title: (r.label || "") + " \u00B7 " + fullName(tierKey(r)) + " \u00B7 " + fmtDur(r.elapsed_s)}, [el("i"), el("span", {class: "wn-id", text: tid}), el("span", {class: "wn-slug", text: slug}), el("span", {class: "wn-tok", text: tokTotal(r.tokens) ? fmtN(tokTotal(r.tokens)) + " \u00B7 " + cachedPct(r.tokens) + "% cached" : (r.context_tokens ? "~" + fmtN(r.context_tokens) + " \u00B7 live" : "")})]);
        pill.firstChild.style.background = colorOf(tierKey(r));
        pill.addEventListener("click", function () { openRun(r); });
        wn.appendChild(pill);
      }); });
      c.appendChild(wn);
    }
    if (unassigned.length) { var ua = el("div", {class: "unassigned"}, [el("b", {style: "font-size:12px;color:var(--ink-2)", text: "Running, not mapped to a task:"})]); unassigned.forEach(function (r) { var sp = el("span", {title: (r.tier || "") + " · " + fmtDur(r.elapsed_s), text: r.label || r.run_id}); sp.style.borderColor = colorOf(tierKey(r)); ua.appendChild(sp); }); c.appendChild(ua); }
    var gs = {}, colStat = {};
    /* Server-computed map layout (dash/maplayout.py, carried on /api/plan):
       density, per-group collapse state, slim columns, chip labels, arrows
       policy, train tags, phases line. Null when the server predates it, and
       the map below falls back to today's behaviour branch by branch. */
    var L = layoutOf(plan), LGroup = {}, LCol = {}, LTrain = null, foldedIdx = {};
    if (L) {
      (L.groups || []).forEach(function (rg) { if (rg && rg.id != null) LGroup[rg.id] = rg; });
      (L.columns || []).forEach(function (rc) { if (rc && rc.index != null) LCol[rc.index] = rc; });
      if (L.folded_rail) (L.folded_rail.columns || []).forEach(function (fc) { if (fc && fc.index != null) foldedIdx[fc.index] = fc; });
      if (L.trains) {
        LTrain = {tagged: {}, legend: L.trains.legend || [], more: L.trains.more_trains || 0, caption: L.trains.caption || null};
        (L.trains.tagged || []).forEach(function (id) { LTrain.tagged[id] = 1; });
      }
    }
    var taskById = {};
    plan.groups.forEach(function (g) { (g.tasks || []).forEach(function (t) { taskById[t.id] = t; }); });
    /* Chips the layout folds away (hidden done chips, deferred chips) stay
       out of the arrow router until their expander opens. */
    var foldedIds = {};
    var trains = (plan.trains || []).filter(function (t) { return t && typeof t === "object"; });
    var trainOf = {};
    trains.forEach(function (t, i) { if (LTrain && t.id != null && !LTrain.tagged[t.id]) return; ((t.members) || []).forEach(function (m) { trainOf[m] = {train: t, slot: (i % 8) + 1}; }); });
    var prevSt = (window.__adPrevStatus = window.__adPrevStatus || {});
    var wrap = el("div", {class: "dag-scroll" + (L ? " fitted" : "")}), dag = el("div", {class: "dag" + (L && L.density === "compact" ? " compact" : "")}), svg = s("svg", {class: "dedges"}); dag.appendChild(svg); wrap.appendChild(dag);
    if (L) {
      var widths = [];
      plan.cols.forEach(function (n, i) { if (!foldedIdx[i]) widths.push((LCol[i] && LCol[i].state !== "full") ? "0.55fr" : "1fr"); });
      if (L.folded_rail) widths.push("0.7fr");
      dag.style.gridTemplateColumns = widths.join(" ");
    } else {
      dag.style.gridTemplateColumns = "repeat(" + plan.cols.length + ",1fr)";
    }
    var cols = [];
    plan.cols.forEach(function (n, i) {
      if (foldedIdx[i]) return;
      var d = el("div", {class: "dcol" + ((LCol[i] && LCol[i].state === "slim") ? " slim-col" : "")}, [el("div", {class: "dcolh", text: n})]);
      dag.appendChild(d); cols[i] = d;
    });
    /* Balance caption (standard B7): when the tallest column outweighs the
       shortest more than 3x, the short side says so instead of looking empty. */
    if (L && L.balance && L.balance.ratio != null && L.balance.ratio > 3 && L.balance.shortest && L.balance.tallest && cols[L.balance.shortest.column]) {
      cols[L.balance.shortest.column].appendChild(el("div", {class: "balance-note", title: "The tallest stage (" + L.balance.tallest.title + ") holds " + L.balance.tallest.tasks + " tasks; the renderer compacts but never moves groups"}, [L.balance.shortest.tasks + " tasks · lightest stage"]));
    }
    var nodes = {}, chipEl = {}, tGroup = {}, tTitle = {}, tUp = {}, tDown = {}, depth = {};
    var tKind = {}, tStatus = {}, tNotes = {};
    plan.groups.forEach(function (g) { g.tasks.forEach(function (t) { tGroup[t.id] = g.id; tTitle[t.id] = t.title || ""; tStatus[t.id] = t.status || "todo"; if (t.kind === "gate") tKind[t.id] = "gate"; if (t.dep_notes && typeof t.dep_notes === "object") tNotes[t.id] = t.dep_notes; }); });
    plan.groups.forEach(function (g) { g.tasks.forEach(function (t) { (t.deps || []).forEach(function (d) { if (!tGroup[d] || d === t.id) return; (tUp[t.id] = tUp[t.id] || []).push(d); (tDown[d] = tDown[d] || []).push(t.id); }); }); });
    var hasTaskDeps = Object.keys(tUp).length > 0;
    function depthOf(id, seen) { if (depth[id] != null) return depth[id]; seen = seen || {}; if (seen[id]) return 0; seen[id] = 1; var d = 0; (tUp[id] || []).forEach(function (p) { d = Math.max(d, 1 + depthOf(p, seen)); }); return (depth[id] = d); }
    function depMet(d) { return tStatus[d] === "done"; }
    function fmtDep(b) { return function (d) { var s = d + " " + (tTitle[d] || "") + " (" + (depMet(d) ? "met" : "not met") + ")"; var note = (tNotes[b] || {})[d]; if (typeof note === "string" && note) s += ": " + note; return s; }; }
    function edgeTitle(a, b) { var note = (tNotes[b] || {})[a]; if (typeof note === "string" && note) return b + " " + (tTitle[b] || "") + " waits on " + a + " " + (tTitle[a] || "") + ": " + note + " (" + (depMet(a) ? "met" : "not met") + ")"; return b + " waits on " + a; }
    var gTitle = {};
    plan.groups.forEach(function (g) { gTitle[g.id] = g.title || g.id; });
    var deps = {}; (plan.edges || []).forEach(function (e) { (deps[e[1]] = deps[e[1]] || []).push(e[0]); });
    /* Local chip meta for tasks the layout folds away, mirroring
       dash/maplayout._chip_tooltip plus capped_label. */
    function localChipMeta(t) {
      var s = t.id + " · " + (t.title || "") + " · " + (t.status || "todo");
      if (t.kind === "gate") s += " · gate";
      if (t.moved_to) s += " to " + t.moved_to;
      if (t.note) s += " (" + t.note + ")";
      var dn = (t.dep_notes && typeof t.dep_notes === "object") ? t.dep_notes : {};
      var ups = (t.deps || []).filter(function (d) { return typeof d === "string"; }).map(function (d) { return d + ((typeof dn[d] === "string" && dn[d]) ? ": " + dn[d] : ""); });
      if (ups.length) s += " · waits on " + ups.join(", ");
      var ndeps = (t.deps || []).filter(function (d) { return typeof d === "string"; }).length;
      return {label: shortChipLabel(t.id), tooltip: s, fan_in: ndeps > 6 ? ndeps : 0};
    }
    /* One task chip. lg is the layout chip ({label, tooltip, fan_in}) or null
       on the fallback path. Live-agent badges and pulses, the "just finished"
       outline, run counts, train tags, gate diamonds and hover/pin tracing
       behave the same on both paths. */
    function makeChip(g, t, lg) {
      var st = t.status || "todo", rs = live[t.id] || [], jr = just[t.id], gate = t.kind === "gate";
      var label = lg ? lg.label : t.id;
      var tip = lg ? lg.tooltip : (t.id + " · " + (t.title || "") + (gate ? " · gate (" + (st === "done" ? "met" : "not met") + ")" : " · " + st) + (t.moved_to ? " to " + t.moved_to : "") + (t.note ? " (" + t.note + ")" : ""));
      if (rs.length) tip += " · working: " + rs.map(function (r) { return (r.label || r.run_id) + " (" + (r.tier || "") + ", " + fmtDur(r.elapsed_s) + ")"; }).join(", ");
      if (jr && !rs.length) tip += " · just finished: " + (jr.label || "") + (jr.verdict ? " " + jr.verdict : "");
      var cp = el("span", {class: "chip " + st + (rs.length ? " live" : "") + (jr && !rs.length ? " just" : "") + (gate ? " gate" : ""), title: tip}, [label]);
      var sk = plan.ws + ":" + t.id; if (prevSt[sk] && prevSt[sk] !== st) cp.classList.add("changed"); prevSt[sk] = st;
      var at = att[t.id]; if (at) { cp.title += " · " + attText(at); if (at.runs > 1) cp.appendChild(el("span", {class: "att" + (at.sentBack >= 2 ? " hot" : ""), text: "↻" + fmtN(at.runs)})); }
      var tr = trainOf[t.id];
      if (tr) {
        cp.classList.add("intrain");
        cp.style.borderLeft = "3px solid var(--s" + tr.slot + ")";
        cp.title += "\nTrain: " + (tr.train.title || tr.train.id) + (tr.train.id ? " (" + tr.train.id + ")" : "") + " · " + (tr.train.status || "planned");
        cp.appendChild(el("span", {class: "trtag", text: tr.train.id || "T?", title: (tr.train.title || "") + " · " + (tr.train.status || "planned")}));
      }
      if (lg && lg.fan_in) cp.appendChild(el("span", {class: "fanin", title: "Waits on " + lg.fan_in + " tasks; hover to trace them all", text: "↓" + lg.fan_in}));
      if (t.wave) cp.title += "\nWave: " + t.wave;
      if (!lg) {
        if (tUp[t.id]) cp.title += "\nWaits on: " + tUp[t.id].map(fmtDep(t.id)).join(", ");
        if (tDown[t.id]) cp.title += "\nUnblocks: " + tDown[t.id].map(function (d) { return d + " " + (tTitle[d] || ""); }).join(", ");
      }
      if (rs.length) { var b = el("span", {class: "ag", text: String(rs.length)}); b.style.background = colorOf(tierKey(rs[0])); cp.appendChild(b); cp.style.cursor = "pointer"; cp.addEventListener("click", function () { openRun(rs[0]); }); }
      else if (hasTaskDeps) { cp.style.cursor = "pointer"; cp.addEventListener("click", function (ev) { ev.stopPropagation(); pinned = pinned === t.id ? null : t.id; if (pinned) focusTask(t.id); else { clearTask(); groupEnter(g.id); } }); }
      if (hasTaskDeps) { cp.addEventListener("mouseenter", function () { focusTask(t.id); }); cp.addEventListener("mouseleave", function () { if (pinned) focusTask(pinned); else { clearTask(); groupEnter(g.id); } }); }
      return cp;
    }
    /* In-place expander for folded chips ("+N done", "↷N deferred"): the
       chips render once inside a hidden span so replay and hover tracing keep
       working, and the button toggles them open. */
    function foldWrap(label, cls, tip, items) {
      var box = el("span", {class: "folded-chips"});
      box.style.display = "none";
      items.forEach(function (it) { var cp = makeChip(it.g, it.t, it.lg); foldedIds[it.t.id] = 1; chipEl[it.t.id] = cp; box.appendChild(cp); });
      var open = false;
      var btn = el("button", {class: "more-btn " + cls, title: tip, text: label, "aria-expanded": "false"});
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        open = !open;
        box.style.display = open ? "" : "none";
        btn.setAttribute("aria-expanded", String(open));
        if (label.charAt(0) === "+") btn.textContent = open ? "−" + label.slice(1) : label;
        if (open) items.forEach(function (it) { delete foldedIds[it.t.id]; });
        else items.forEach(function (it) { foldedIds[it.t.id] = 1; });
        clearTask(true); draw();
      });
      return el("span", {class: "fold-wrap"}, [btn, box]);
    }
    function groupCounts(g) {
      var cnt = {done: 0, wip: 0, prog: 0, pr: 0, todo: 0, moved: 0, deferred: 0};
      (g.tasks || []).forEach(function (t) { cnt[t.status || "todo"] = (cnt[t.status || "todo"] || 0) + 1; });
      return cnt;
    }
    function buildTile(g) {
      var cnt = groupCounts(g);
      var liveTotal = (g.tasks || []).length - (cnt.moved || 0) - (cnt.deferred || 0);
      var n = el("div", {class: "dnode " + (liveTotal && cnt.done === liveTotal ? "s-done" : (cnt.done + cnt.prog + cnt.pr ? "s-prog" : ""))});
      n.appendChild(el("h4", {text: g.title}));
      var gr = 0, gv = 0; (g.tasks || []).forEach(function (t) { var a = att[t.id]; if (a) { gr += a.runs; gv += a.reviews; } });
      var left = liveTotal - cnt.done;
      n.appendChild(el("div", {class: "m", title: cnt.done + " done, " + cnt.wip + " in progress, " + cnt.pr + " waiting to merge, " + cnt.prog + " merged and waiting on a prod step, " + cnt.todo + " not started" + (cnt.deferred ? ", " + cnt.deferred + " deferred" : "") + (gr ? "\n" + fmtN(gr) + " agent runs, " + fmtN(gv) + " of them reviews" : "")}, [el("b", {text: cnt.done + " of " + liveTotal}), " done" + (left ? " · " + left + " to go" : "")]));
      var mini = el("div", {class: "mini"}); [["done", "var(--good)"], ["pr", "var(--warning)"], ["prog", "var(--s5)"]].forEach(function (p) { var sp = el("span"); sp.style.width = (liveTotal ? 100 * cnt[p[0]] / liveTotal : 0) + "%"; sp.style.background = p[1]; sp.style.display = "block"; mini.appendChild(sp); }); n.appendChild(mini);
      if (L) {
        var ups = (deps[g.id] || []).map(function (d) { return gTitle[d] || d; });
        if (ups.length && !(liveTotal && cnt.done === liveTotal)) n.appendChild(el("div", {class: "tile-waits", title: "Group edge: this stage waits on " + ups.join(", ")}, ["← waits on " + ups.join(", ")]));
      }
      var ch = el("div", {class: "chips"});
      var gLive = 0;
      var lg = L ? LGroup[g.id] : null;
      if (lg) {
        (lg.chips || []).forEach(function (lc) {
          var t = taskById[lc.id]; if (!t) return;
          var cp = makeChip(g, t, lc);
          if ((live[t.id] || []).length) gLive += live[t.id].length;
          chipEl[t.id] = cp; ch.appendChild(cp);
        });
        var hidden = (lg.hidden_done_ids || []).map(function (id) { return taskById[id]; }).filter(Boolean);
        if (hidden.length && lg.more_token) ch.appendChild(foldWrap(lg.more_token, "is-done", hidden.length + " finished tasks are folded here; click to expand in place", hidden.map(function (t) { return {g: g, t: t, lg: localChipMeta(t)}; })));
        var defs = (g.tasks || []).filter(function (t) { return (t.status || "todo") === "deferred"; });
        if (defs.length && lg.deferred_token) ch.appendChild(foldWrap(lg.deferred_token, "is-def", defs.length + " deferred tasks are folded here; click to expand in place", defs.map(function (t) { return {g: g, t: t, lg: localChipMeta(t)}; })));
      } else {
        var ordered = hasTaskDeps ? (g.tasks || []).map(function (t, i) { return [depthOf(t.id), i, t]; }).sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; }).map(function (x) { return x[2]; }) : (g.tasks || []);
        ordered.forEach(function (t) {
          var cp = makeChip(g, t, null);
          if ((live[t.id] || []).length) gLive += live[t.id].length;
          chipEl[t.id] = cp; ch.appendChild(cp);
        });
      }
      if (gLive) { n.insertBefore(el("div", {class: "live-n", text: "● " + gLive + " agent" + (gLive > 1 ? "s" : "") + " working"}), n.children[2]); n.classList.add("s-live"); }
      n.appendChild(ch);
      nodes[g.id] = n;
      gs[g.id] = {done: liveTotal > 0 && cnt.done === liveTotal, started: cnt.done + cnt.wip + cnt.prog + cnt.pr > 0, active: gLive > 0 || cnt.wip + cnt.prog + cnt.pr > 0, col: g.col, cnt: cnt, live: gLive, title: g.title};
      if (cols[g.col]) { colStat[g.col] = colStat[g.col] || {done: 0, tot: 0, now: false}; colStat[g.col].done += cnt.done; colStat[g.col].tot += liveTotal; }
      return n;
    }
    /* Slim tile for an all-done stage when columns overflow: title, ✓N/N and
       the bar, no chips. Click expands the full tile in place. */
    function slimTile(g) {
      var cnt = groupCounts(g);
      var liveTotal = (g.tasks || []).length - (cnt.moved || 0) - (cnt.deferred || 0);
      var lg = LGroup[g.id] || {};
      var sum = lg.slim_summary || {done: cnt.done, total: liveTotal};
      var b = el("button", {class: "slim-tile", title: (g.title || g.id) + " · all done (" + sum.done + " of " + sum.total + "); click to expand the chips in place"}, [el("b", {text: g.title || g.id}), el("span", {text: "✓" + sum.done + "/" + sum.total})]);
      var mini = el("div", {class: "mini"}); var f = el("span"); f.style.width = "100%"; f.style.background = "var(--good)"; f.style.display = "block"; mini.appendChild(f); b.appendChild(mini);
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var full = buildTile(g);
        b.replaceWith(full); paintStage(g);
        clearTask(true); draw();
      });
      nodes[g.id] = b;
      gs[g.id] = {done: true, started: true, active: false, col: g.col, cnt: cnt, live: 0, title: g.title};
      if (cols[g.col]) { colStat[g.col] = colStat[g.col] || {done: 0, tot: 0, now: false}; colStat[g.col].done += cnt.done; colStat[g.col].tot += liveTotal; }
      return b;
    }
    /* Stage pill per group tile: where it stands and why. Skips slim tiles
       (they already read ✓N/N) and folded columns (no tile yet). */
    function paintStage(g) {
      var st = gs[g.id], n = nodes[g.id]; if (!st || !n) return;
      if (n.classList.contains("slim-tile")) return;
      if (n.querySelector(":scope > .gtag")) return;
      var k = st.cnt, stage, cls, why;
      var waitOn = (deps[g.id] || []).filter(function (d) { return !(gs[d] && gs[d].done); });
      if (st.done) { n.classList.add("g-done"); stage = "✓ Done"; cls = "done"; why = "Every task is merged and live."; }
      else if (st.active) {
        n.classList.add("g-now"); if (colStat[g.col]) colStat[g.col].now = true;
        if (st.live || k.wip) { stage = "▶ Building"; cls = "now"; why = "Agents are building tasks in this stage."; }
        else if (k.pr) { stage = "Merging"; cls = "merge"; why = "Work is built and approved; it is waiting to merge."; }
        else { stage = "Rolling out"; cls = "prod"; why = "Merged; waiting on a production step."; }
      }
      else if (!waitOn.length) { n.classList.add("g-ready"); stage = st.started ? "Partly done" : "Ready to start"; cls = "ready"; why = st.started ? "Some tasks are done and nothing is running now." : "Everything it waits on is done."; }
      else { stage = "Waiting"; cls = "wait"; why = "Waits on " + waitOn.map(function (d) { return gs[d] ? gs[d].title : d; }).join(", ") + "."; }
      n.insertBefore(el("span", {class: "gtag " + cls, title: why, text: stage}), n.firstChild);
    }
    plan.groups.forEach(function (g) {
      if (foldedIdx[g.col]) {
        var cnt = groupCounts(g);
        var liveTotal = (g.tasks || []).length - (cnt.moved || 0) - (cnt.deferred || 0);
        var gLive = 0; (g.tasks || []).forEach(function (t) { gLive += (live[t.id] || []).length; });
        gs[g.id] = {done: liveTotal > 0 && cnt.done === liveTotal, started: cnt.done + cnt.wip + cnt.prog + cnt.pr > 0, active: gLive > 0 || cnt.wip + cnt.prog + cnt.pr > 0, col: g.col, cnt: cnt, live: gLive, title: g.title};
        return;
      }
      var tile = (L && LGroup[g.id] && LGroup[g.id].state === "slim") ? slimTile(g) : buildTile(g);
      cols[g.col].appendChild(tile);
    });
    if (L && L.folded_rail) {
      var rail = el("div", {class: "dcol fold-rail"}, [el("div", {class: "dcolh", text: "Later stages"})]);
      (L.folded_rail.columns || []).forEach(function (fc) {
        var sb = el("button", {class: "slim-tile", title: (fc.title || "") + " · " + (fc.tasks || 0) + " tasks; click to expand this stage in place"}, [el("b", {text: fc.title || ("Stage " + (fc.index + 1))}), el("span", {text: "+" + (fc.tasks || 0) + " tasks"})]);
        sb.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var frag = document.createDocumentFragment();
          plan.groups.forEach(function (gg) {
            if (gg.col !== fc.index) return;
            frag.appendChild((LGroup[gg.id] && LGroup[gg.id].state === "slim") ? slimTile(gg) : buildTile(gg));
            paintStage(gg);
          });
          sb.replaceWith(frag); clearTask(true); draw();
        });
        rail.appendChild(sb);
      });
      dag.appendChild(rail);
    }
    plan.groups.forEach(function (g) { paintStage(g); });
    cols.forEach(function (d, i) { var cs = colStat[i]; if (!cs) return; var mb = el("div", {class: "colbar", title: cs.done + " of " + cs.tot + " done in this phase"}); var f = el("i"); f.style.width = (cs.tot ? 100 * cs.done / cs.tot : 0) + "%"; mb.appendChild(f); d.insertBefore(mb, d.children[1]); if (cs.now) d.classList.add("c-now"); });
    /* Overall progress comes from the server progress object, never recomputed here. */
    var pprog = plan.progress || {done: 0, total: 0, counts: {}, pct: 0};
    pprog.counts = pprog.counts || {};
    var all = pprog.total, pct = pprog.pct, tot = pprog.counts;
    var lg = el("div", {class: "plegend"});
    [["live", "agent working now"], ["wip", "in progress (branch, not merged)"], ["pr", "waiting to merge (approved or PR open)"], ["prog", "merged, waiting on a prod step"], ["done", "done"], ["just", "finished in the last 15 min"], ["todo", "not started"], ["deferred", "deferred or moved to another workstream"]].forEach(function (p) { var ch2 = el("span", {class: "chip " + (p[0] === "just" ? "done just" : p[0])}, [p[0] === "live" ? "\u25CF" : p[0] === "todo" ? "\u00B7" : "\u2009"]); lg.appendChild(el("span", {style: "display:inline-flex;align-items:center;gap:6px"}, [ch2, p[1]])); });
    legendPop.appendChild(lg);
    legendPop.appendChild(el("p", {text: "The badge on a working chip is the model family and how many agents are on it. \u21BBN counts the agent runs on a task in the last 24 h (an amber edge means it was sent back twice or more). Arrows between tiles show which stage waits on which; hover a tile to light up its chain. Every task dependency is an arrow between chips: solid when the prerequisite is done, dashed while it waits; hover an arrow for its condition. Hover a chip to see the tasks it waits on (amber) and unblocks (blue); click to pin the whole chain. A \u25C7 chip is a gate, an outside signal: a check means met. The Arrows toggle switches between all arrows and on-focus only. Chips in a tile run left to right in the order they can start."}));
    var ob = el("div", {class: "pbig"}), obBar = el("div", {class: "pbig-bar", role: "img", "aria-label": tot.done + " of " + all + " tasks done"});
    [["done", "var(--good)"], ["prog", "var(--s5)"], ["pr", "var(--warning)"]].forEach(function (p) { var sp = el("span"); sp.style.background = p[1]; var pw0 = ((window.__adPrevBar || {})[plan.ws] || {})[p[0]]; sp.style.width = (pw0 == null ? 0 : pw0) + "%"; sp.dataset.w = all ? (100 * (tot[p[0]] || 0) / all) : 0; sp.dataset.k = p[0]; obBar.appendChild(sp); });
    var obPct = el("b", {class: "pbig-pct", text: "0%"});
    ob.appendChild(obPct); ob.appendChild(obBar);
    var nowCols = Object.keys(colStat).map(Number).filter(function (i) { return colStat[i].now; }).sort(function (a, b) { return a - b; });
    var openCols = Object.keys(colStat).map(Number).filter(function (i) { return colStat[i].done < colStat[i].tot; }).sort(function (a, b) { return a - b; });
    var nP = plan.cols.length, phaseTxt;
    if (!openCols.length) phaseTxt = "every phase done";
    else if (nowCols.length === 1) phaseTxt = "now in " + plan.cols[nowCols[0]] + " (phase " + (nowCols[0] + 1) + " of " + nP + ")";
    else if (nowCols.length > 1) phaseTxt = "active in " + nowCols.length + " of " + nP + " phases, " + plan.cols[nowCols[0]] + " to " + plan.cols[nowCols[nowCols.length - 1]];
    else phaseTxt = "next up: " + plan.cols[openCols[0]] + " (phase " + (openCols[0] + 1) + " of " + nP + ")";
    ob.appendChild(el("div", {class: "pbig-txt", title: tot.done + " done, " + tot.wip + " in progress, " + tot.pr + " waiting to merge, " + tot.prog + " merged and waiting on a prod step, " + tot.todo + " not started" + (tot.deferred ? ", " + tot.deferred + " deferred" : "")}, [el("b", {text: tot.done + " of " + all + " tasks done"}), " \u00B7 " + (all - tot.done) + " to go \u00B7 " + phaseTxt + (nLive ? " \u00B7 " + nLive + " agent" + (nLive > 1 ? "s" : "") + " working" : "")]));
    c.appendChild(ob);
    setTimeout(function () { var pb = (window.__adPrevBar = window.__adPrevBar || {})[plan.ws] = {}; [].forEach.call(obBar.children, function (sp) { sp.style.width = sp.dataset.w + "%"; pb[sp.dataset.k] = sp.dataset.w; }); countUp(obPct, (window.__adPrevPct || {})[plan.ws] || 0, pct); (window.__adPrevPct = window.__adPrevPct || {})[plan.ws] = pct; }, 40);
    /* Always-visible compact legend row (standard layer B): status colour plus
       symbol plus word, then the arrows mode and why. Details stay in the
       Legend popover as today. */
    if (L) {
      var mleg = el("div", {class: "map-legend"});
      mleg.appendChild(el("span", {class: "cap", text: "Map"}));
      [["done", "✓", "done"], ["live", "●", "working now"], ["gate", "◇", "gate"], ["todo", "·", "not started"], ["deferred", "↷", "deferred"]].forEach(function (p) {
        mleg.appendChild(el("span", {title: p[2]}, [el("span", {class: "chip " + p[0] + " sample", text: p[1]}), document.createTextNode(p[2])]));
      });
      mleg.appendChild(el("span", {class: "arrows-why", title: arrowWhy, text: (L.arrows && L.arrows.reason) || ""}));
      c.appendChild(mleg);
    }
    c.appendChild(wrap);
    if (trains.length || (LTrain && LTrain.caption)) {
      var trl = el("div", {class: "trlegend"});
      if (LTrain) {
        var shownTrains = trains.filter(function (t) { return t.id != null && LTrain.tagged[t.id]; });
        var byId = {};
        shownTrains.forEach(function (t) { byId[t.id] = t; });
        (LTrain.legend || []).forEach(function (id) {
          var t = byId[id]; if (!t) return;
          var i = shownTrains.indexOf(t);
          var sw = el("i", {class: "sw"});
          sw.style.background = "var(--s" + ((i % 8) + 1) + ")";
          var label = (t.title || t.id) + (t.id ? " (" + t.id + ")" : "") + " · " + (t.status || "planned");
          var tip = (t.title || t.id) + (t.id ? " (" + t.id + ")" : "") + (t.status ? " · " + t.status : "") + (t.branch ? " · " + t.branch : "") + (t.pr != null ? " · PR " + t.pr : "");
          trl.appendChild(el("span", {title: tip}, [sw, document.createTextNode(label)]));
        });
        if (LTrain.more) {
          var rest = shownTrains.filter(function (t) { return LTrain.legend.indexOf(t.id) === -1; });
          var rbox = el("span", {class: "folded-trains"});
          rbox.style.display = "none";
          rest.forEach(function (t) {
            var i = shownTrains.indexOf(t);
            var sw2 = el("i", {class: "sw"});
            sw2.style.background = "var(--s" + ((i % 8) + 1) + ")";
            rbox.appendChild(el("span", {title: (t.title || t.id) + " · " + (t.status || "planned")}, [sw2, document.createTextNode((t.title || t.id) + (t.id ? " (" + t.id + ")" : ""))]));
          });
          var topen = false;
          var tbtn = el("button", {class: "more-btn is-train", title: "Click to show the remaining merge trains", text: "+" + LTrain.more + " more trains", "aria-expanded": "false"});
          tbtn.addEventListener("click", function (ev) {
            ev.stopPropagation();
            topen = !topen;
            rbox.style.display = topen ? "" : "none";
            tbtn.setAttribute("aria-expanded", String(topen));
            tbtn.textContent = topen ? "−" + LTrain.more + " more trains" : "+" + LTrain.more + " more trains";
            clearTask(true); draw();
          });
          trl.appendChild(tbtn); trl.appendChild(rbox);
        }
        if (LTrain.caption) trl.appendChild(el("span", {class: "trcap", title: "Trains with a single member render no tag (MAP107)", text: LTrain.caption}));
      } else {
        trains.forEach(function (t, i) {
          var sw = el("i", {class: "sw"});
          sw.style.background = "var(--s" + ((i % 8) + 1) + ")";
          var label = (t.title || t.id) + (t.id ? " (" + t.id + ")" : "") + " · " + (t.status || "planned");
          var tip = (t.title || t.id) + (t.id ? " (" + t.id + ")" : "") + (t.status ? " · " + t.status : "") + (t.branch ? " · " + t.branch : "") + (t.pr != null ? " · PR " + t.pr : "");
          var item = el("span", {title: tip}, [sw, document.createTextNode(label)]);
          trl.appendChild(item);
        });
      }
      c.appendChild(trl);
    }
    /* Always-on task arrows: every task dependency draws from the prerequisite chip to the dependent chip. */
    var nTaskEdges = 0;
    Object.keys(tUp).forEach(function (b) { nTaskEdges += (tUp[b] || []).length; });
    var storedArrows = null;
    try { storedArrows = localStorage.getItem("agent-dash-arrows"); } catch (e) {}
    var layoutArrows = (L && L.arrows) ? (L.arrows.mode === "on-focus" ? "focus" : "all") : null;
    var arrowWhy = (L && L.arrows && L.arrows.reason) ? L.arrows.reason : (nTaskEdges + " task deps" + (nTaskEdges > 150 ? ": over 150, so the map defaults to on-focus arrows" : ""));
    var arrowMode = storedArrows === "focus" ? "focus" : storedArrows === "all" ? "all" : (layoutArrows || (nTaskEdges > 150 ? "focus" : "all"));
    var arrowsTg = el("span", {class: "arrows-toggle", title: arrowWhy}, [document.createTextNode("Arrows: ")]);
    var arrowsSeg = el("span", {class: "seg-ctl"});
    var arrowsAll = el("button", {text: "all", title: "Draw every task dependency arrow"});
    var arrowsFocus = el("button", {text: "on focus", title: "Draw task arrows only for the hovered or pinned chip"});
    arrowsSeg.appendChild(arrowsAll); arrowsSeg.appendChild(arrowsFocus); arrowsTg.appendChild(arrowsSeg);
    c.firstChild.appendChild(arrowsTg);
    function paintArrowsToggle() { arrowsAll.setAttribute("aria-pressed", String(arrowMode === "all")); arrowsFocus.setAttribute("aria-pressed", String(arrowMode === "focus")); }
    paintArrowsToggle();
    arrowsAll.addEventListener("click", function () { setArrowMode("all"); });
    arrowsFocus.addEventListener("click", function () { setArrowMode("focus"); });
    function setArrowMode(m) {
      arrowMode = m;
      try { localStorage.setItem("agent-dash-arrows", m); } catch (e) {}
      paintArrowsToggle();
      if (pinned) focusTask(pinned);
      else { clearTask(true); if (m === "all") drawAllTaskEdges(); }
    }
    function edgeSample(cls, tip) {
      var g = s("svg", {width: "34", height: "10", title: tip, role: "img", "aria-label": tip});
      var col = cls === "a-met" ? "var(--good)" : "var(--muted)";
      var pl = s("path", {d: "M2,5 L26,5", fill: "none", stroke: col, "stroke-width": "1.4"});
      if (cls !== "a-met") pl.setAttribute("stroke-dasharray", "3 4");
      g.appendChild(pl);
      g.appendChild(s("path", {d: "M26,2 L32,5 L26,8 z", fill: col}));
      return g;
    }
    if (hasTaskDeps) {
      var aleg = el("div", {class: "alegend"});
      aleg.appendChild(el("span", {title: "Solid arrow: the prerequisite chip is done"}, [edgeSample("a-met", "Solid arrow: the prerequisite chip is done"), document.createTextNode("met prerequisite")]));
      aleg.appendChild(el("span", {title: "Dashed arrow: the prerequisite chip is not done yet"}, [edgeSample("a-un", "Dashed arrow: the prerequisite chip is not done yet"), document.createTextNode("not met")]));
      aleg.appendChild(el("span", {title: "Diamond chip: a gate, an outside signal or condition. A check means met, a hollow diamond means not met."}, [el("i", {class: "gchip", text: "\u25C7"}), document.createTextNode("gate")]));
      c.appendChild(aleg);
    }
    function draw() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var box = dag.getBoundingClientRect(); if (!box.width) return;
      svg.setAttribute("width", dag.scrollWidth); svg.setAttribute("height", dag.scrollHeight);
      (plan.edges || []).forEach(function (e) {
        if (!nodes[e[0]] || !nodes[e[1]]) return;
        var a = nodes[e[0]].getBoundingClientRect(), b = nodes[e[1]].getBoundingClientRect(), d;
        if (Math.abs(a.left - b.left) < 4) { var x1 = a.left + a.width * .85 - box.left, y1 = a.bottom - box.top, x2 = b.left + b.width * .85 - box.left, y2 = b.top - box.top; d = "M" + x1 + "," + y1 + " C" + (x1 + 14) + "," + (y1 + 10) + " " + (x2 + 14) + "," + (y2 - 10) + " " + x2 + "," + y2; }
        else { var X1 = a.right - box.left, Y1 = a.top + Math.min(26, a.height / 2) - box.top, X2 = b.left - box.left - 2, Y2 = b.top + Math.min(26, b.height / 2) - box.top, mx = (X1 + X2) / 2; d = "M" + X1 + "," + Y1 + " C" + mx + "," + Y1 + " " + mx + "," + Y2 + " " + X2 + "," + Y2; }
        var to = gs[e[1]], fr = gs[e[0]];
        svg.appendChild(s("path", {d: d, class: to && to.active && !to.done ? "flow" : fr && fr.done ? "done" : "", "data-a": e[0], "data-b": e[1]}));
      });
      if (arrowMode === "all" && !pinned) drawAllTaskEdges();
    }
    requestAnimationFrame(function () { requestAnimationFrame(draw); }); window.__adPlanDraw = draw;
    /* Focus on hover: a group's upstream and downstream light up and animate; everything else dims. */
    var up = {}, down = {};
    (plan.edges || []).forEach(function (e) { (down[e[0]] = down[e[0]] || []).push(e[1]); (up[e[1]] = up[e[1]] || []).push(e[0]); });
    function reach(start, map) { var seen = {}, stack = [start]; while (stack.length) { var x = stack.pop(); (map[x] || []).forEach(function (y) { if (!seen[y]) { seen[y] = 1; stack.push(y); } }); } return seen; }
    function groupEnter(gid) {
      if (pinned) return;
      var ups = reach(gid, up), downs = reach(gid, down);
      dag.classList.add("focus");
      Object.keys(nodes).forEach(function (k) { nodes[k].classList.toggle("f-self", k === gid); nodes[k].classList.toggle("f-up", !!ups[k]); nodes[k].classList.toggle("f-down", !!downs[k]); });
      [].forEach.call(svg.querySelectorAll("path"), function (pth) {
        var a = pth.getAttribute("data-a"), b = pth.getAttribute("data-b");
        var on = (b === gid || ups[b]) && (a === gid || ups[a]) ? "e-up" : (a === gid || downs[a]) && (b === gid || downs[b]) ? "e-down" : "";
        pth.classList.remove("e-up", "e-down"); if (on) pth.classList.add(on);
      });
    }
    function groupClear() {
      dag.classList.remove("focus");
      Object.keys(nodes).forEach(function (k) { nodes[k].classList.remove("f-self", "f-up", "f-down"); });
      [].forEach.call(svg.querySelectorAll("path"), function (pth) { pth.classList.remove("e-up", "e-down"); });
    }
    Object.keys(nodes).forEach(function (gid) {
      nodes[gid].addEventListener("mouseenter", function () { groupEnter(gid); });
      nodes[gid].addEventListener("mouseleave", function () { if (!pinned) { groupClear(); clearTask(); } });
    });
    /* Task focus: lines from the chips a task waits on (amber) and to the chips it unblocks (blue), across tiles. */
    var pinned = null, tsvg = s("svg", {class: "tedges", "data-diagram": "task-deps"}); dag.appendChild(tsvg);
    dag.addEventListener("click", function (ev) { if (pinned && !(ev.target.closest && ev.target.closest(".chip"))) { pinned = null; clearTask(); groupClear(); } });
    function clearTask(silent) {
      dag.classList.remove("tfocus");
      Object.keys(chipEl).forEach(function (k) { chipEl[k].classList.remove("t-self", "t-up", "t-down", "t-near"); });
      Object.keys(nodes).forEach(function (k) { nodes[k].classList.remove("t-lit"); });
      while (tsvg.firstChild) tsvg.removeChild(tsvg.firstChild);
      if (!silent && arrowMode === "all" && !pinned) drawAllTaskEdges();
    }
    function svgTitle(text) { var t = document.createElementNS("http://www.w3.org/2000/svg", "title"); t.textContent = text; return t; }
    function ensureTaskMarkers() {
      if (tsvg.querySelector("defs")) return;
      var defs = s("defs"); ["t-up", "t-down", "a-met", "a-un"].forEach(function (cl) { var m = s("marker", {id: "tm-" + plan.ws + "-" + cl, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse"}); m.appendChild(s("path", {d: "M0,0 L10,5 L0,10 z", class: "tm " + cl})); defs.appendChild(m); }); tsvg.appendChild(defs);
    }
    /* Shared router: each link gets its own slot on the chip edge (sorted by where the far end is) and its own bend, so lines fan out instead of piling up. */
    function drawTaskLinks(links) {
      ensureTaskMarkers();
      tsvg.setAttribute("width", dag.scrollWidth); tsvg.setAttribute("height", dag.scrollHeight);
      var box = dag.getBoundingClientRect(), L = box.left, T = box.top, R = {};
      function rect(k) { if (!R[k]) { var r = chipEl[k].getBoundingClientRect(); R[k] = {l: r.left - L, r: r.right - L, t: r.top - T, b: r.bottom - T, cx: (r.left + r.right) / 2 - L, cy: (r.top + r.bottom) / 2 - T, w: r.width, h: r.height}; } return R[k]; }
      var sides = {};
      links = links.filter(function (k) { return chipEl[k.a] && chipEl[k.b] && !foldedIds[k.a] && !foldedIds[k.b]; });
      links.forEach(function (k) {
        var A = rect(k.a), B = rect(k.b), dx = B.cx - A.cx, dy = B.cy - A.cy;
        if (tGroup[k.a] === tGroup[k.b] && Math.abs(dy) < 4) { k.kind = "arc"; k.sa = "t"; k.sb = "t"; }
        else if (Math.abs(dx) > Math.abs(dy)) { k.kind = "h"; k.sa = dx > 0 ? "r" : "l"; k.sb = dx > 0 ? "l" : "r"; }
        else { k.kind = "v"; k.sa = dy > 0 ? "b" : "t"; k.sb = dy > 0 ? "t" : "b"; }
        (sides[k.a + "|" + k.sa] = sides[k.a + "|" + k.sa] || []).push({k: k, end: "a", far: B});
        (sides[k.b + "|" + k.sb] = sides[k.b + "|" + k.sb] || []).push({k: k, end: "b", far: A});
      });
      Object.keys(sides).forEach(function (key) {
        var list = sides[key], chip = key.split("|")[0], side = key.split("|")[1], me = rect(chip), n = list.length;
        var horiz = side === "l" || side === "r";
        list.sort(function (p, q) { return horiz ? p.far.cy - q.far.cy : p.far.cx - q.far.cx; });
        list.forEach(function (it, i) {
          var f = n === 1 ? 0.5 : 0.15 + 0.7 * i / (n - 1);
          var pt = horiz ? {x: side === "r" ? me.r : me.l, y: me.t + me.h * f} : {x: me.l + me.w * f, y: side === "b" ? me.b : me.t};
          it.k[it.end === "a" ? "pa" : "pb"] = pt;
        });
      });
      var gut = {};
      links.forEach(function (k) {
        var a = k.pa, b = k.pb, d;
        var lane = (gut[k.kind + Math.round((a.x + b.x) / 40)] = (gut[k.kind + Math.round((a.x + b.x) / 40)] || 0) + 1) - 1, off = (lane % 2 ? 1 : -1) * Math.ceil(lane / 2) * 7;
        if (k.kind === "arc") { var lift = 12 + Math.min(36, Math.abs(b.x - a.x) * .22) + Math.abs(off) * .6; d = "M" + a.x + "," + a.y + " C" + a.x + "," + (a.y - lift) + " " + b.x + "," + (b.y - lift) + " " + b.x + "," + (b.y - 2); }
        else if (k.kind === "h") { var mx = (a.x + b.x) / 2 + off, x2 = b.x + (k.sb === "l" ? -2 : 2); d = "M" + a.x + "," + a.y + " C" + mx + "," + a.y + " " + mx + "," + b.y + " " + x2 + "," + b.y; }
        else { var my = (a.y + b.y) / 2 + off, y2 = b.y + (k.sb === "t" ? -2 : 2); d = "M" + a.x + "," + a.y + " C" + a.x + "," + my + " " + b.x + "," + my + " " + b.x + "," + y2; }
        tsvg.appendChild(s("path", {d: d, class: "tl " + k.cls + (k.near ? " near" : ""), "marker-end": "url(#tm-" + plan.ws + "-" + k.cls + ")"}));
        /* Wide invisible hit path: hover shows the edge condition without blocking chip clicks. */
        var hit = s("path", {d: d, class: "thit"});
        hit.appendChild(svgTitle(edgeTitle(k.a, k.b)));
        hit.addEventListener("click", function (ev) {
          ev.stopPropagation();
          hit.style.pointerEvents = "none";
          var under = document.elementFromPoint(ev.clientX, ev.clientY);
          hit.style.pointerEvents = "";
          var chip = under && under.closest ? under.closest(".chip") : null;
          if (chip) chip.click();
          else if (pinned) { pinned = null; clearTask(); groupClear(); }
        });
        tsvg.appendChild(hit);
      });
    }
    /* Rest state: every task dependency as one arrow, styled by whether the prerequisite is done. */
    function drawAllTaskEdges() {
      if (!hasTaskDeps || !dag.getBoundingClientRect().width) return;
      var links = [];
      Object.keys(tUp).forEach(function (b) { (tUp[b] || []).forEach(function (a) { links.push({a: a, b: b, cls: depMet(a) ? "a-met" : "a-un", near: false}); }); });
      drawTaskLinks(links);
    }
    function focusTask(id) {
      clearTask(true); groupClear();
      var ups = reach(id, tUp), downs = reach(id, tDown);
      dag.classList.add("tfocus");
      function lit(k, cls) { if (!chipEl[k]) return; chipEl[k].classList.add(cls); nodes[tGroup[k]].classList.add("t-lit"); }
      lit(id, "t-self"); Object.keys(ups).forEach(function (k) { lit(k, "t-up"); }); Object.keys(downs).forEach(function (k) { lit(k, "t-down"); });
      (tUp[id] || []).concat(tDown[id] || []).forEach(function (k) { if (chipEl[k]) chipEl[k].classList.add("t-near"); });
      /* Collect links, then draw them through the shared router. */
      var links = [], full = pinned === id;
      if (full) {
        Object.keys(ups).concat([id]).forEach(function (x) { (tUp[x] || []).forEach(function (d) { if (ups[d]) links.push({a: d, b: x, cls: "t-up", near: x === id}); }); });
        Object.keys(downs).forEach(function (y) { (tUp[y] || []).forEach(function (d) { if (d === id || downs[d]) links.push({a: d, b: y, cls: "t-down", near: d === id}); }); });
      } else {
        (tUp[id] || []).forEach(function (d) { links.push({a: d, b: id, cls: "t-up", near: true}); });
        (tDown[id] || []).forEach(function (y) { links.push({a: id, b: y, cls: "t-down", near: true}); });
      }
      drawTaskLinks(links);
    }
    /* Narrative phases strip (standard B8): one line on the execution map;
       the full prose cards stay on the plan view where they belong. */
    if (L && L.phases_line) c.appendChild(el("div", {class: "phases-line", title: "Full prose lives on the plan view", text: L.phases_line}));
    else if (plan.phases && plan.phases.length) { var ph = el("div", {class: "phases"}); plan.phases.forEach(function (p) { ph.appendChild(el("div", {class: "phase", title: p.gate ? "Done when: " + p.gate : ""}, [el("b", {text: p.name}), p.goal, p.gate ? el("small", {class: "gate", text: "Done when \u24D8"}) : null])); }); c.appendChild(ph); }
    /* Replay: play this workstream's history as a sped-up animation on the map.
       Chips start gray, pulse in their tool color while a mapped run is
       active, and turn done at their done moment; unblocking a dependent
       flashes its arrows for about 600 ms. Live refresh stays frozen while
       the replay owns the card; Esc or Live rebuilds the live map. */
    var RP_FAM = {opus: "var(--s1)", haiku: "var(--s2)", sonnet: "var(--s3)", grok: "var(--s4)", muse: "var(--s5)", glm: "var(--s6)", other: "var(--other)"};
    var RP_REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var RP = {on: false, playing: false, loading: false, tasks: {}, prevDone: {}, savedCss: {}, elapsed: 0, duration: 15, raf: null, compress: false, warp: null, tlRuns: [], tl: null, groups: {}, clip: false};
    var replayBtn = el("button", {class: "planbtn", title: "Play this workstream's history as a sped-up animation", text: "Replay"});
    replayBtn.addEventListener("click", function () { startReplay(true); });
    c.firstChild.appendChild(replayBtn);
    function rpTotal() { return (plan.progress && plan.progress.total) || 0; }
    function rpDoneAt(T) {
      var ids = Object.keys(RP.tasks), n = 0;
      ids.forEach(function (id) { var t = RP.tasks[id]; if (t && t.status !== "deferred" && t.done_at != null && t.done_at <= T) n++; });
      return n;
    }
    /* ONE playhead time source for the map and the run timeline: both paint
       from rpCursorTime, so scrubbing either one moves both. In clip mode
       (and with "Compress idle" on) idle stretches of the real span are
       warped by cumulative event count blended with real time; the date
       label still shows the real date at the playhead. */
    function rpClamp01(frac) { return Math.min(1, Math.max(0, +frac || 0)); }
    function rpBuildWarp(events, span) {
      var seen = {}, ts = [];
      (events || []).forEach(function (e) { if (e.t != null && !seen[e.t]) { seen[e.t] = 1; ts.push(e.t); } });
      ts.sort(function (a, b) { return a - b; });
      if (ts.length < 2) return null;
      var len = Math.max(1, span.end - span.start), blend = 0.75, pts = [];
      ts.forEach(function (t, i) {
        pts.push({w: blend * i / (ts.length - 1) + (1 - blend) * (t - span.start) / len, t: t});
      });
      return pts;
    }
    /* The playhead (position -> time) and the timeline axis (time ->
       position) are one shared mapping: rpWarpT inverts rpWarpX through
       the same warp points (AD.warpTime/AD.warpPos), so the map date,
       cursor, bars, strip and ticks agree at every moment. */
    function rpWarpT(frac) {
      return warpTime(RP.warp, frac, RP.span.start, RP.span.end);
    }
    function rpWarpX(t) {
      return warpPos(RP.warp, t, RP.span.start, RP.span.end);
    }
    function rpTimeToFrac(t) {
      if (RP.compress && RP.warp) return rpWarpX(t);
      return (t - RP.span.start) / RP.spanLen;
    }
    function rpCursorTime(frac) {
      frac = rpClamp01(frac);
      if (RP.compress && RP.warp) return rpWarpT(frac);
      return RP.span.start + frac * (RP.span.end - RP.span.start);
    }
    /* Plain-words label for a quiet stretch, e.g. "2 days quiet". */
    function rpQuietLabel(seconds) {
      var s = Math.max(0, Math.round(seconds));
      if (s >= 129600) { var d = Math.round(s / 86400); return d + (d === 1 ? " day quiet" : " days quiet"); }
      if (s >= 7200) { var h = Math.round(s / 3600); return h + " hours quiet"; }
      if (s >= 120) { var m = Math.round(s / 60); return m + " min quiet"; }
      return s + "s quiet";
    }
    /* Quiet stretches squeezed by the warp: consecutive warp points at
       least RP_BREAK_S apart in real time. Drawn as one dashed guide plus
       a plain-text label each when the axis is warped. */
    var RP_BREAK_S = 6 * 3600;
    function rpWarpBreaks() {
      if (!RP.compress || !RP.warp || RP.warp.length < 2) return [];
      var out = [];
      for (var i = 1; i < RP.warp.length; i++) {
        var a = RP.warp[i - 1], b = RP.warp[i], dur = b.t - a.t;
        if (dur >= RP_BREAK_S) out.push({a: a.t, b: b.t, label: rpQuietLabel(dur)});
      }
      return out.slice(0, 6);
    }
    /* Runs of one replay task active at T (same stub rule as the live map:
       a run with no end time counts briefly after its start). */
    function rpActiveRuns(rt, T) {
      var out = [];
      if (!rt || !rt.runs) return out;
      for (var i = 0; i < rt.runs.length; i++) {
        var r = rt.runs[i];
        if (r.start == null || r.start > T) continue;
        var e = r.end == null ? r.start + RP.spanLen * 0.01 : r.end;
        if (T <= e) out.push(r);
      }
      return out;
    }
    function rpIsAgentRun(r) { var src = r.source; return src !== "claude-main" && src !== "shell" && src !== "dagster"; }
    function rpPaint(frac, fire) {
      var T = rpCursorTime(frac), now = {};
      Object.keys(RP.tasks).forEach(function (tid) {
        var t = RP.tasks[tid];
        if (t && t.status !== "deferred" && t.done_at != null && t.done_at <= T) now[tid] = 1;
      });
      if (fire) {
        var freshIds = Object.keys(now).filter(function (tid) { return !RP.prevDone[tid]; });
        if (freshIds.length) rpFlash(freshIds, T);
      }
      RP.prevDone = now;
      rpFlashSweep();
      Object.keys(chipEl).forEach(function (tid) {
        var cp = chipEl[tid], t = RP.tasks[tid];
        if (!cp || !t || t.status === "deferred") return;
        var done = !!now[tid], gate = tKind[tid] === "gate";
        var actives = (!done && !RP_REDUCED) ? rpActiveRuns(t, T) : [];
        var active = actives.length ? actives[0] : null;
        if (done) {
          cp.className = "chip done" + (gate ? " gate" : "");
          cp.style.cssText = RP.savedCss[tid] || "";
        } else if (active) {
          var col = RP_FAM[active.family] || RP_FAM.other;
          cp.className = "chip live";
          cp.style.cssText = RP.savedCss[tid] || "";
          cp.style.background = col;
          cp.style.borderColor = col;
          cp.style.color = "#fff";
        } else {
          cp.className = "chip" + (gate ? " gate" : "");
          cp.style.cssText = RP.savedCss[tid] || "";
        }
      });
      var total = rpTotal(), n = rpDoneAt(T), pct = total ? Math.round(100 * n / total) : 0;
      [].forEach.call(obBar.children, function (sp) {
        sp.style.transition = "none";
        sp.style.width = (sp.dataset.k === "done" && total ? (100 * n / total) : 0) + "%";
      });
      obPct.textContent = pct + "%";
      RP.txt.innerHTML = "";
      RP.txt.appendChild(el("b", {text: n + " of " + total + " tasks done"}));
      RP.txt.appendChild(document.createTextNode(" · " + (total - n) + " to go · "));
      RP.txt.appendChild(el("span", {class: "rp-tag", text: "replay"}));
      RP.date.textContent = fmtDate(T);
      RP.scrub.value = String(Math.round(frac * 1000));
      rpPaintGroups(T);
      rpPaintTimeline(T);
      if (RP.clip) rpPaintClip(frac, T);
    }
    /* Group tiles in replay come from replay state at the playhead, never
       live state: each tile's pill, count, bar and done/active styling. */
    function rpGroupEls(gid) {
      var hit = RP.groups[gid];
      if (hit && hit.node && hit.node.isConnected) return hit;
      if (hit) delete RP.groups[gid];
      var node = nodes[gid];
      if (!node) return null;
      var minis = node.querySelectorAll(".mini > span");
      hit = RP.groups[gid] = {
        node: node,
        tag: node.querySelector(".gtag"),
        count: node.querySelector(".m"),
        mini: [minis[0], minis[1], minis[2]],
        badge: node.querySelector(".live-n"),
      };
      return hit;
    }
    function rpPaintGroups(T) {
      var gDone = {};
      plan.groups.forEach(function (g) {
        var tot = 0, n = 0;
        g.tasks.forEach(function (t) {
          if (t.status === "deferred" || t.status === "moved") return;
          var rt = RP.tasks[t.id];
          if (!rt) return;
          tot++;
          if (rt.status !== "deferred" && rt.done_at != null && rt.done_at <= T) n++;
        });
        gDone[g.id] = tot > 0 && n === tot;
      });
      var colDone = {}, colTot = {}, colNow = {};
      plan.groups.forEach(function (g) {
        var els = rpGroupEls(g.id);
        if (!els) return;
        var tot = 0, n = 0, wip = 0, liveN = 0, lastAct = -1, started = false;
        g.tasks.forEach(function (t) {
          if (t.status === "deferred" || t.status === "moved") return;
          var rt = RP.tasks[t.id];
          if (!rt) return;
          tot++;
          if (rt.status !== "deferred" && rt.done_at != null && rt.done_at <= T) { n++; started = true; }
          else {
            if (rt.first_active != null && rt.first_active <= T) {
              started = true;
              if (rt.first_active > lastAct) lastAct = rt.first_active;
            }
            var act = RP_REDUCED ? [] : rpActiveRuns(rt, T);
            if (act.length) { wip++; liveN += act.length; }
          }
        });
        colDone[g.col] = (colDone[g.col] || 0) + n;
        colTot[g.col] = (colTot[g.col] || 0) + tot;
        if (liveN > 0 || wip > 0) colNow[g.col] = true;
        var waitOn = (deps[g.id] || []).filter(function (d) { return !gDone[d]; });
        var stage, cls, why;
        if (tot > 0 && n === tot) { stage = "✓ Done"; cls = "done"; why = "Every task is merged and live."; }
        else if (liveN > 0 || wip > 0) { stage = "▶ Building"; cls = "now"; why = "Agents are building tasks in this stage."; }
        else if (!waitOn.length) { stage = started ? "Partly done" : "Ready to start"; cls = "ready"; why = started ? "Some tasks are done and nothing is running now." : "Everything it waits on is done."; }
        else { stage = "Waiting"; cls = "wait"; why = "Waits on " + waitOn.map(function (d) { return (gs[d] && gs[d].title) || d; }).join(", ") + "."; }
        els.node.classList.remove("s-done", "s-prog", "s-live", "g-done", "g-now", "g-ready");
        if (tot > 0 && n === tot) els.node.classList.add("s-done", "g-done");
        else if (liveN > 0 || wip > 0) els.node.classList.add("s-prog", "g-now");
        else if (n > 0) els.node.classList.add("s-prog");
        else if (!waitOn.length) els.node.classList.add("g-ready");
        if (els.tag) { els.tag.className = "gtag " + cls; els.tag.textContent = stage; els.tag.title = why; }
        if (els.count) {
          els.count.innerHTML = "";
          els.count.appendChild(el("b", {text: n + " of " + tot}));
          els.count.appendChild(document.createTextNode(" done" + (tot - n ? " · " + (tot - n) + " to go" : "")));
          els.count.title = n + " done, " + wip + " in progress, " + (tot - n - wip) + " not started";
        }
        if (els.mini[0]) els.mini[0].style.width = (tot ? 100 * n / tot : 0) + "%";
        if (els.mini[1]) els.mini[1].style.width = "0%";
        if (els.mini[2]) els.mini[2].style.width = "0%";
        if (liveN > 0) {
          if (!els.badge) {
            els.badge = el("div", {class: "live-n", text: ""});
            els.node.insertBefore(els.badge, els.count ? els.count.nextSibling : null);
            RP.groups[g.id] = els;
          }
          els.badge.style.display = "";
          els.badge.textContent = "● " + liveN + " agent" + (liveN > 1 ? "s" : "") + " working";
        } else if (els.badge) els.badge.style.display = "none";
        RP.groupState = RP.groupState || {};
        RP.groupState[g.id] = {done: tot > 0 && n === tot, started: started, lastAct: lastAct, stage: stage, title: g.title};
      });
      (plan.cols || []).forEach(function (name, i) {
        var dcol = cols[i];
        if (!dcol) return;
        var bar = dcol.querySelector(".colbar > i");
        if (bar) bar.style.width = (colTot[i] ? 100 * (colDone[i] || 0) / colTot[i] : 0) + "%";
        dcol.classList.toggle("c-now", !!colNow[i]);
      });
    }
    /* The run timeline in replay: one strip of agents at once plus one lane
       per model family, drawn from the same /api/replay span as the map and
       painted from the same rpCursorTime playhead. Bars appear as their run
       starts and grow until it ends; a vertical cursor sweeps the shared
       time axis. Clicking or dragging the plot scrubs both cards. */
    function rpOutcomeOf(run) {
      if (!run.end) return run.status === "running" ? "run" : "unk";
      if (run.status === "died_early" || run.status === "killed" || run.verdict === "BLOCKED" || run.verdict === "REQUEST_CHANGES" || run.verdict === "FAILURE" || run.verdict === "CANCELED") return "bad";
      if (run.verdict === "PARTIAL" || run.verdict === "APPROVE_WITH_CHANGES") return "part";
      if (run.status === "done" || run.verdict === "SUCCESS") return "ok";
      return "unk";
    }
    /* Pack runs into rows in axis space (rpTimeToFrac), not wall-clock
       seconds, so rows follow the same warp as the axis: runs across a
       squeezed quiet stretch share rows instead of leaving tall empty
       bands. gapF is a fraction of the axis width. */
    function rpPackRuns(runs, gapF) {
      var rows = [];
      runs.slice().sort(function (x, y) { return x.start - y.start; }).forEach(function (r) {
        var x0 = rpTimeToFrac(r.start), x1 = rpTimeToFrac(r.end == null ? r.start + 60 : r.end), placed = false;
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].end + gapF <= x0) { rows[i].runs.push(r); rows[i].end = x1; placed = true; break; }
        }
        if (!placed) rows.push({end: x1, runs: [r]});
      });
      return rows;
    }
    function rpTimelineCard() {
      var c = el("div", {class: "card", id: "exec-timeline", "data-nav": "Run timeline"}, [el("h2", {text: "Run timeline"}), el("p", {class: "sub", text: "The same playhead as the plan progress map above: bars appear as their run starts and grow until it ends. The strip is how many worker agents ran at each moment. Click or drag to scrub both; hover a bar for the run."})]);
      var lg = el("div", {class: "legend"});
      ["run", "ok", "part", "bad"].forEach(function (k) {
        var o = OUTCOME[k], sp = el("span");
        var sw = el("i", {class: "sw"}); sw.style.background = o[0]; sp.appendChild(sw);
        sp.appendChild(document.createTextNode(o[1] + " " + o[2])); lg.appendChild(sp);
      });
      c.appendChild(lg);
      var W = CW(), L = 150, Rm = 14, pw = W - L - Rm, stripH = 28, rowH = 6, rowGap = 2;
      var t0 = RP.span.start, t1 = RP.span.end;
      /* The shared warp mapping: the axis, ticks, strip, bars, veil and
         cursor all position through rpTimeToFrac, the inverse of the
         rpCursorTime playhead, so everything lines up with the map. */
      var X = function (t) { return L + pw * rpTimeToFrac(t); };
      var breaks = rpWarpBreaks();
      var breakX = breaks.map(function (br) { return (X(br.a) + X(br.b)) / 2; });
      var fams = {};
      RP.tlRuns.forEach(function (r) { fams[r.family || "other"] = true; });
      var order = Object.keys(fams).sort();
      var lanes = order.map(function (f) {
        var rows = rpPackRuns(RP.tlRuns.filter(function (r) { return (r.family || "other") === f; }), 6 / pw);
        return {fam: f, name: f.charAt(0).toUpperCase() + f.slice(1), rows: rows, h: Math.max(rows.length * (rowH + rowGap), 10) + 2};
      });
      var lanesH = lanes.reduce(function (a, l) { return a + l.h; }, 0);
      var plotB = 12 + stripH + 8 + lanesH, H = plotB + 30;
      var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": "Agent runs over the replay span, one lane per model family"});
      var defs = s("defs", {}), pat = s("pattern", {id: "rp-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"});
      pat.appendChild(s("rect", {width: 6, height: 6, fill: "var(--surface-2)"}));
      pat.appendChild(s("line", {x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--muted)", "stroke-width": 2}));
      defs.appendChild(pat); svg.appendChild(defs);
      /* Real date/time tick labels placed through the shared helper on the
         warped axis; crowded ticks and ticks near a break marker are
         thinned, never relabeled. */
      var tickGap = Math.max(36, pw / 28);
      var ticks = timeTicks(t0, t1, X, 6, {minGap: tickGap, avoid: breakX});
      ticks.forEach(function (k) { svg.appendChild(s("line", {x1: k.x, x2: k.x, y1: 4, y2: plotB, class: "gl"})); });
      /* One dashed guide plus a plain-words label per squeezed idle
         stretch, so the compressed axis reads honestly. */
      var labeledX = -1e9;
      breaks.forEach(function (br, i) {
        var bx = breakX[i];
        svg.appendChild(s("line", {x1: bx, x2: bx, y1: 4, y2: plotB, class: "gl", "stroke-dasharray": "3 3"}));
        svg.appendChild(s("line", {x1: bx - 6, x2: bx - 1, y1: plotB + 5, y2: plotB - 5, class: "bl"}));
        svg.appendChild(s("line", {x1: bx + 1, x2: bx + 6, y1: plotB + 5, y2: plotB - 5, class: "bl"}));
        /* Labels must fit on the axis: clear of each other, the edges
           and (via the ticks' avoid list) the date/time ticks. The guide
           and slashes still mark a squeeze whose label would not fit. */
        if (bx - labeledX >= tickGap && bx - L >= tickGap && L + pw - bx >= tickGap) {
          var bt = s("text", {x: bx, y: plotB + 15, "text-anchor": "middle", class: "ax"});
          bt.textContent = br.label;
          svg.appendChild(bt);
          labeledX = bx;
        }
      });
      var step = Math.max(60, Math.round((t1 - t0) / 300)), pts = [], mx = 1, y0 = 12;
      for (var t = t0; t <= t1; t += step) {
        var n = 0;
        RP.tlRuns.forEach(function (r) {
          if (!rpIsAgentRun(r) || r.start > t) return;
          var e = r.end == null ? r.start + 60 : r.end;
          if (e >= t) n++;
        });
        pts.push([t, n]); mx = Math.max(mx, n);
      }
      var Y = function (v) { return y0 + stripH - (stripH - 4) * v / mx; };
      svg.appendChild(s("rect", {x: L, y: y0, width: pw, height: stripH, rx: 4, fill: "var(--surface-2)"}));
      var d = "M" + X(pts[0][0]) + "," + (y0 + stripH);
      pts.forEach(function (p) { d += " L" + X(p[0]) + "," + Y(p[1]); });
      d += " L" + X(t1) + "," + (y0 + stripH) + " Z";
      svg.appendChild(s("path", {d: d, fill: "var(--s1)", "fill-opacity": ".55", stroke: "var(--s1)", "stroke-width": 1.2}));
      var lab = s("text", {x: 0, y: y0 + stripH / 2 + 4}); lab.textContent = "agents at once"; lab.setAttribute("style", "fill:var(--ink);font:700 12px var(--font)"); svg.appendChild(lab);
      var cnt = s("text", {x: 0, y: y0 + stripH / 2 + 17, class: "ax"}); cnt.textContent = RP.tlRuns.length + " runs"; svg.appendChild(cnt);
      [[0, y0 + stripH], [mx, y0 + 4]].forEach(function (p) {
        svg.appendChild(s("line", {x1: L - 4, x2: L, y1: p[1], y2: p[1], class: "bl"}));
        var tk = s("text", {x: L - 7, y: p[1] + 3.5, "text-anchor": "end", class: "ax"}); tk.textContent = fmtN(p[0]); svg.appendChild(tk);
      });
      var veil = s("rect", {x: L, y: y0, width: pw, height: stripH, fill: "var(--surface)", opacity: ".72"});
      svg.appendChild(veil);
      var bars = [], by = y0 + stripH + 8;
      lanes.forEach(function (lane) {
        var ml = s("text", {x: 10, y: by + 9}); ml.textContent = lane.name + " · " + lane.rows.reduce(function (a, row) { return a + row.runs.length; }, 0); ml.setAttribute("style", "fill:var(--ink-2);font:600 11.5px var(--font)"); svg.appendChild(ml);
        var ry = by;
        lane.rows.forEach(function (row) {
          row.runs.forEach(function (r) {
            var bar = s("rect", {y: ry, height: rowH, rx: 2, fill: "var(--s1)", class: "rp-tl-bar"});
            bar.setAttribute("data-task", r.task);
            bar.addEventListener("mousemove", function (ev) {
              var o = OUTCOME[rpOutcomeOf({end: r.end, status: r.status, verdict: r.verdict})] || OUTCOME.run;
              showTip(ev, r.label || r.task, [{k: "outcome", v: o[1] + " " + (r.verdict || o[2]), c: o[0]}, {k: "model", v: r.family || "other"}, {k: "elapsed", v: r.end ? fmtDur(r.end - r.start) : "end not recorded"}, {k: "task", v: r.task}]);
              if (chipEl[r.task]) chipEl[r.task].classList.add("rp-hi");
            });
            bar.addEventListener("mouseleave", function () { hideTip(); if (chipEl[r.task]) chipEl[r.task].classList.remove("rp-hi"); });
            svg.appendChild(bar);
            bars.push({el: bar, run: r});
          });
          ry += rowH + rowGap;
        });
        by += lane.h;
      });
      axes(svg, {L: L, T: 4, pw: pw, ph: plotB - 4, yMax: mx, yTicks: 1, xTicks: ticks, xTitle: "time", yTitle: null});
      var cursor = s("line", {x1: L, x2: L, y1: 4, y2: plotB, stroke: "var(--s1)", "stroke-width": 1.5});
      svg.appendChild(cursor);
      var scrub = s("rect", {x: L, y: 4, width: pw, height: plotB - 4, fill: "transparent"});
      scrub.style.cursor = "ew-resize";
      var dragging = false;
      var toFrac = function (ev) {
        var bb = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
        var px = (ev.clientX - bb.left) * vb.width / bb.width;
        return rpClamp01((px - L) / pw);
      };
      scrub.addEventListener("mousedown", function (ev) { dragging = true; rpSeek(toFrac(ev)); });
      scrub.addEventListener("mousemove", function (ev) { if (dragging) rpSeek(toFrac(ev)); });
      scrub.addEventListener("mouseup", function () { dragging = false; });
      scrub.addEventListener("mouseleave", function () { dragging = false; });
      svg.appendChild(scrub);
      c.appendChild(svg);
      RP.tl = {card: c, svg: svg, X: X, L: L, pw: pw, cursor: cursor, veil: veil, bars: bars, plotB: plotB};
      return c;
    }
    /* One seek path for the map scrub, the timeline scrub and the render
       driver: sets the shared playhead and repaints both cards. */
    function rpSyncPrev(T) {
      var cur = {};
      Object.keys(RP.tasks).forEach(function (tid) {
        var t = RP.tasks[tid];
        if (t && t.status !== "deferred" && t.done_at != null && t.done_at <= T) cur[tid] = 1;
      });
      RP.prevDone = cur;
    }
    function rpSeek(frac) {
      frac = rpClamp01(frac);
      RP.elapsed = frac * RP.duration;
      var T = rpCursorTime(frac);
      rpSyncPrev(T);
      rpPaint(frac, false);
      if (RP.elapsed < RP.duration) window.__adReplay.done = false;
      else window.__adReplay.done = true;
    }
    /* Timeline paint reads the same rpCursorTime as the map: bars appear at
       their run start and grow to the playhead, the veil lifts, the cursor
       sweeps the shared axis. */
    function rpPaintTimeline(T) {
      var tl = RP.tl;
      if (!tl || !RP.span) return;
      var x = tl.X(T);
      tl.cursor.setAttribute("x1", x);
      tl.cursor.setAttribute("x2", x);
      tl.veil.setAttribute("x", Math.min(x, tl.L + tl.pw));
      tl.veil.setAttribute("width", Math.max(0, tl.L + tl.pw - x));
      tl.bars.forEach(function (b) {
        var r = b.run;
        if (r.start == null || r.start > T) { b.el.style.display = "none"; return; }
        b.el.style.display = "";
        var end = r.end == null || r.end > T ? T : r.end;
        var x1 = tl.X(r.start), x2 = tl.X(Math.max(end, r.start));
        b.el.setAttribute("x", x1);
        b.el.setAttribute("width", Math.max(3, x2 - x1 - 1));
        var live = r.end == null || r.end > T;
        var fill = live ? "var(--s1)" : (function () {
          var o = OUTCOME[rpOutcomeOf(r)];
          return o ? o[0] : "var(--muted)";
        })();
        if (r.clock === "mtime") { b.el.setAttribute("fill", "url(#rp-hatch)"); b.el.setAttribute("stroke", fill); }
        else { b.el.setAttribute("fill", fill); b.el.setAttribute("stroke", "none"); }
      });
    }
    /* Hovering a map chip highlights its timeline bars; hovering a timeline
       bar highlights its chip (see the bar listeners above). */
    /* The timeline card is rebuilt when the warp toggles (grid, ticks and
       breaks follow the axis), while chip listeners stay linked once: they
       read the current RP.tl at fire time. */
    function rpRebuildTimeline() {
      if (!RP.tlSec) return;
      RP.tlSec.innerHTML = "";
      RP.tlSec.appendChild(rpTimelineCard());
    }
    function rpLinkHover() {
      if (RP.hoverLinked) return;
      RP.hoverLinked = true;
      Object.keys(chipEl).forEach(function (tid) {
        var cp = chipEl[tid];
        if (!cp) return;
        cp.addEventListener("mouseenter", function () {
          if (!RP.tl) return;
          RP.tl.bars.forEach(function (b) { if (b.run.task === tid) b.el.classList.add("rp-hi"); });
        });
        cp.addEventListener("mouseleave", function () {
          if (!RP.tl) return;
          RP.tl.bars.forEach(function (b) { if (b.run.task === tid) b.el.classList.remove("rp-hi"); });
        });
      });
    }
    /* Flash the arrows from just-finished tasks to the dependents whose
       deps are all done now, plus a short outline on those chips. One
       capped layer, redrawn per frame and swept when it expires, so dense
       finishes read as a beat instead of a hairball. */
    function rpFreshLinks(tid, T) {
      return (tDown[tid] || []).filter(function (dep) {
        return (tUp[dep] || []).every(function (d) {
          var t = RP.tasks[d];
          return t && t.done_at != null && t.done_at <= T;
        });
      }).map(function (dep) { return {a: tid, b: dep}; });
    }
    function rpFlashClear() {
      (RP.flashNodes || []).forEach(function (node) { if (node.parentNode === tsvg) tsvg.removeChild(node); });
      (RP.flashChips || []).forEach(function (tid) { if (chipEl[tid]) chipEl[tid].classList.remove("rp-unblock"); });
      RP.flashNodes = [];
      RP.flashChips = [];
    }
    function rpFlash(ids, T) {
      var seen = {}, links = [];
      ids.reduce(function (all, tid) { return all.concat(rpFreshLinks(tid, T)); }, []).concat(RP.flashLinks || []).forEach(function (link) {
        var key = link.a + "|" + link.b;
        if (!seen[key]) { seen[key] = 1; links.push(link); }
      });
      RP.flashLinks = links.slice(0, 12);
      rpFlashClear();
      var chips = {};
      RP.flashLinks.forEach(function (link) { chips[link.b] = 1; });
      RP.flashChips = Object.keys(chips);
      RP.flashChips.forEach(function (tid) { if (chipEl[tid]) chipEl[tid].classList.add("rp-unblock"); });
      if (!RP.flashLinks.length) return;
      var before = tsvg.childNodes.length;
      try {
        drawTaskLinks(RP.flashLinks.map(function (link) { return {a: link.a, b: link.b, cls: "t-down near"}; }));
      } catch (e) { return; }
      RP.flashNodes = Array.prototype.slice.call(tsvg.childNodes, before);
      /* Clip speed needs a longer beat so the unblock stays readable. */
      RP.flashUntil = performance.now() + (RP.clip ? 1400 : 600);
    }
    function rpFlashSweep() {
      if ((RP.flashNodes || []).length && performance.now() > (RP.flashUntil || 0)) rpFlashClear();
    }
    function rpTick(ts) {
      if (!RP.on || !RP.playing) return;
      var dt = ts - RP.last;
      RP.last = ts;
      RP.elapsed += dt / 1000;
      if (RP.elapsed >= RP.duration) {
        RP.elapsed = RP.duration;
        rpPaint(1, true);
        rpSetPlaying(false);
        window.__adReplay.done = true;
        return;
      }
      rpPaint(RP.elapsed / RP.duration, true);
      RP.raf = requestAnimationFrame(rpTick);
    }
    function rpSetPlaying(playing) {
      RP.playing = playing;
      RP.playBtn.textContent = playing ? "Pause" : (RP.elapsed >= RP.duration ? "Play again" : "Play");
      if (playing) {
        if (RP.elapsed >= RP.duration) { RP.elapsed = 0; RP.prevDone = {}; }
        RP.last = performance.now();
        RP.raf = requestAnimationFrame(rpTick);
      } else if (RP.raf) {
        cancelAnimationFrame(RP.raf);
        RP.raf = null;
      }
    }
    function startReplay(autoplay) {
      if (window.__adReplayOn || RP.loading) return;
      RP.loading = true;
      replayBtn.textContent = "Loading…";
      api("/api/replay", {ws: plan.ws}).then(function (data) {
        RP.loading = false;
        replayBtn.textContent = "Replay";
        enterReplay(data, autoplay);
      }).catch(function () {
        RP.loading = false;
        replayBtn.textContent = "Replay";
      });
    }
    /* Title, caption, counters and closing for clip mode: labels and
       counts only, never prompts, argv or secrets. */
    function rpWsName() {
      var hit = null;
      (state.wss || []).forEach(function (w) { if (w.ws === plan.ws) hit = w; });
      return (hit && hit.name) || "";
    }
    function rpShipLine() {
      var n = 0;
      plan.groups.forEach(function (g) {
        g.tasks.forEach(function (t) { if (t.status !== "deferred" && t.status !== "moved") n++; });
      });
      var first = (plan.cols || [])[0] || "", last = (plan.cols || [])[(plan.cols || []).length - 1] || "";
      var line = n + " tasks across " + plan.groups.length + " stages";
      if (first && last && first !== last) line += " · " + first + " to " + last;
      return line;
    }
    function rpBuildClip() {
      var name = rpWsName();
      var title = el("div", {class: "clip-title"}, [el("h1", {text: plan.ws + (name ? " · " + name : "")}), el("p", {text: rpShipLine()})]);
      document.body.appendChild(title);
      RP.clipTitle = title;
      var capStage = el("span", {class: "cap-stage", text: ""});
      var capN = el("span", {class: "cap-n", text: ""});
      var cap = el("div", {class: "rp-cap"}, [capStage, capN]);
      var capSec = el("section", {class: "sec", "data-sec": "rp-cap"}, [cap]);
      var sec = c.parentNode;
      sec.parentNode.insertBefore(capSec, sec);
      RP.capStage = capStage; RP.capN = capN;
      var tot = rpTotal(), toks = 0;
      RP.tlRuns.forEach(function (r) { toks += r.tokens || 0; });
      var close = el("div", {class: "clip-close", style: "opacity:0;visibility:hidden"}, [
        el("div", {class: "panel"}, [
          el("p", {class: "big", text: tot + " tasks · " + fmtN(RP.tlRuns.length) + " agent runs · " + fmtN(toks) + " tokens · " + fmtN(RP.spanLen / 86400) + " days"}),
          el("p", {class: "fine", text: "timeline reconstructed from agent runs"}),
        ]),
      ]);
      document.body.appendChild(close);
      RP.clipClose = close;
      if (!name) {
        api("/api/workstreams").then(function (j) {
          (j.workstreams || []).forEach(function (w) {
            if (w.ws === plan.ws && w.name) title.firstChild.textContent = plan.ws + " · " + w.name;
          });
        }).catch(function () {}).then(function () { window.__adReplay.clipReady = true; });
      } else window.__adReplay.clipReady = true;
    }
    function rpPaintClip(frac, T) {
      if (RP.clipTitle) {
        var showTitle = frac < 0.1;
        RP.clipTitle.style.opacity = showTitle ? "1" : "0";
        RP.clipTitle.style.visibility = showTitle ? "visible" : "hidden";
      }
      if (RP.clipClose) {
        var closingSecs = Math.max(2, RP.duration * 0.13);
        var showClose = RP.elapsed > RP.duration - closingSecs;
        RP.clipClose.style.opacity = showClose ? "1" : "0";
        RP.clipClose.style.visibility = showClose ? "visible" : "hidden";
      }
      if (RP.capStage) {
        var best = null, fallback = null;
        plan.groups.forEach(function (g) {
          var st = (RP.groupState || {})[g.id];
          if (!st) return;
          if (st.stage === "▶ Building" && (!best || st.lastAct > best.lastAct)) best = {title: g.title, lastAct: st.lastAct, kind: "build"};
          if (!st.done && st.started && (!fallback || st.lastAct > fallback.lastAct)) fallback = {title: g.title, lastAct: st.lastAct, stage: st.stage};
        });
        var label;
        if (best) label = "Now building · " + best.title;
        else if (fallback) label = (fallback.stage === "Partly done" ? "Still open · " : fallback.stage === "Ready to start" ? "Ready to start · " : "In progress · ") + fallback.title;
        else if (rpDoneAt(T) >= rpTotal()) label = "Complete · every stage done";
        else label = "Starting · " + (((plan.groups || [])[0] || {}).title || "");
        RP.capStage.textContent = label;
      }
      if (RP.capN) {
        var n = rpDoneAt(T), total = rpTotal(), agents = 0, runs = 0, toks = 0;
        RP.tlRuns.forEach(function (r) {
          if (r.start == null || r.start > T) return;
          runs++;
          toks += r.tokens || 0;
          var e = r.end == null ? T : r.end;
          if (T <= e && rpIsAgentRun(r)) agents++;
        });
        RP.capN.textContent = n + " of " + total + " tasks · " + agents + " working · " + fmtN(runs) + " run" + (runs === 1 ? "" : "s") + " · " + fmtN(toks) + " tokens · day " + fmtN((T - RP.span.start) / 86400) + " of " + fmtN(RP.spanLen / 86400);
      }
    }
    function enterReplay(data, autoplay) {
      if (!data || !data.tasks || !data.span) {
        window.__adReplay = {ready: true, done: true, empty: true, ws: plan.ws};
        return;
      }
      RP.on = true;
      RP.data = data;
      RP.tasks = data.tasks;
      RP.span = data.span;
      RP.spanLen = Math.max(1, data.span.end - data.span.start);
      RP.clip = isClipMode();
      RP.compress = RP.clip;
      RP.warp = rpBuildWarp(data.events, data.span);
      RP.tlRuns = [];
      Object.keys(data.tasks).forEach(function (tid) {
        var t = data.tasks[tid];
        (t.runs || []).forEach(function (r) {
          if (r.start == null) return;
          RP.tlRuns.push({task: tid, start: r.start, end: r.end, verdict: r.verdict || null, status: r.status || null, source: r.source || null, label: r.label, family: r.family, tokens: r.tokens || 0, clock: r.clock || null});
        });
      });
      RP.tlRuns.sort(function (a, b) { return a.start - b.start; });
      RP.duration = 15;
      RP.elapsed = 0;
      RP.prevDone = {};
      RP.savedCss = {};
      RP.groups = {};
      RP.groupState = {};
      RP.hoverLinked = false;
      RP.tl = null;
      window.__adReplayOn = true;
      window.__adReplay = {ready: true, done: false, ws: plan.ws, clip: RP.clip, clipReady: !RP.clip};
      Object.keys(chipEl).forEach(function (tid) {
        var extra = chipEl[tid].querySelectorAll(".ag");
        Array.prototype.forEach.call(extra, function (node) { node.parentNode.removeChild(node); });
        RP.savedCss[tid] = chipEl[tid].style.cssText;
        chipEl[tid].classList.remove("live", "just", "changed");
      });
      /* Live snapshot rows would lie during a historical playback. */
      Array.prototype.forEach.call(c.querySelectorAll(".working-now,.unassigned"), function (node) { node.style.display = "none"; });
      /* The live count-up animation would fight the replay percent. */
      (window.__adCountUp || []).forEach(function (h) { clearInterval(h); });
      try { clearTask(true); } catch (e) {}
      if (arrowMode === "all") { try { drawAllTaskEdges(); } catch (e) {} }
      var bar = el("div", {class: "rp-bar", role: "group", "aria-label": "Replay controls"});
      RP.playBtn = el("button", {text: "Pause", title: "Play or pause the replay"});
      RP.playBtn.addEventListener("click", function () { rpSetPlaying(!RP.playing); });
      var restart = el("button", {text: "Restart", title: "Restart the replay"});
      restart.addEventListener("click", function () { rpSeek(0); rpSetPlaying(true); });
      var seg = el("span", {class: "seg-ctl", title: "Replay length"});
      var b15 = el("button", {text: "15 s", title: "Play the whole history in about 15 seconds"});
      var b30 = el("button", {text: "30 s", title: "Play the whole history in about 30 seconds"});
      function paintSpeed() {
        b15.setAttribute("aria-pressed", String(RP.duration === 15));
        b30.setAttribute("aria-pressed", String(RP.duration === 30));
      }
      b15.addEventListener("click", function () { var f = RP.elapsed / RP.duration; RP.duration = 15; RP.elapsed = f * 15; paintSpeed(); });
      b30.addEventListener("click", function () { var f = RP.elapsed / RP.duration; RP.duration = 30; RP.elapsed = f * 30; paintSpeed(); });
      paintSpeed();
      seg.appendChild(b15); seg.appendChild(b30);
      var cmp = el("button", {text: "Compress idle", title: "Compress idle stretches so motion stays steady"});
      function paintCmp() { cmp.setAttribute("aria-pressed", String(RP.compress)); }
      cmp.addEventListener("click", function () { RP.compress = !RP.compress; paintCmp(); rpRebuildTimeline(); rpSeek(RP.elapsed / RP.duration); });
      paintCmp();
      var clipBtn = el("button", {text: "Clip view", title: "Open the promo clip composition"});
      clipBtn.addEventListener("click", function () {
        var u = new URL(location.href);
        u.searchParams.set("view", "exec");
        u.searchParams.set("ws", plan.ws);
        u.searchParams.set("replay", "1");
        u.searchParams.set("autoplay", "1");
        u.searchParams.set("clip", "1");
        location.href = u.pathname + u.search + u.hash;
      });
      RP.scrub = document.createElement("input");
      RP.scrub.type = "range"; RP.scrub.min = "0"; RP.scrub.max = "1000"; RP.scrub.value = "0";
      RP.scrub.setAttribute("aria-label", "Replay position");
      RP.scrub.addEventListener("input", function () { rpSeek(Number(RP.scrub.value) / 1000); });
      RP.date = el("span", {class: "rp-date", text: ""});
      var live = el("button", {text: "Live", title: "Return to the live map"});
      live.addEventListener("click", function () { exitReplay(false); });
      bar.appendChild(RP.playBtn); bar.appendChild(restart); bar.appendChild(seg); bar.appendChild(cmp); bar.appendChild(clipBtn);
      bar.appendChild(RP.scrub); bar.appendChild(RP.date); bar.appendChild(live);
      RP.bar = bar;
      c.insertBefore(bar, c.children[1]);
      var sec = c.parentNode;
      var tlSec = el("section", {class: "sec", "data-sec": "rp-timeline", id: "x-rp-timeline", style: "margin-top:14px"}, [rpTimelineCard()]);
      sec.parentNode.insertBefore(tlSec, sec.nextSibling);
      RP.tlSec = tlSec;
      rpLinkHover();
      if (RP.clip) rpBuildClip();
      RP.txt = ob.parentNode.querySelector(".pbig-txt");
      rpPaint(0, false);
      rpSetPlaying(autoplay !== false);
      window.__adReplayExit = function (silent) { exitReplay(!!silent); };
      window.__adReplay.seek = function (frac) { rpSeek(frac); return rpClamp01(frac); };
      /* The render driver steps through this: like a live playback, newly
         finished tasks flash their unblock arrows (they clear on their own). */
      window.__adReplay.seekFire = function (frac) {
        frac = rpClamp01(frac);
        rpSetPlaying(false);
        RP.elapsed = frac * RP.duration;
        rpPaint(frac, true);
        if (RP.elapsed < RP.duration) window.__adReplay.done = false;
        else window.__adReplay.done = true;
        return frac;
      };
      window.__adReplay.setDuration = function (s) {
        var f = rpClamp01(RP.elapsed / RP.duration);
        RP.duration = s === 30 ? 30 : 15;
        RP.elapsed = f * RP.duration;
        paintSpeed();
        rpPaint(f, false);
        return RP.duration;
      };
      window.__adReplay.frac = function () { return rpClamp01(RP.elapsed / RP.duration); };
    }
    function exitReplay(silent) {
      if (!RP.on && !window.__adReplayOn) return;
      RP.on = false;
      RP.playing = false;
      if (RP.raf) { cancelAnimationFrame(RP.raf); RP.raf = null; }
      window.__adReplayOn = false;
      window.__adReplayExit = null;
      RP.tl = null;
      RP.clip = false;
      if (RP.clipTitle && RP.clipTitle.parentNode) RP.clipTitle.parentNode.removeChild(RP.clipTitle);
      if (RP.clipClose && RP.clipClose.parentNode) RP.clipClose.parentNode.removeChild(RP.clipClose);
      RP.clipTitle = null; RP.clipClose = null; RP.capStage = null; RP.capN = null;
      document.body.classList.remove("clip", "clip-v");
      delete secCache.plan;
      if (!silent) {
        drawMain();
        refreshAll();
      }
    }
    window.__adReplayExit = null;
    if (!window.__adReplayTried && Q.has("replay")) {
      var wantWs = canonWs(Q.get("ws") || "");
      if (!Q.get("ws") || wantWs === plan.ws) {
        window.__adReplayTried = true;
        setTimeout(function () { startReplay(Q.has("autoplay")); }, 600);
      }
    }
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.planCard = planCard;
  global.planCard = planCard;
  global.loadTaskmap = loadTaskmap;
  global.taskOf = taskOf;
  global.taskOfVerified = taskOfVerified;
  global.shortChipLabel = shortChipLabel;
  global.layoutOf = layoutOf;
  global.planTaskIds = planTaskIds;
  Object.defineProperty(global, "TASKMAP", {
    configurable: true,
    get: function () { return TASKMAP; },
    set: function (v) { TASKMAP = v; }
  });
  Object.defineProperty(AdPage, "TASKMAP", {
    configurable: true,
    get: function () { return TASKMAP; },
    set: function (v) { TASKMAP = v; }
  });
})(window);
