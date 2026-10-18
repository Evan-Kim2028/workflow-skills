/* Page card: Project roadmap swimlane (?view=project and ?view=planned).
   One lane per workstream over time: bars run from the first recorded
   run or history move to done or to today, filled by the server plan
   progress in status colours. Lanes with no recorded dates sit in a
   labelled "before history" band at the left, in plan order, without
   pretending dates ("no history before <date>" names the gap honestly).
   Planned lanes are dashed future bars from today sized by plan size
   ('estimate: plan size'), never history. Lanes order topological by
   depends_on then start, so most dependencies point downward; the
   connector for a lane's depends_on is drawn only on hover, focus or
   pin (upstream amber, downstream blue) — never always-on lines — plus
   a small 'waits on N' badge on blocked planned lanes. Hover any lane
   for exact dates, progress, runs, tokens and cost; click a lane to
   open that workstream's plan page.
   Reads: state.timeline (/api/project/timeline, fixtures/timeline.json
   under ?fixtures), state.seq, state.view.
   Exports (on window.AdPage.cards, also kept as plain globals):
   roadmapSpec, roadmapCard, roadState, roadDay, loadTimeline.
   Needs: el, s, axes, timeTicks, showTip, hideTip, fmtN, fmtUsd,
   fmtDate, openPlan, keyOf, CW, api, FIX, drawMain, state (plain
   globals). */
(function (global) {
  "use strict";
  /* Status is glyph plus word plus colour, never colour alone. */
  var ROAD_STATE = {
    done: {mark: "\u2713", word: "Done", color: "var(--good)"},
    archived: {mark: "\u2713", word: "Archived", color: "var(--good)"},
    active: {mark: "\u25CF", word: "Active", color: "var(--s1)"},
    paused: {mark: "\u23F8", word: "Paused", color: "var(--muted)"},
    planned: {mark: "\u25CB", word: "Planned", color: "var(--muted)"},
    dropped: {mark: "\u2715", word: "Dropped", color: "var(--muted)"}
  };
  var ROAD_GROUP = {
    done: {mark: "\u2713", word: "Done", color: "var(--good)"},
    active: {mark: "\u25CF", word: "Active", color: "var(--s1)"},
    planned: {mark: "\u25CB", word: "Planned", color: "var(--muted)"}
  };

  /* Pure: the glyph/word/colour triple for a lifecycle state. */
  function roadState(state) {
    return ROAD_STATE[state] || ROAD_STATE.active;
  }

  /* Pure: UTC calendar day for an epoch, from date parts (never a locale
     string, so screenshots and tests agree). */
  function roadDay(t) {
    if (t == null || !isFinite(+t)) return "\u2013";
    var d = new Date(+t * 1000);
    var m = d.getUTCMonth() + 1, day = d.getUTCDate();
    return d.getUTCFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (day < 10 ? "0" + day : day);
  }

  /* Pure: parse a YYYY-MM-DD project date to epoch seconds, else null. */
  function roadEpochDay(raw) {
    if (typeof raw !== "string") return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    if (!m) return null;
    var t = Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000;
    return isFinite(t) ? t : null;
  }

  /* In-flight fetch guard plus the last refresh stamp, so the registry
     spec can refetch every 30 s without touching the shared refresh. */
  var roadLoading = false;
  var roadPinned = null;
  var roadSvg = null;
  var roadGeom = {};
  var roadDown = {};
  var roadData = null;

  function laneById(id) {
    var lanes = (roadData && roadData.lanes) || [];
    for (var i = 0; i < lanes.length; i++) {
      if (lanes[i] && lanes[i].id === id) return lanes[i];
    }
    return null;
  }

  /* Fetch the timeline payload (live endpoint, or the seeded fixture
     under ?fixtures). A failure is loud — never a silent zero — and a
     stale payload keeps rendering meanwhile. The slot section key does
     not cover this payload, so a settle that changes what renders drops
     the cached exec-roadmap section before redrawing; an unchanged
     payload redraws nothing, so hover and focus survive refreshes. */
  var roadRendered = null;
  function loadTimeline() {
    if (roadLoading) return;
    var nowS = Date.now() / 1000;
    if (state.timeline && nowS - (state.timelineAt || 0) < 30) return;
    roadLoading = true;
    var settle = function (ok, body) {
      roadLoading = false;
      var rendered = "failed";
      if (ok && body && typeof body === "object" && Array.isArray(body.lanes)) {
        state.timeline = body;
        state.timelineError = false;
        rendered = keyOf([body.lanes, body.history_floor, body.project]);
      } else {
        state.timelineError = true;
      }
      state.timelineAt = Date.now() / 1000;
      if ((state.view === "project" || state.view === "planned") && rendered !== roadRendered) {
        roadRendered = rendered;
        try { delete secCache["exec-roadmap"]; } catch (e) {}
        drawMain();
      }
    };
    var fail = function () { settle(false, null); };
    try {
      if (FIX) {
        fetch("../fixtures/timeline.json").then(function (r) {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        }).then(function (j) { settle(true, j); }).catch(fail);
      } else {
        api("/api/project/timeline", {}).then(function (j) { settle(true, j); }).catch(fail);
      }
    } catch (e) {
      fail();
    }
  }

  /* Remove the focus connector group, if one is drawn. */
  function clearLane() {
    roadPinned = null;
    if (!roadSvg || !roadSvg.querySelector) return;
    var old = roadSvg.querySelector(".road-focus");
    if (old) old.remove();
  }

  /* Draw this lane's depends_on connectors only: upstream amber to each
     dependency's bar end, downstream blue to each dependent's bar start.
     This is the one place connectors are created — hovering away, Escape
     or clicking the lane again clears them. */
  function focusLane(id) {
    clearLane();
    if (!roadSvg || !roadData) return;
    var lane = laneById(id);
    var me = roadGeom[id];
    if (!lane || !me) return;
    var g = s("g", {class: "road-focus"});
    var draw = function (x1, y1, x2, y2, dir) {
      var mx = (x1 + x2) / 2;
      var p = s("path", {d: "M" + x1 + "," + y1 + " C" + mx + "," + y1 + " " + mx + "," + y2 + " " + x2 + "," + y2,
        class: "road-link " + dir, fill: "none",
        stroke: dir === "up" ? "var(--warning)" : "var(--s1)", "stroke-width": 2});
      g.appendChild(p);
      var dot = s("circle", {cx: x1, cy: y1, r: 3, class: "road-link " + dir,
        fill: dir === "up" ? "var(--warning)" : "var(--s1)"});
      g.appendChild(dot);
    };
    ((lane.depends_on) || []).forEach(function (dep) {
      var o = roadGeom[dep];
      if (o) draw(o.x2, o.y, me.x1, me.y, "up");
    });
    ((roadDown[id]) || []).forEach(function (child) {
      var o = roadGeom[child];
      if (o) draw(me.x2, me.y, o.x1, o.y, "down");
    });
    roadSvg.appendChild(g);
  }

  /* Tooltip rows for one lane: exact dates, progress, runs, tokens, cost. */
  function laneTip(lane) {
    var st = roadState(lane.state);
    var rows = [{k: "state", v: st.mark + " " + st.word, c: st.color}];
    rows.push({k: "started", v: lane.started_at == null ? "not recorded" : fmtDate(lane.started_at) + " (" + roadDay(lane.started_at) + ")"});
    if (lane.done_at != null) rows.push({k: "done", v: fmtDate(lane.done_at) + " (" + roadDay(lane.done_at) + ")" + (lane.done_inferred ? " — inferred" : "")});
    else if (lane.last_active_at != null) rows.push({k: "last active", v: fmtDate(lane.last_active_at) + " (" + roadDay(lane.last_active_at) + ")"});
    if (lane.before_history) rows.push({k: "dates", v: "no recorded dates — before-history band"});
    if (lane.state === "planned" && lane.estimate_days != null) rows.push({k: "plan", v: "estimate: plan size (" + fmtN(lane.estimate_days) + " days)"});
    var pr = lane.progress || {done: 0, total: 0, pct: 0};
    rows.push({k: "progress", v: fmtN(pr.done) + "/" + fmtN(pr.total) + " (" + fmtN(pr.pct) + "%)"});
    rows.push({k: "runs", v: fmtN(lane.runs || 0) + " lifetime"});
    rows.push({k: "tokens", v: fmtN(lane.tokens || 0) + " lifetime"});
    rows.push({k: "cost", v: fmtUsd(lane.cost_usd || 0) + " lifetime"});
    if (lane.milestone) rows.push({k: "milestone", v: lane.milestone});
    rows.push({k: "depends on", v: ((lane.depends_on) || []).length ? lane.depends_on.join(", ") : "nothing"});
    if ((lane.blocked_by || []).length) rows.push({k: "waiting on", v: lane.blocked_by.join(", ")});
    return rows;
  }

  function laneTitle(lane) {
    var st = roadState(lane.state);
    return lane.id + " " + (lane.title || "") + " — " + st.word;
  }

  function shortName(lane, n) {
    var text = lane.id + (lane.title ? " · " + lane.title : "");
    if (text.length > n) text = text.slice(0, n - 1) + "…";
    return text;
  }

  /* The swimlane chart: axes via the shared helpers, x is time with a
     today line and project started/target markers, y is one lane per
     workstream grouped Done / Active / Planned. */
  function roadmapCard() {
    roadSvg = null;
    roadGeom = {};
    roadDown = {};
    var c = el("div", {class: "card road", id: "exec-roadmap", "data-nav": "Project roadmap"});
    c.appendChild(el("h2", {text: "Project roadmap"}));
    c.appendChild(el("p", {class: "sub", text: "One lane per workstream over time. Bars run from the first recorded run or history move to done or today; planned bars are estimates from plan size. Hover a lane for its dependencies."}));
    if (state.timelineError && !state.timeline) {
      var failed = el("div", {class: "empty road-failed", text: "Roadmap unavailable — request failed. The numbers below would be guesses, so nothing is drawn."});
      failed.setAttribute("data-failed-scope", "timeline");
      c.appendChild(failed);
      return c;
    }
    var payload = state.timeline;
    if (!payload) {
      var loading = el("div", {class: "card road-loading", "aria-busy": "true"});
      loading.appendChild(el("div", {class: "skel"}));
      loading.appendChild(el("div", {class: "skel"}));
      loading.appendChild(el("p", {class: "pj-note", text: "Loading the roadmap…"}));
      c.appendChild(loading);
      return c;
    }
    roadData = payload;
    var lanes = payload.lanes || [];
    var byId = {};
    lanes.forEach(function (lane) { if (lane && lane.id) byId[lane.id] = lane; });
    var order = (payload.order || []).filter(function (id) { return byId[id]; });
    Object.keys(byId).forEach(function (id) { if (order.indexOf(id) < 0) order.push(id); });
    if (!order.length) {
      c.appendChild(el("div", {class: "empty", text: "No workstreams yet. Add one with ws new and it gets a lane here."}));
      return c;
    }
    var now = payload.now || Date.now() / 1000;
    var today = now;
    var proj = payload.project || {};
    var startedMark = roadEpochDay(proj.started);
    var targetMark = roadEpochDay(proj.target);
    var t0 = null, t1 = today;
    order.forEach(function (id) {
      var lane = byId[id];
      if (lane.started_at != null) t0 = t0 == null ? lane.started_at : Math.min(t0, lane.started_at);
      var end = lane.done_at != null ? lane.done_at : (lane.state === "planned" && lane.estimate_days != null ? today + lane.estimate_days * 86400 : today);
      if (end != null) t1 = Math.max(t1, end);
    });
    /* The domain follows lane dates only: a project start a year before
       the first recorded run must not crush every bar into one pixel.
       Markers draw when they fall inside, and the note always names the
       project started/target strings in words. */
    if (t0 == null) t0 = today - 86400;
    if (t1 <= t0) t1 = t0 + 86400;
    t0 -= (t1 - t0) * 0.02;
    t1 += (t1 - t0) * 0.02;
    var band = order.some(function (id) { return byId[id].before_history; });
    var floor = payload.history_floor;
    var note = "Lanes with no recorded dates sit in the before-history band, in plan order — no dates invented.";
    if (band) note += floor != null ? " No history before " + roadDay(floor) + "." : " No recorded history yet.";
    if (proj.target) note += " Target " + proj.target + ".";
    else if (proj.started) note += " Started " + proj.started + "; no target date (gate-based cutover).";
    var legend = el("div", {class: "legend road-legend"});
    [["done", "Done"], ["active", "Active"], ["planned", "Planned"]].forEach(function (pair) {
      var gg = ROAD_GROUP[pair[0]], sp = el("span");
      var sw = el("i", {class: "sw"});
      sw.style.background = gg.color;
      sp.appendChild(sw);
      sp.appendChild(document.createTextNode(gg.mark + " " + pair[1]));
      legend.appendChild(sp);
    });
    var msLegend = el("span", {text: "\u25C6 milestone"});
    legend.appendChild(msLegend);
    var todayLegend = el("span");
    var todaySw = el("i", {class: "sw"});
    todaySw.style.background = "var(--accent)";
    todayLegend.appendChild(todaySw);
    todayLegend.appendChild(document.createTextNode("today"));
    legend.appendChild(todayLegend);
    c.appendChild(legend);
    var W = CW(), narrow = W <= 700;
    var L = narrow ? 104 : 168, Rm = 12, bandW = band ? (narrow ? 76 : 148) : 0;
    var plotL = L + bandW, pw = Math.max(80, W - plotL - Rm), T = 10, laneH = 34, headH = 20;
    var X = function (t) { return plotL + pw * (t - t0) / (t1 - t0); };
    var rows = [], yy = T, lastGroup = null;
    order.forEach(function (id) {
      var lane = byId[id];
      if (lane.group !== lastGroup) {
        lastGroup = lane.group;
        rows.push({header: lastGroup, y: yy});
        yy += headH;
      }
      rows.push({lane: lane, y: yy});
      yy += laneH;
    });
    var plotB = yy - 6, H = Math.ceil(plotB + 34);
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "aria-label": "Project roadmap: one lane per workstream over time"});
    svg.setAttribute("data-road-lanes", String(order.length));
    var gap = narrow ? 44 : 64;
    var ticks = timeTicks(t0, t1, X, 6, {minGap: gap});
    if (ticks.length > 1 && ticks[ticks.length - 1].x - ticks[ticks.length - 2].x < gap) ticks.pop();
    ticks.forEach(function (k) { svg.appendChild(s("line", {x1: k.x, x2: k.x, y1: T, y2: plotB, class: "gl"})); });
    if (band) {
      svg.appendChild(s("rect", {x: L, y: T, width: bandW, height: plotB - T, rx: 6, fill: "var(--surface-2)"}));
      svg.appendChild(s("line", {x1: L + bandW, x2: L + bandW, y1: T, y2: plotB, stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "4 4"}));
      var bandLab = s("text", {x: L + 10, y: T + 16, class: "road-band-lab"});
      bandLab.textContent = "before history";
      svg.appendChild(bandLab);
    }
    rows.forEach(function (row) {
      if (row.header) {
        var g = ROAD_GROUP[row.header] || ROAD_GROUP.active;
        var ht = s("text", {x: 0, y: row.y + 14, class: "road-group"});
        ht.textContent = g.mark + " " + g.word;
        svg.appendChild(ht);
        return;
      }
      var lane = row.lane, ly = row.y, cy = ly + 15, st = roadState(lane.state);
      var lab = s("text", {x: 0, y: ly + 12, class: "road-lane-id"});
      lab.textContent = shortName(lane, narrow ? 12 : 24);
      svg.appendChild(lab);
      var slab = s("text", {x: 0, y: ly + 26, class: "road-lane-state"});
      slab.textContent = st.mark + " " + st.word;
      svg.appendChild(slab);
      if (lane.state === "planned" && (lane.blocked_by || []).length) {
        var badge = s("text", {x: 0, y: ly + 26, class: "road-waits", dx: String((st.mark + " " + st.word).length * 6.4 + 8)});
        badge.textContent = "waits on " + lane.blocked_by.length;
        badge.style.cursor = "pointer";
        badge.addEventListener("mousemove", function (ev) { showTip(ev, laneTitle(lane), laneTip(lane)); });
        badge.addEventListener("mouseleave", hideTip);
        badge.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (roadPinned === lane.id) { roadPinned = null; clearLane(); }
          else { roadPinned = lane.id; focusLane(lane.id); }
        });
        svg.appendChild(badge);
      }
      var hit = s("rect", {x: 0, y: ly, width: W - Rm, height: laneH - 4, fill: "transparent", class: "road-lane-hit", "data-ws": lane.id, "data-state": lane.state, tabindex: "0", role: "button", "aria-label": laneTitle(lane) + ". Activate to open its plan page."});
      hit.style.cursor = "pointer";
      hit.addEventListener("mousemove", function (ev) { showTip(ev, laneTitle(lane), laneTip(lane)); focusLane(lane.id); });
      hit.addEventListener("mouseleave", function () { hideTip(); if (roadPinned !== lane.id) clearLane(); });
      hit.addEventListener("focus", function () { focusLane(lane.id); });
      hit.addEventListener("blur", function () { if (roadPinned !== lane.id) clearLane(); });
      hit.addEventListener("click", function () { openPlan(lane.id); });
      hit.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { openPlan(lane.id); }
        else if (ev.key === " ") {
          ev.preventDefault();
          if (roadPinned === lane.id) { roadPinned = null; clearLane(); }
          else { roadPinned = lane.id; focusLane(lane.id); }
        }
        else if (ev.key === "Escape") { clearLane(); }
      });
      svg.appendChild(hit);
      var pr = lane.progress || {done: 0, total: 0, pct: 0};
      var frac = pr.total > 0 ? Math.min(1, Math.max(0, pr.done / pr.total)) : 0;
      var msAt = null;
      if (lane.before_history) {
        var chipX = L + 8, chipW = Math.max(8, bandW - 16);
        svg.appendChild(s("rect", {x: chipX, y: cy - 7, width: chipW, height: 14, rx: 7, fill: "var(--surface-2)", class: "road-chip", "data-ws": lane.id}));
        if (frac > 0) svg.appendChild(s("rect", {x: chipX, y: cy - 7, width: Math.max(4, chipW * frac), height: 14, rx: 7, fill: st.color, class: "road-chip-fill", "data-ws": lane.id}));
        var chipT = s("text", {x: chipX + 6, y: cy + 4, class: "road-chip-lab"});
        chipT.textContent = lane.id + " " + st.mark + " " + fmtN(pr.pct) + "%";
        svg.appendChild(chipT);
        roadGeom[lane.id] = {x1: chipX, x2: chipX + chipW, y: cy};
      } else if (lane.state === "planned") {
        var estEnd = today + (lane.estimate_days || 7) * 86400;
        var ex1 = X(Math.max(today, t0)), ex2 = Math.max(ex1 + 2, X(estEnd));
        svg.appendChild(s("rect", {x: ex1, y: cy - 8, width: ex2 - ex1, height: 16, rx: 8, fill: "var(--accent-soft)", stroke: "var(--muted)", "stroke-width": 1.5, "stroke-dasharray": "5 4", class: "road-est", "data-ws": lane.id}));
        var estLab = s("text", {y: cy + 4, class: "road-est-lab"});
        estLab.textContent = "estimate: plan size";
        if (ex2 + 8 + 118 > W - Rm) { estLab.setAttribute("x", ex2 - 6); estLab.setAttribute("text-anchor", "end"); }
        else { estLab.setAttribute("x", ex2 + 8); estLab.setAttribute("text-anchor", "start"); }
        svg.appendChild(estLab);
        if (lane.started_at != null && lane.started_at < today) {
          var sx = X(Math.max(lane.started_at, t0));
          svg.appendChild(s("circle", {cx: sx, cy: cy, r: 3, fill: st.color, class: "road-seen", "data-ws": lane.id}));
        }
        msAt = ex2;
        roadGeom[lane.id] = {x1: ex1, x2: ex2, y: cy};
      } else {
        var end = lane.done_at != null ? lane.done_at : today;
        var bx1 = X(Math.max(lane.started_at != null ? lane.started_at : end, t0)), bx2 = Math.max(bx1 + 2, X(end));
        svg.appendChild(s("rect", {x: bx1, y: cy - 8, width: bx2 - bx1, height: 16, rx: 8, fill: "var(--surface-2)", class: "road-track", "data-ws": lane.id}));
        if (frac > 0) svg.appendChild(s("rect", {x: bx1, y: cy - 8, width: Math.max(3, (bx2 - bx1) * frac), height: 16, rx: 8, fill: st.color, class: "road-fill", "data-ws": lane.id}));
        msAt = bx2;
        roadGeom[lane.id] = {x1: bx1, x2: bx2, y: cy};
      }
      if (lane.milestone && msAt != null) {
        var ms = s("path", {d: "M" + msAt + "," + (cy - 11) + " l6,6 l-6,6 l-6,-6 Z", class: "road-ms", fill: "var(--accent)", stroke: "var(--ink)", "stroke-width": 1, "data-ms": lane.milestone, "data-ws": lane.id});
        ms.addEventListener("mousemove", function (ev) { showTip(ev, laneTitle(lane), [{k: "milestone", v: lane.milestone, c: "var(--accent)"}]); });
        ms.addEventListener("mouseleave", hideTip);
        svg.appendChild(ms);
      }
    });
    svg.appendChild(s("line", {x1: X(today), x2: X(today), y1: T, y2: plotB, class: "road-now"}));
    var nowLab = s("text", {x: Math.min(X(today) + 4, W - 40), y: T + 2, class: "road-now-lab"});
    nowLab.textContent = "today";
    svg.appendChild(nowLab);
    if (startedMark != null && startedMark >= t0 && startedMark <= t1) {
      svg.appendChild(s("line", {x1: X(startedMark), x2: X(startedMark), y1: T, y2: plotB, class: "road-mark"}));
      var smLab = s("text", {x: Math.min(X(startedMark) + 4, W - 120), y: plotB + 30, class: "road-mark-lab"});
      smLab.textContent = "started " + proj.started;
      svg.appendChild(smLab);
    }
    if (targetMark != null && targetMark >= t0 && targetMark <= t1) {
      svg.appendChild(s("line", {x1: X(targetMark), x2: X(targetMark), y1: T, y2: plotB, class: "road-mark target"}));
      var tmLab = s("text", {x: Math.max(X(targetMark) - 4, 120), y: plotB + 30, class: "road-mark-lab", "text-anchor": "end"});
      tmLab.textContent = "target " + proj.target;
      svg.appendChild(tmLab);
    }
    axes(svg, {L: plotL, T: T, pw: pw, ph: plotB - T, xTicks: ticks, xTitle: "time"});
    roadSvg = svg;
    roadDown = {};
    order.forEach(function (id) {
      ((byId[id] && byId[id].depends_on) || []).forEach(function (dep) {
        if (byId[dep]) (roadDown[dep] = roadDown[dep] || []).push(id);
      });
    });
    c.appendChild(svg);
    c.appendChild(el("p", {class: "pj-note road-note", text: note}));
    return c;
  }

  /* Registry spec: kick a refresh when stale, then render the card. The
     needs entry beside Recently done owns its own renderer; this spec
     mounts into the exec-roadmap slot on both views. */
  function roadmapSpec(passedState, snap) {
    loadTimeline();
    return ["roadmap", keyOf([state.timeline, state.timelineError, state.seq, state.view]), function () { return roadmapCard(); }, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.roadmap = roadmapSpec;
  global.roadmapSpec = roadmapSpec;
  global.roadmapCard = roadmapCard;
  global.roadState = roadState;
  global.roadDay = roadDay;
  global.loadTimeline = loadTimeline;
  global.focusLane = focusLane;
  global.clearLane = clearLane;
})(window);
