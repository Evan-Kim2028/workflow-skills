/* Page card: Activity over time, one tile for tokens, runs, completion
   and live agents. Replaces the separate sections (Tokens per hour, Run
   timeline, Live agents over time, Progress over time) with a single
   card that stacks the four strips vertically in that order on ONE
   shared x axis (time) with ONE time filter. The filter sits on top of
   the tile as a horizontal range bar on the time axis: a track covering
   the loaded window with two draggable handles and a draggable middle,
   quick presets (Last hour, 6 h, 24 h, All), a Reset button, the
   selected range label, and a tokens-per-hour sparkline inside the bar
   showing where to brush. A brush zooms all four strips to the same
   domain at once; x labels draw once at the bottom of the stack (plus
   the brush bar's own labels at the top) while each strip keeps its own
   y axis and title. The timeline strip keeps the timeline row model
   (one lane per model family, packed rows, outcome colours,
   click-to-drawer); the token strip keeps the grouping toggle and
   legend; the progress strip keeps the Done / Merged-waiting-on-prod
   step series on a 0-100% axis; the live strips keep one strip per
   family on its own scale.
   Reads: state.usage, state.runs, state.conc (plus the older-than-24 h
   top-up in state.actWide), state.actProg (the progress payload cache);
   Date.now for the rolling window.
   Exports (on window.AdPage, also kept as plain globals): activityCard
   plus the pure range helpers actClampRange, actPresetRange, actTimeToX,
   actXToTime, actDragHandle, actDragMove and actTickStep (node-tested),
   the pure strip-family helper actConcFams (node-tested), and the pure
   progress-strip helpers actProgY, actProgClip and actProgHas
   (node-tested).
   Needs: el, s, CW, axes, timeTicks, fmtN, fmtPct, fmtDur, fmtT, fmtDate,
   showTip, hideTip, openRun, drawMain, api, colorOf, fullName, tierKey,
   modelKey, state, since, groupFor, expanded, isAgent, runEnd, packRows,
   outcomeOf, OUTCOME, FIX, secCache, progressFor, shown, progStep,
   progPctFmt, progLegendTip, progStepRows, progMileRows, progTakeaway,
   progBest, progBestElapsed (plain globals). */
(function (global) {
  "use strict";

  /* Brushes never collapse below five minutes; arrow keys move a handle
     five minutes, Shift+arrows one hour; the All preset loads at most
     seven days of older data through the since/until endpoints. */
  var ACT_MIN_SPAN = 5 * 60;
  var ACT_KEY_STEP = 5 * 60;
  var ACT_KEY_BIG = 3600;
  var ACT_WIDE_HOURS = 7 * 24;
  var ACT_PRESETS = [["hour", "Last hour", 3600], ["6h", "6 h", 6 * 3600], ["24h", "24 h", 24 * 3600], ["all", "All", 0]];

  /* Clamp an absolute selection into [lo, hi]: ordered, inside, at least
     a minimum span wide (the span shrinks to the window when the window
     itself is narrower). Pure: no DOM, no storage. */
  function actClampRange(from, to, lo, hi) {
    from = +from; to = +to; lo = +lo; hi = +hi;
    if (!isFinite(from)) from = lo;
    if (!isFinite(to)) to = hi;
    if (!isFinite(lo)) lo = Math.min(from, to);
    if (!isFinite(hi)) hi = Math.max(from, to);
    if (hi < lo) { var t = lo; lo = hi; hi = t; }
    var span = Math.min(ACT_MIN_SPAN, Math.max(1, hi - lo));
    if (to < from) { var t2 = from; from = to; to = t2; }
    if (to - from < span) { var mid = (from + to) / 2; from = mid - span / 2; to = mid + span / 2; }
    if (from < lo) { to += lo - from; from = lo; }
    if (to > hi) { from -= to - hi; to = hi; }
    if (from < lo) from = lo;
    if (to > hi) to = hi;
    if (to - from < span) to = Math.min(hi, from + span);
    return {from: Math.round(from), to: Math.round(to)};
  }

  /* A preset name to an absolute range at now. Hour presets follow now on
     ticks (live); All covers the loaded window as an absolute range. */
  function actPresetRange(preset, now, loadT0) {
    now = Math.round(+now); loadT0 = Math.round(+loadT0);
    if (!isFinite(now)) now = Math.round(Date.now() / 1000);
    if (!isFinite(loadT0)) loadT0 = now - 24 * 3600;
    for (var i = 0; i < ACT_PRESETS.length; i++) {
      if (ACT_PRESETS[i][0] === preset && ACT_PRESETS[i][2] > 0)
        return {from: now - ACT_PRESETS[i][2], to: now, live: true, preset: preset};
    }
    return {from: loadT0, to: now, live: false, preset: "all"};
  }

  /* One-decimal pixel rounding for path data (compact-formatter rule:
     no toFixed outside the shared helpers). */
  function actR1(x) { return Math.round(x * 10) / 10; }

  /* Pure hover-text builders: every number the user sees on hover is
     compact (fmtN), with the exact value left to data-v/data-t attrs. */
  function actLegendTip(name, total) { return name + " " + fmtN(total) + " tokens"; }
  function actTimelineAria(counts, n) {
    function bits() {
      var want = [["ok", "finished"], ["part", "partial"], ["blocked", "blocked"], ["died", "died"], ["cancelled", "cancelled"], ["stalled", "stalled"], ["run", "running"], ["unk", "no verdict"]];
      return want.filter(function (p) { return counts[p[0]]; }).map(function (p) { return fmtN(counts[p[0]]) + " " + p[1]; }).join(", ");
    }
    return "Agent runs in the selected range, one lane per model family: " + fmtN(n) + " runs" + (bits() ? ", " + bits() : "");
  }
  function actConcAria(name, peak) { return name + " live runs in the selected range, peak " + fmtN(peak); }
  /* Pixel<->time on one shared axis: time t in [from, to] sits at pixel
     x in [L, L+pw]. Clamped, so a bar edge past the window lands on it. */
  function actTimeToX(t, from, to, L, pw) {
    if (!(to > from) || !(pw > 0)) return L;
    var f = (+t - from) / (to - from);
    return L + pw * Math.max(0, Math.min(1, f));
  }
  function actXToTime(x, from, to, L, pw) {
    if (!(to > from) || !(pw > 0)) return from;
    var f = (x - L) / pw;
    return from + Math.max(0, Math.min(1, f)) * (to - from);
  }

  /* Brush drag math on absolute epoch seconds. Moving one edge never
     crosses the other (minimum span kept); moving the middle shifts both
     and stops at the window edges. Both return clamped ranges. */
  function actDragHandle(range, edge, t, lo, hi) {
    var r = {from: range.from, to: range.to};
    if (edge === "from") r.from = Math.min(+t, r.to - ACT_MIN_SPAN);
    else r.to = Math.max(+t, r.from + ACT_MIN_SPAN);
    return actClampRange(r.from, r.to, lo, hi);
  }
  function actDragMove(range, dt, lo, hi) {
    var span = Math.max(ACT_MIN_SPAN, range.to - range.from), from = range.from + (+dt || 0);
    if (from < lo) from = lo;
    if (from + span > hi) from = hi - span;
    return actClampRange(from, from + span, lo, hi);
  }

  /* Tick selection for a shared domain: the smallest grid step (one
     minute to one week, the same steps timeTicks uses) that fits at most
     maxLabels labels across the span, so a 2 h zoom reads minutes and a
     day reads hours, not one bare end tick. */
  function actTickStep(spanS, maxLabels) {
    var steps = [60, 120, 300, 900, 1800, 3600, 3 * 3600, 4 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, 2 * 24 * 3600, 7 * 24 * 3600];
    maxLabels = Math.max(1, +maxLabels || 6);
    spanS = Math.max(1, +spanS || 1);
    for (var i = 0; i < steps.length; i++) {
      if (spanS / steps[i] <= maxLabels) return steps[i];
    }
    return 7 * 24 * 3600;
  }

  /* One shared tick list for a zoomed range: every strip draws its
     gridlines on these positions while only the bottom strip labels
     them, so strips never disagree after a range change. The label
     budget follows the plot width, so narrow cards thin to fewer,
     never overlapping, ticks. */
  function actSharedTicks(range, g) {
    var X = function (t) { return actTimeToX(t, range.from, range.to, g.L, g.pw); };
    return {X: X, ticks: timeTicks(range.from, range.to, X, Math.max(2, Math.floor(g.pw / 150)))};
  }
  function actXGrid(svg, ticks, y1, y2) {
    ticks.forEach(function (k) { svg.appendChild(s("line", {x1: k.x, x2: k.x, y1: y1, y2: y2, class: "gl"})); });
  }

  /* The selection per scope: a live preset ({live, preset}) or an
     absolute brush ({from, to}). Committed ranges persist per scope in
     sessionStorage (guarded: private mode never breaks the page) and in
     the URL query (?from=<ms>&to=<ms>) so a view can be shared. A shared
     URL wins over storage on first draw; ticks never touch either. */
  var actRanges = {};
  function actKey(ws) { return "agent-dash-activity:" + (ws || "all"); }
  function actScope() { return (typeof state !== "undefined" && state.ws) || "all"; }
  function actReadStored(ws) {
    try {
      var raw = window.sessionStorage.getItem(actKey(ws));
      if (!raw) return null;
      var j = JSON.parse(raw);
      if (!j || typeof j !== "object") return null;
      if (j.live) return {preset: j.preset || "24h", live: true, from: 0, to: 0};
      if (isFinite(+j.from) && isFinite(+j.to) && +j.to > +j.from)
        return {from: +j.from, to: +j.to, live: false, preset: j.preset || null};
    } catch (e) {}
    return null;
  }
  function actWriteStored(ws, r) {
    try {
      window.sessionStorage.setItem(actKey(ws), JSON.stringify(r.live ? {live: true, preset: r.preset} : {from: r.from, to: r.to, live: false, preset: r.preset || null}));
    } catch (e) {}
  }
  function actReadUrl() {
    try {
      var q = new URLSearchParams(location.search);
      if (!q.has("from") || !q.has("to")) return null;
      var f = +q.get("from") / 1000, t = +q.get("to") / 1000;
      if (isFinite(f) && isFinite(t) && t > f) return {from: f, to: t, live: false, preset: null};
    } catch (e) {}
    return null;
  }
  function actWriteUrl(r) {
    try {
      var u = new URL(location.href);
      if (r.live) { u.searchParams.delete("from"); u.searchParams.delete("to"); }
      else { u.searchParams.set("from", String(Math.round(r.from * 1000))); u.searchParams.set("to", String(Math.round(r.to * 1000))); }
      var next = u.pathname + u.search + u.hash;
      if (next !== location.pathname + location.search + location.hash) history.replaceState(history.state, "", next);
    } catch (e) {}
  }
  /* Section key fragment for the committed range: stable across ticks
     for an absolute brush (no volatile clocks), live presets key by
     name with the minute bucket behind the now-rule. */
  function actRangeKey(ws) {
    var r = actRanges[ws || actScope()];
    if (!r) return "live:24h";
    if (r.live) return "live:" + (r.preset || "24h");
    return "abs:" + Math.round(r.from) + ":" + Math.round(r.to);
  }
  function actRangeLabel(r) { return fmtDate(r.from) + " to " + fmtDate(r.to); }

  /* Loaded data for the strips: the 24 h snapshot rows plus the
     older-than-24 h top-up (state.actWide) merged in front, so a brush
     into older data draws from memory. The track covers [t0, t1]. */
  function actQuant(t) { return Math.floor(t / 30) * 30; }
  function actLoaded() {
    var now = Date.now() / 1000, t1 = now, t0 = since();
    var usage = state.usage || [], runs = state.runs || [], conc = state.conc || [];
    var wide = state.actWide;
    if (wide && wide.key === actWideKey() && wide.since < t0) {
      var cut = Math.floor(t0 / 3600) * 3600;
      usage = (wide.usage || []).filter(function (b) { return b.t < cut; }).concat(usage);
      runs = (wide.runs || []).filter(function (r) { return (r.started_at || 0) < t0; }).concat(runs);
      conc = (wide.conc || []).filter(function (p) { return p.t < t0; }).concat(conc);
      t0 = Math.min(t0, wide.since);
    }
    return {t0: t0, t1: t1, usage: usage, runs: runs, conc: conc};
  }
  function actWideKey() { return actScope() + "|" + groupFor(); }
  /* Wider fetch for older data: quantized to the 30 s grid the server
     keys (and tags) on, conditional-GET friendly through api(), merged
     into state.actWide on arrival. Runs on preset commits and on first
     draw of a stored range only, never per pixel of drag. */
  var actWideInflight = {};
  function actEnsureWide(from) {
    var nowS = Date.now() / 1000;
    var want = Math.max(actQuant(from), actQuant(nowS - ACT_WIDE_HOURS * 3600));
    var key = actWideKey();
    var have = state.actWide;
    if (have && have.key === key && have.since <= want + 1) return;
    if (actWideInflight[key]) return;
    actWideInflight[key] = true;
    var me = actScope();
    var wsArg = me === "all" ? {} : {ws: me};
    var group = groupFor();
    Promise.all([
      api("/api/usage", Object.assign({group: group, since: want}, wsArg)).then(function (j) { return (j && j.buckets) || []; }).catch(function () { return []; }),
      api("/api/runs", Object.assign({since: want, limit: 5000}, wsArg)).then(function (j) { return (j && j.runs) || []; }).catch(function () { return []; }),
      api("/api/concurrency", Object.assign({since: want, step: 120}, wsArg)).then(function (j) { return (j && j.points) || []; }).catch(function () { return []; })
    ]).then(function (parts) {
      delete actWideInflight[key];
      if (actScope() !== me || state.view !== "exec") return;
      if (!parts[0].length && !parts[1].length && !parts[2].length) return;
      state.actWide = {key: key, since: want, usage: parts[0], runs: parts[1], conc: parts[2]};
      drawMain();
    });
  }

  /* The effective draw range: live presets follow now every draw (the
     live chip says so); an absolute brush is clamped to the loaded
     window for drawing while the committed range stays put, and a brush
     reaching past loaded data tops the window up in the background.
     Ticks and polls never rewrite the committed range. */
  function actEffective(ws, loaded, now) {
    var r = actRanges[ws];
    if (!r) {
      r = actReadUrl() || actReadStored(ws);
      if (r && !r.live) {
        actRanges[ws] = {from: r.from, to: r.to, live: false, preset: r.preset || null};
        if (r.from < loaded.t0 - 1) actEnsureWide(r.from);
      } else if (r && r.live) {
        actRanges[ws] = {preset: r.preset || "24h", live: true, from: 0, to: 0};
      } else {
        actRanges[ws] = {preset: "24h", live: true, from: 0, to: 0};
      }
      r = actRanges[ws];
    }
    if (r.live) return actPresetRange(r.preset || "24h", now, loaded.t0);
    return actClampRange(r.from, Math.min(r.to, now), loaded.t0, now);
  }
  function actCommit() {
    var ws = actScope(), r = actRanges[ws];
    if (!r) return;
    actWriteStored(ws, r);
    actWriteUrl(r);
    if (!r.live) {
      var loaded = actLoaded();
      if (r.from < loaded.t0 - 1) actEnsureWide(r.from);
    }
    drawMain();
  }
  function actSetPreset(preset) {
    var ws = actScope(), now = Date.now() / 1000, loaded = actLoaded();
    if (preset === "all") {
      actEnsureWide(now - ACT_WIDE_HOURS * 3600);
      loaded = actLoaded();
      actRanges[ws] = {from: loaded.t0, to: now, live: false, preset: "all"};
    } else {
      actRanges[ws] = actPresetRange(preset, now, loaded.t0);
    }
    actCommit();
  }
  function actReset() {
    var ws = actScope();
    try { window.sessionStorage.removeItem(actKey(ws)); } catch (e) {}
    actRanges[ws] = {preset: "24h", live: true, from: 0, to: 0};
    actWriteUrl(actRanges[ws]);
    drawMain();
  }

  /* One geometry for the whole stack: every strip and the brush share
     the same left margin and plot width, so one x maps them all. */
  function actGeo() {
    var W = CW(), L = W <= 600 ? 104 : 150, R = 14;
    return {W: W, L: L, R: R, pw: Math.max(200, W - L - R)};
  }

  /* Hidden runs and tokens in the selected range: unknown-model runs
     (the per-model timeline rows the toggle folds away) and their
     usage-bucket share under the current grouping, for the muted
     one-line note beside the Show button. Pure reads, no DOM. */
  function actHidden(range, loaded) {
    var runs = 0, tokens = 0;
    var now = Date.now() / 1000;
    (loaded.runs || []).forEach(function (x) {
      if (!x.started_at) return;
      if (runEnd(x, now) < range.from || x.started_at > range.to) return;
      if (AdUnatt.isUnattributed(tierKey(x))) runs++;
    });
    (loaded.usage || []).forEach(function (b) {
      if (!(b.t + 3600 > range.from && b.t < range.to)) return;
      var k = state.groupBy === "model" ? String(b.key || "other") : null;
      if (k && AdUnatt.isUnattributed(k))
        tokens += (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0);
    });
    return {runs: runs, tokens: tokens};
  }
  /* The Show/Hide unattributed control for the Activity header: the
     muted one-line note while hidden entries exist, the real button
     always (so the toggle is discoverable both ways). Null when there
     is nothing hidden and nothing to hide. */
  function actUnattControl(ws, range, loaded) {
    var showU = AdUnatt.shown(ws);
    var hid = actHidden(range, loaded);
    if (!showU && !hid.runs && !hid.tokens) return null;
    var row = el("div", {class: "act-unatt", style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:2px 0 8px"});
    if (!showU && (hid.runs || hid.tokens)) {
      var note = AdUnatt.noteText(hid.runs, hid.tokens, fmtN);
      row.appendChild(el("span", {class: "unatt-note", style: "color:var(--muted);font-size:12px", text: note, title: note, "data-unatt-runs": String(hid.runs), "data-unatt-tokens": String(hid.tokens)}));
    }
    row.appendChild(AdUnatt.toggleButton(ws));
    return row;
  }
  function activityCard() {
    var ws = actScope();
    var loaded = actLoaded();
    var now = Date.now() / 1000;
    var r = actEffective(ws, loaded, now);
    var g = actGeo();
    var c = el("div", {class: "card", id: "exec-activity", "data-nav": "Activity over time"});
    c.appendChild(el("h2", {text: "Activity over time"}));
    c.appendChild(el("p", {class: "sub", text: "Tokens, runs, completion and live agents on one shared time axis. Brush the range bar to zoom all four at once; the Last hour preset follows now, a brush stays put."}));
    var unattCtl = actUnattControl(ws, r, loaded);
    if (unattCtl) c.appendChild(unattCtl);
    var bar = el("div", {class: "act-controls", role: "group", "aria-label": "Time range"});
    ACT_PRESETS.forEach(function (p) {
      var active = r.live ? r.preset === p[0] : p[0] === "all" && r.preset === "all";
      var b = el("button", {type: "button", "aria-pressed": String(!!active), title: p[0] === "all" ? "Everything loaded, up to seven days" : "The last " + p[1].toLowerCase() + ", following now"}, [p[1]]);
      b.addEventListener("click", (function (name) { return function () { actSetPreset(name); }; })(p[0]));
      bar.appendChild(b);
    });
    var reset = el("button", {type: "button", class: "act-reset", text: "Reset", title: "Back to the last 24 hours, following now"});
    reset.addEventListener("click", actReset);
    bar.appendChild(reset);
    if (r.live) bar.appendChild(el("span", {class: "act-live", text: "live", title: "This range follows now on every tick"}));
    var rangeLab = el("span", {class: "act-range", text: actRangeLabel(r), title: "Selected range"});
    bar.appendChild(rangeLab);
    c.appendChild(bar);
    var brushWrap = el("div", {class: "act-brush"});
    var strips = el("div", {class: "act-strips"});
    /* Live redraw while dragging: the brush selection and range label
       repaint on every move (cheap); the four strips repaint from
       loaded data at most every 400 ms and at most once per animation
       frame, so a fast drag never queues seconds of strip rebuilds
       behind the release and never flickers mid-frame. The commit on
       release persists, syncs the URL and rebuilds the section, so the
       strips always settle on the committed range with a fresh clock. */
    var paintT = 0, paintRaf = 0, paintPending = null;
    var paintFrame = typeof requestAnimationFrame === "function" ? requestAnimationFrame : function (f) { return setTimeout(f, 16); };
    function paintStripsNow(range) {
      paintT = Date.now();
      paintPending = null;
      actPaintStrips(strips, range, loaded, Date.now() / 1000, g);
    }
    function paint(range) {
      rangeLab.textContent = actRangeLabel(range);
      actPaintBrush(brushSvg, range, loaded, g);
      if (Date.now() - paintT < 400) {
        paintPending = range;
        if (!paintRaf) paintRaf = paintFrame(function () {
          paintRaf = 0;
          if (!paintPending) return;
          var wait = 400 - (Date.now() - paintT), next = paintPending;
          if (wait > 0) setTimeout(function () { if (paintPending === next) paintStripsNow(next); }, wait);
          else paintStripsNow(next);
        });
        return;
      }
      if (paintRaf) paintPending = null;
      paintStripsNow(range);
    }
    var brushSvg = actBrush(ws, r, loaded, g, paint, function () { actCommit(); });
    brushWrap.appendChild(brushSvg);
    c.appendChild(brushWrap);
    actPaintStrips(strips, r, loaded, now, g);
    c.appendChild(strips);
    return c;
  }
  function actPaintStrips(strips, range, loaded, now, g) {
    strips.textContent = "";
    var tok = actTokenStrip(range, loaded, g);
    var tml = actTimelineStrip(range, loaded, now, g, false);
    var prog = actProgStrip(range, loaded, g, false);
    var conc = actConcStrips(range, loaded, g, false);
    if (!tok.hasData && !tml.hasData && !prog.hasData && !conc.length) {
      strips.appendChild(el("div", {class: "empty", text: "No activity in this window."}));
      return;
    }
    /* The x axis labels draw once at the bottom of the stack: only the
       last strip that actually drew gets ticks. Order is tokens, run
       timeline, progress over time, then live agents. */
    if (conc.length) conc[conc.length - 1] = actConcStrip(range, loaded, g, conc[conc.length - 1].fam, true, null, null, true);
    else if (prog.hasData) prog = actProgStrip(range, loaded, g, true);
    else tml = actTimelineStrip(range, loaded, now, g, true);
    strips.appendChild(tok.node);
    strips.appendChild(tml.node);
    if (prog.hasData) strips.appendChild(prog.node);
    conc.forEach(function (p) { strips.appendChild(p.node); });
    if (!conc.length) {
      if (state.snapError) strips.appendChild(el("div", {class: "empty err", "data-failed-scope": state.ws, text: "request failed \u2014 " + state.snapError}));
      else strips.appendChild(el("div", {class: "empty", text: "No live agents in this window."}));
    }
    actWireCross(strips, range, g);
  }
  /* One crosshair line per strip svg, all on the same shared x: hovering
     any strip shows the pointer's time across the whole stack. */
  function actWireCross(strips, range, g) {
    var lines = strips.querySelectorAll("line.act-cross");
    function move(t) {
      var x = actTimeToX(t, range.from, range.to, g.L, g.pw);
      for (var i = 0; i < lines.length; i++) {
        lines[i].setAttribute("x1", x);
        lines[i].setAttribute("x2", x);
        lines[i].style.display = "block";
      }
    }
    function hide() {
      for (var i = 0; i < lines.length; i++) lines[i].style.display = "none";
    }
    strips.actCrossMove = move;
    strips.actCrossHide = hide;
  }
  function actCrossOf(svg, y1, y2) {
    var ln = s("line", {x1: 0, x2: 0, y1: y1, y2: y2, class: "act-cross"});
    ln.style.display = "none";
    svg.appendChild(ln);
    return ln;
  }

  /* The time filter bar: a track over the loaded window with the
     tokens-per-hour shape inside, a selection with two handles and a
     draggable middle. Handles are focusable role=slider knobs (arrows
     move, Shift+arrows leap, Home/End jump, Escape resets); pointer
     drags move edges, the middle, or draw a new brush; double-click
     resets. While dragging only onDrag repaints from loaded data;
     onRelease commits once, so no request fires per pixel. */
  function actBrush(ws, range0, loaded, g, onDrag, onRelease) {
    var W = g.W, L = g.L, pw = g.pw;
    var H = 66, trackY = 28, trackH = 18;
    var XB = function (t) { return actTimeToX(t, loaded.t0, loaded.t1, L, pw); };
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", class: "act-svg", role: "group", "aria-label": "Time filter: brush a range to zoom the four strips below"});
    var ref = svg._act = {XB: XB, range: {from: range0.from, to: range0.to}};
    svg.appendChild(s("rect", {x: L, y: trackY, width: pw, height: trackH, rx: 5, fill: "var(--surface-2)", class: "act-track"}));
    /* Sparkline: the activity shape (tokens per hour) behind the brush. */
    var hours = {};
    loaded.usage.forEach(function (b) {
      var tot = (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0);
      hours[b.t] = (hours[b.t] || 0) + tot;
    });
    var hts = Object.keys(hours).map(Number).sort(function (a, b) { return a - b; });
    var hmax = 1;
    hts.forEach(function (t) { hmax = Math.max(hmax, hours[t]); });
    if (hts.length) {
      var d = "";
      hts.forEach(function (t) {
        var cx = XB(t + 1800), h = (trackH - 4) * hours[t] / hmax;
        d += (d ? " L" : "M") + actR1(cx) + "," + actR1(trackY + trackH - 2 - h);
      });
      var base = trackY + trackH - 2;
      svg.appendChild(s("path", {d: d + " L" + actR1(XB(hts[hts.length - 1] + 1800)) + "," + base + " L" + actR1(XB(hts[0] + 1800)) + "," + base + " Z", fill: "var(--s1)", "fill-opacity": ".35", stroke: "var(--s1)", "stroke-width": 1}));
    }
    var dimA = s("rect", {y: trackY, height: trackH, fill: "var(--surface-2)", "fill-opacity": ".72", class: "act-dim"});
    var dimB = s("rect", {y: trackY, height: trackH, fill: "var(--surface-2)", "fill-opacity": ".72", class: "act-dim"});
    var sel = s("rect", {y: trackY - 3, height: trackH + 6, rx: 7, fill: "rgba(76,150,255,.10)", stroke: "var(--s1)", "stroke-width": 1.5, class: "act-sel"});
    svg.appendChild(dimA); svg.appendChild(dimB); svg.appendChild(sel);
    var mid = s("rect", {y: trackY - 3, height: trackH + 6, fill: "transparent", class: "act-mid"});
    mid.style.cursor = "grab";
    svg.appendChild(mid);
    /* Brush labels ride the top; on narrow screens ticks squeeze
       together, so keep at least ~72 px between neighbours (rightmost
       first, so the "now" edge label always survives), room enough for
       a date-stamped label on multi-day windows. */
    var blabs = timeTicks(loaded.t0, loaded.t1, XB, Math.max(2, Math.floor(pw / 110)));
    var kept = [];
    for (var bi = blabs.length - 1; bi >= 0; bi--) {
      if (!blabs[bi].label) continue;
      if (kept.length && kept[kept.length - 1].x - blabs[bi].x < 72) continue;
      kept.push(blabs[bi]);
    }
    kept.reverse().forEach(function (tk) {
      var lab = s("text", {x: tk.x, y: 12, "text-anchor": tk.anchor || "middle", class: "act-blab"});
      lab.textContent = tk.label;
      svg.appendChild(lab);
    });
    ref.dimA = dimA; ref.dimB = dimB; ref.sel = sel; ref.mid = mid;
    function mkHandle(edge) {
      var vis = s("rect", {y: trackY - 5, width: 8, height: trackH + 10, rx: 4, fill: "var(--s1)", class: "act-handle"});
      var edgeT = edge === "from" ? range0.from : range0.to;
      var hit = s("rect", {y: 4, height: H - 8, width: 26, fill: "transparent", class: edge === "from" ? "act-hit act-handle-from" : "act-hit act-handle-to", tabindex: "0", role: "slider", "aria-label": edge === "from" ? "Selection start" : "Selection end", "aria-valuemin": String(Math.round(loaded.t0)), "aria-valuemax": String(Math.round(loaded.t1)), "aria-valuenow": String(Math.round(edgeT)), "aria-valuetext": fmtDate(edgeT)}); // page-rule-allow: slider bounds stay epoch seconds; the announced value is the fmtDate valuetext
      hit.style.cursor = "ew-resize";
      hit.addEventListener("pointerdown", function (ev) { startDrag(ev, edge); });
      hit.addEventListener("keydown", function (ev) { keyEdge(ev, edge); });
      hit.addEventListener("focus", function () { vis.setAttribute("class", "act-handle focused"); });
      hit.addEventListener("blur", function () { vis.setAttribute("class", "act-handle"); });
      svg.appendChild(vis); svg.appendChild(hit);
      return {vis: vis, hit: hit};
    }
    ref.hFrom = mkHandle("from"); ref.hTo = mkHandle("to");
    actPaintBrush(svg, range0, loaded, g);
    /* Drag state lives on the gesture, not in storage: nothing persists
       or refetches until release. */
    var drag = null;
    function evT(ev) {
      var bb = svg.getBoundingClientRect();
      var x = (ev.clientX - bb.left) * W / Math.max(1, bb.width);
      return actXToTime(x, loaded.t0, loaded.t1, L, pw);
    }
    /* Pointer capture keeps the moves flowing to the brush while the
       button is held, even when the cursor leaves the svg mid-drag; the
       svg also carries touch-action:none so touch drags never scroll. */
    function startDrag(ev, mode) {
      ev.preventDefault();
      try { svg.setPointerCapture(ev.pointerId); } catch (e) {}
      var t = evT(ev);
      drag = {mode: mode, anchor: t, orig: {from: ref.range.from, to: ref.range.to}, moved: false, pid: ev.pointerId};
    }
    mid.addEventListener("pointerdown", function (ev) { startDrag(ev, "move"); });
    sel.addEventListener("pointerdown", function (ev) { startDrag(ev, "move"); });
    var trackBg = svg.querySelector(".act-track");
    trackBg.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      try { svg.setPointerCapture(ev.pointerId); } catch (e) {}
      var t = evT(ev);
      drag = {mode: "new", anchor: t, orig: {from: ref.range.from, to: ref.range.to}, moved: false, pid: ev.pointerId};
    });
    trackBg.addEventListener("dblclick", actReset);
    svg.addEventListener("pointermove", function (ev) {
      if (!drag || (drag.pid != null && ev.pointerId !== drag.pid)) return;
      var t = evT(ev), cur;
      if (drag.mode === "from" || drag.mode === "to") cur = actDragHandle(drag.orig, drag.mode, t, loaded.t0, loaded.t1);
      else if (drag.mode === "move") cur = actDragMove(drag.orig, t - drag.anchor, loaded.t0, loaded.t1);
      else {
        if (Math.abs(t - drag.anchor) * 1 < 60) return;
        cur = actClampRange(Math.min(drag.anchor, t), Math.max(drag.anchor, t), loaded.t0, loaded.t1);
      }
      if (cur.from !== ref.range.from || cur.to !== ref.range.to) { drag.moved = true; ref.range = cur; onDrag(cur); }
    });
    function endDrag(ev) {
      if (!drag || (ev && drag.pid != null && ev.pointerId !== drag.pid)) return;
      var d = drag; drag = null;
      if (d.mode === "new" && !d.moved) { ref.range = {from: d.orig.from, to: d.orig.to}; onDrag(ref.range); return; }
      actRanges[ws] = {from: ref.range.from, to: ref.range.to, live: false, preset: null};
      onRelease();
    }
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", function () { drag = null; });
    function keyEdge(ev, edge) {
      var cur = ref.range, step = ev.shiftKey ? ACT_KEY_BIG : ACT_KEY_STEP, next = null;
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        ev.preventDefault();
        var d = (ev.key === "ArrowRight" ? step : -step);
        next = actDragHandle({from: cur.from, to: cur.to}, edge, (edge === "from" ? cur.from : cur.to) + d, loaded.t0, loaded.t1);
      } else if (ev.key === "Home") { ev.preventDefault(); next = actDragHandle({from: cur.from, to: cur.to}, edge, loaded.t0, loaded.t0, loaded.t1); }
      else if (ev.key === "End") { ev.preventDefault(); next = actDragHandle({from: cur.from, to: cur.to}, edge, loaded.t1, loaded.t0, loaded.t1); }
      else if (ev.key === "Escape") { ev.preventDefault(); actReset(); return; }
      else return;
      actRanges[ws] = {from: next.from, to: next.to, live: false, preset: null};
      actCommit();
    }
    return svg;
  }
  function actPaintBrush(svg, range, loaded, g) {
    var ref = svg._act;
    if (!ref) return;
    ref.range = {from: range.from, to: range.to};
    var L = g.L, pw = g.pw;
    var x1 = actTimeToX(range.from, loaded.t0, loaded.t1, L, pw);
    var x2 = actTimeToX(range.to, loaded.t0, loaded.t1, L, pw);
    ref.dimA.setAttribute("x", L); ref.dimA.setAttribute("width", Math.max(0, x1 - L));
    ref.dimB.setAttribute("x", x2); ref.dimB.setAttribute("width", Math.max(0, L + pw - x2));
    ref.sel.setAttribute("x", x1); ref.sel.setAttribute("width", Math.max(2, x2 - x1));
    ref.mid.setAttribute("x", x1); ref.mid.setAttribute("width", Math.max(2, x2 - x1));
    ref.hFrom.vis.setAttribute("x", x1 - 4);
    ref.hTo.vis.setAttribute("x", x2 - 4);
    ref.hFrom.hit.setAttribute("x", x1 - 13);
    ref.hTo.hit.setAttribute("x", x2 - 13);
    [["hFrom", range.from], ["hTo", range.to]].forEach(function (p) {
      ref[p[0]].hit.setAttribute("aria-valuenow", String(Math.round(p[1])));
      ref[p[0]].hit.setAttribute("aria-valuetext", fmtDate(p[1]));
    });
  }

  /* Tokens strip: the hourly stacks for the selected range only, on the
     shared x. Same grouping toggle and legend as the old Tokens per hour
     card; the legend Total is the selected range (the tile keeps the
     24 h total), so no tile/chart cross-check applies here. */
  function actTokenStrip(range, loaded, g) {
    var from = range.from, to = range.to;
    var wrap = el("div", {class: "act-strip", id: "exec-tokens"});
    var hdr = el("div", {class: "tok-head"}, [el("h3", {text: "Tokens per hour"})]);
    var seg = el("span", {class: "seg-ctl", style: "margin-left:auto", role: "group", "aria-label": "Stack grouping"});
    [["model", "By model"], ["workstream", "By workstream"]].forEach(function (gg) { seg.appendChild(el("button", {"aria-pressed": String(state.groupBy === gg[0]), onclick: function () { state.groupBy = gg[0]; refreshSnapshot(); }}, [gg[1]])); });
    if (state.ws === "all") hdr.appendChild(seg);
    wrap.appendChild(hdr);
    var B = (loaded.usage || []).filter(function (b) { return b.t + 3600 > from && b.t < to; });
    if (!B.length) {
      if (state.snapError) { wrap.appendChild(el("div", {class: "empty err", "data-failed-scope": state.ws, text: "request failed \u2014 " + state.snapError})); }
      else wrap.appendChild(el("div", {class: "empty", text: "No usage in this window."}));
      return {node: wrap, hasData: false};
    }
    var byModel = state.groupBy === "model";
    var showU = AdUnatt.shown(actScope());
    var hours = {}, keys = {};
    /* Model-grouped cells arrive as model@harness series keys; the stack
       splits harnesses of one model instead of folding them together. */
    B.forEach(function (b) { var k = state.groupBy === "model" ? String(b.key || "other") : (b.key || "unassigned"); var tot = (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0); hours[b.t] = hours[b.t] || {}; hours[b.t][k] = (hours[b.t][k] || 0) + tot; keys[k] = 1; });
    var allKs = byModel ? ADM.sortSeries(Object.keys(keys)) : Object.keys(keys).sort();
    /* Runs without a recorded model stay out of the model lists by
       default; the stacked total keeps them as one muted Unattributed
       segment, so the bars still sum to the legend Total. */
    var hideU = byModel && !showU && allKs.some(function (k) { return AdUnatt.isUnattributed(k); });
    var ks = hideU ? AdUnatt.filterKnown(allKs) : allKs;
    function hourHidden(t) {
      var n = 0;
      if (!hideU) return 0;
      allKs.forEach(function (k) { if (AdUnatt.isUnattributed(k)) n += hours[t][k] || 0; });
      return n;
    }
    var ts = Object.keys(hours).map(Number).sort(function (a, b) { return a - b; });
    var tots = {}, max = 0;
    ts.forEach(function (t) { var sum = 0; allKs.forEach(function (k) { sum += hours[t][k] || 0; }); tots[t] = sum; max = Math.max(max, sum); });
    var keyTot = {}, grand = 0, hiddenTotal = 0;
    ts.forEach(function (t) { allKs.forEach(function (k) { var v = hours[t][k] || 0; keyTot[k] = (keyTot[k] || 0) + v; grand += v; if (AdUnatt.isUnattributed(k)) hiddenTotal += v; }); });
    var lg = el("div", {class: "legend tok-legend"});
    var ltotal = el("span", {class: "ltotal", title: "Tokens in the selected range, input + output + cache read + cache write.", "data-legend-total": String(grand), "data-chart-sum": String(grand)}, [el("b", {text: "Total " + fmtN(grand)}), document.createTextNode(" \u00B7 " + fmtN(ts.length ? grand / ts.length : 0) + "/h avg \u00B7 peak " + fmtN(max) + "/h")]);
    lg.appendChild(ltotal);
    if (byModel) {
      /* One entry per model@harness series in family order: the word
         names the model and the harness, the shade names the harness
         within its family hue. */
      allKs.forEach(function (k) {
        if (!keyTot[k]) return;
        var sp = el("span", {class: "lentry", "data-key": k, "data-v": String(keyTot[k]), title: actLegendTip(ADM.seriesName(k), keyTot[k])});
        var sw = el("i", {class: "sw"}); sw.style.background = ADM.seriesColor(k, allKs); sp.appendChild(sw);
        sp.appendChild(el("span", {class: "lname", text: ADM.seriesName(k)}));
        sp.appendChild(el("span", {class: "lv", text: fmtN(keyTot[k])}));
        lg.appendChild(sp);
      });
      if (hideU && hiddenTotal > 0) {
        var usp = el("span", {class: "lentry unatt", "data-key": "unattributed", "data-v": String(hiddenTotal), title: actLegendTip("Unattributed", hiddenTotal)});
        var usw = el("i", {class: "sw"}); usw.style.background = "var(--muted)"; usp.appendChild(usw);
        usp.appendChild(el("span", {class: "lname", text: "Unattributed"}));
        usp.appendChild(el("span", {class: "lv", text: fmtN(hiddenTotal)}));
        lg.appendChild(usp);
      }
    } else {
      ks.forEach(function (k) {
        if (!keyTot[k]) return;
        var sp = el("span", {class: "lentry", "data-key": k, "data-v": String(keyTot[k]), title: actLegendTip(k, keyTot[k])});
        var sw = el("i", {class: "sw"}); sw.style.background = colorOf(k, state.groupBy); sp.appendChild(sw);
        sp.appendChild(el("span", {class: "lname", text: k}));
        sp.appendChild(el("span", {class: "lv", text: fmtN(keyTot[k])}));
        lg.appendChild(sp);
      });
    }
    wrap.appendChild(lg);
    var W = g.W, L = g.L, pw = g.pw, H = 270, T = 30, Bm = 42, ph = H - T - Bm;
    var st = actSharedTicks(range, g), X = st.X;
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "data-strip": "tokens", "data-x-from": String(Math.round(from)), "data-x-to": String(Math.round(to)), "aria-label": "Tokens per hour in the selected range: " + fmtN(grand) + " total"});
    /* Gridlines on the shared ticks; labels stay off mid-stack. */
    actXGrid(svg, st.ticks, T, T + ph);
    var nice = niceMax(max);
    var GAP = 2, RAD = 5;
    var peakT = -1;
    ts.forEach(function (t) { if (tots[t] > (tots[peakT] || 0)) peakT = t; });
    var top3 = ts.slice().sort(function (a, b) { return tots[b] - tots[a]; }).slice(0, 3);
    ts.forEach(function (t) {
      var bx1 = X(Math.max(t, from)) + 2, bx2 = X(Math.min(t + 3600, to)) - 2;
      if (bx2 - bx1 > 32) { var midx = (bx1 + bx2) / 2; bx1 = midx - 14; bx2 = midx + 14; }
      var bw = Math.max(2, bx2 - bx1), x = bx1, y0 = T + ph;
      /* The hidden share rides one muted segment, so the stack still
         sums to the legend Total while no unknown model is listed. */
      var dks = ks.slice();
      if (hideU && hourHidden(t) > 0) dks.push("unattributed");
      function segVal(k) { return k === "unattributed" ? hourHidden(t) : (hours[t][k] || 0); }
      function segFill(k) { return k === "unattributed" ? "var(--muted)" : (byModel ? ADM.seriesColor(k, allKs) : colorOf(k, state.groupBy)); }
      var order = [];
      dks.forEach(function (k, ki) { if (segVal(k)) order.push(ki); });
      var topKi = order.length ? order[order.length - 1] : -1;
      dks.forEach(function (k, ki) {
        var v = segVal(k); if (!v) return;
        var h = ph * v / nice, top = y0 - h;
        var gap = ki !== order[0] ? GAP : 0;
        var y = top + gap, hh = h - gap;
        if (hh < 0) { hh = 0; y = y0; }
        var ry = ki === topKi ? Math.min(RAD, bw / 2, Math.max(hh / 2, 1)) : 1.5;
        var attrs = {x: x, y: y, width: bw, height: Math.max(hh, 0.4), rx: ry, fill: segFill(k), "data-key": k, "data-v": String(v)};
        /* Shown unknown models keep the honest entry with a dashed
           outline, so they read as recorded-but-unattributed. */
        if (k !== "unattributed" && showU && AdUnatt.isUnattributed(k)) {
          attrs.stroke = "var(--muted)";
          attrs["stroke-width"] = 1;
          attrs["stroke-dasharray"] = "4 3";
        }
        svg.appendChild(s("rect", attrs));
        y0 = top;
      });
      var stackTop = T + ph - ph * tots[t] / nice, cx = x + bw / 2;
      if (tots[t] > 0 && top3.indexOf(t) >= 0) {
        var lab = s("text", {x: cx, y: Math.max(stackTop - 5, 10), "text-anchor": "middle", class: t === peakT ? "peaklab" : "vallab"});
        lab.textContent = (t === peakT ? "peak " : "") + fmtN(tots[t]);
        svg.appendChild(lab);
      }
      var hourTot = tots[t];
      var hit = s("rect", {x: x - 1, y: T, width: bw + 2, height: ph, fill: "transparent", "data-t": String(t), "data-v": String(hourTot)});
      hit.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(t), [{k: "Total", v: fmtN(hourTot), c: "var(--ink)"}].concat(dks.filter(function (k) { return segVal(k); }).map(function (k) { return {k: k === "unattributed" ? "Unattributed" : (byModel ? ADM.seriesName(k) : k), v: fmtN(segVal(k)), c: segFill(k)}; })));
        var root = svg.parentNode && svg.parentNode.parentNode;
        if (root && root.actCrossMove) root.actCrossMove(t + 1800);
      });
      hit.addEventListener("mouseleave", function () {
        hideTip();
        var root = svg.parentNode && svg.parentNode.parentNode;
        if (root && root.actCrossHide) root.actCrossHide();
      });
      svg.appendChild(hit);
    });
    /* No x labels mid-stack: the shared axis draws once at the bottom. */
    axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: nice, xTicks: [], yTitle: "tokens", xTitle: null});
    actCrossOf(svg, T, T + ph);
    wrap.appendChild(svg);
    return {node: wrap, hasData: true};
  }

  /* Timeline strip: every run in the selected range on the shared x,
     one lane per model family with the same packed rows, outcome
     colours and click-to-drawer as the old Run timeline card. Runs
     outside the range are not drawn; a bar crossing the edge is
     clipped with a visible cap at that edge. */
  function actLaneDims(nRows) {
    var rowH = 7, rowGap = 2;
    if (nRows > 10) { rowH = 4; rowGap = 1; }
    if (nRows > 40) { rowH = 3; rowGap = 1; }
    if (nRows > 120) { rowH = 2; rowGap = 1; }
    if (nRows > 250) { rowH = 2; rowGap = 0; }
    return {rowH: rowH, rowGap: rowGap, h: Math.max(nRows * (rowH + rowGap) - rowGap, 12)};
  }
  function actFamOfRun(r) { return ADM.famOf(tierKey(r)); }
  function actFamName(f) { return (ADM.FAM[f] || ADM.FAM.other).name; }
  function actFamHue(f) { return "var(" + (ADM.FAM[f] || ADM.FAM.other).hue + ")"; }
  function actDurText(r, now) {
    if (r.ended_at) return fmtDur(r.ended_at - r.started_at);
    if (r.status === "running") return fmtDur(now - r.started_at);
    return "end not recorded";
  }
  /* Tooltip outcome line: the class in words first, then the recorded
     verdict when it says more than the word, then the reason when one is
     known ("process exited without a verdict"). Close-out markers (QUOTA,
     IDLE) never print raw: the reason already names them. */
  function actOutcomeText(r, oc, o) {
    var reason = (typeof ADM !== "undefined" && ADM.outcomeReason) ? ADM.outcomeReason(r, oc) : null;
    var text = o[1] + " " + o[2];
    var v = r.verdict ? String(r.verdict) : null;
    if (v && v.toUpperCase() !== o[2].toUpperCase() && v !== "QUOTA" && v !== "IDLE") text += " (" + v + ")";
    if (reason) text += " \u2014 " + reason;
    return text;
  }
  function actDrawBar(svg, r, x1, x2, y, h, model, now, clipped) {
    var oc = outcomeOf(r), o = OUTCOME[oc];
    var bar = s("rect", {x: x1, y: y, width: Math.max(2, x2 - x1 - 1), height: h, rx: Math.min(2, h / 2), fill: r.clock_source === "mtime" ? "url(#act-hatch)" : o[0], stroke: r.clock_source === "mtime" ? o[0] : "none", class: "tl-bar"});
    if (r.status === "running") { bar.setAttribute("stroke", "var(--ink)"); bar.setAttribute("stroke-width", "1"); }
    /* Shown runs without a recorded model keep the honest entry with a
       dashed outline; the running outline always wins when live. */
    else if (AdUnatt.shown(actScope()) && AdUnatt.isUnattributed(tierKey(r))) {
      bar.setAttribute("stroke", "var(--muted)");
      bar.setAttribute("stroke-width", "1");
      bar.setAttribute("stroke-dasharray", "4 3");
    }
    bar.setAttribute("data-run-id", r.run_id || r.label || "");
    bar.setAttribute("data-outcome", oc);
    if (clipped) bar.setAttribute("data-clipped", clipped);
    bar.addEventListener("mousemove", function (ev) { showTip(ev, r.label || r.run_id, [{k: "model", v: model}, {k: "outcome", v: actOutcomeText(r, oc, o), c: o[0]}, {k: "duration", v: actDurText(r, now)}, {k: "workstream", v: r.workstream || "unattributed"}]); });
    bar.addEventListener("mouseleave", hideTip);
    bar.addEventListener("click", function () { openRun(r); });
    svg.appendChild(bar);
    /* Visible edge caps where the range clips the bar. */
    (clipped ? clipped.split(" ") : []).forEach(function (edge) {
      var cx = edge === "start" ? x1 : Math.max(x1, x2 - 3);
      var cap = s("rect", {x: cx, y: y, width: 3, height: h, fill: o[0], stroke: "var(--ink)", "stroke-width": 0.75, class: "tl-clip"});
      cap.setAttribute("data-run-id", r.run_id || r.label || "");
      svg.appendChild(cap);
    });
    return bar;
  }
  function actTimelineStrip(range, loaded, now, g, withX) {
    var from = range.from, to = range.to;
    var wrap = el("div", {class: "act-strip", id: "exec-runs"});
    wrap.appendChild(el("h3", {text: "Run timeline"}));
    var R = (loaded.runs || []).filter(function (r) { return r.started_at && runEnd(r, now) >= from && r.started_at <= to; });
    if (!R.length) { wrap.appendChild(el("div", {class: "empty", text: "No runs in this window."})); return {node: wrap, hasData: false}; }
    var lg = el("div", {class: "legend"});
    /* The legend lists exactly the classes present in this window, in
       severity order — never a fixed roster with zeroes. */
    var present = {};
    R.forEach(function (r) { present[outcomeOf(r)] = true; });
    ["run", "ok", "part", "blocked", "died", "cancelled", "stalled", "unk"].filter(function (k) { return present[k]; }).forEach(function (k) { var o = OUTCOME[k], sp = el("span"); var sw = el("i", {class: "sw"}); sw.style.background = o[0]; sp.appendChild(sw); sp.appendChild(document.createTextNode(o[1] + " " + o[2])); lg.appendChild(sp); });
    wrap.appendChild(lg);
    var W = g.W, L = g.L, Rm = g.R, pw = g.pw, T = 8, stripH = 30;
    var st = actSharedTicks(range, g), X = st.X;
    var byFam = {};
    R.forEach(function (r) { var f = actFamOfRun(r); (byFam[f] = byFam[f] || []).push(r); });
    var order = ADM.FAMS.filter(function (f) { return byFam[f]; });
    var top = order.slice().sort(function (a, b) { return byFam[b].length - byFam[a].length; })[0];
    if (state.ws !== "all" && top && order.every(function (f) { return expanded[f] === undefined; })) expanded[top] = true;
    var lanes = order.map(function (f) {
      var runs = byFam[f], lane = {fam: f, runs: runs, dense: false, subs: [], rows: null, dims: null, h: 0};
      if (expanded[f]) {
        var byM = {}, y = 0;
        /* One row per model plus harness: the same model under two
           harnesses draws two rows, "Beam 1.0 · pi" beside
           "Beam 1.0 · Grok Build". Rows without a recorded model stay
           undrawn by default, while the lane count above includes them. */
        runs.forEach(function (r) { var k = tierKey(r) + "@" + ADM.harnessOf(r); (byM[k] = byM[k] || []).push(r); });
        var mks = ADM.sortSeries(Object.keys(byM));
        if (!AdUnatt.shown(actScope())) mks = AdUnatt.filterKnown(mks);
        mks.forEach(function (k) {
          var mrows = packRows(byM[k], 20), dd = actLaneDims(mrows.length);
          lane.subs.push({name: ADM.seriesName(k), runs: byM[k], rows: mrows, dims: dd, y: y});
          y += 16 + dd.h + 6;
        });
        lane.h = y;
      } else {
        var rows = packRows(runs, 20);
        if (rows.length > 400) { lane.dense = true; lane.h = 26; }
        else { lane.rows = rows; lane.dims = actLaneDims(rows.length); lane.h = lane.dims.h; }
      }
      return lane;
    });
    var stripY = 12, yy = stripY + stripH + 14;
    lanes.forEach(function (lane) { lane.y = yy; yy += 22 + lane.h + 10; });
    var plotB = yy - 10, H = Math.ceil(plotB + 32);
    var ocCount = {run: 0, ok: 0, part: 0, blocked: 0, died: 0, cancelled: 0, stalled: 0, unk: 0};
    R.forEach(function (r) { var k = outcomeOf(r); ocCount[k] = (ocCount[k] || 0) + 1; });
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "data-strip": "timeline", "data-x-from": String(Math.round(from)), "data-x-to": String(Math.round(to)), "aria-label": actTimelineAria(ocCount, R.length)});
    var defs = s("defs", {}); var pat = s("pattern", {id: "act-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"}); pat.appendChild(s("rect", {width: 6, height: 6, fill: "var(--surface-2)"})); pat.appendChild(s("line", {x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--muted)", "stroke-width": 2})); defs.appendChild(pat); svg.appendChild(defs);
    /* Gridlines on the shared ticks in every lane; labels only bottom. */
    var gTicks = withX ? st.ticks : [];
    actXGrid(svg, st.ticks, T, plotB);
    var slab = s("text", {x: 0, y: stripY + stripH / 2, class: "tl-lane-label"}); slab.textContent = "agents at once"; svg.appendChild(slab);
    var scnt = s("text", {x: 0, y: stripY + stripH / 2 + 14, class: "tl-strip-sub"}); scnt.textContent = R.length + " runs"; svg.appendChild(scnt);
    var step = Math.max(60, Math.round((to - from) / 300)), pts = [], mx = 1, t;
    for (t = from; t <= to; t += step) { var n = 0; R.forEach(function (r) { if (isAgent(r) && r.started_at <= t && runEnd(r, now) >= t) n++; }); pts.push([t, n]); mx = Math.max(mx, n); }
    var Y = function (v) { return stripY + stripH - (stripH - 4) * v / mx; };
    svg.appendChild(s("rect", {x: L, y: stripY, width: pw, height: stripH, rx: 4, fill: "var(--surface-2)"}));
    var dd = "M" + X(pts[0][0]) + "," + (stripY + stripH); pts.forEach(function (p) { dd += " L" + X(p[0]) + "," + Y(p[1]); }); dd += " L" + X(to) + "," + (stripY + stripH) + " Z";
    svg.appendChild(s("path", {d: dd, fill: "var(--s1)", "fill-opacity": ".55", stroke: "var(--s1)", "stroke-width": 1.2}));
    [[0, stripY + stripH], [mx, stripY + 4]].forEach(function (p) { svg.appendChild(s("line", {x1: L - 4, x2: L, y1: p[1], y2: p[1], class: "bl"})); var tk = s("text", {x: L - 7, y: p[1] + 3.5, "text-anchor": "end", class: "ax"}); tk.textContent = fmtN(p[0]); svg.appendChild(tk); });
    var shit = s("rect", {x: L, y: stripY, width: pw, height: stripH, fill: "transparent", class: "tl-strip-hit"});
    shit.addEventListener("mousemove", function (ev) {
      var bb = svg.getBoundingClientRect();
      var tt = actXToTime((ev.clientX - bb.left) * W / Math.max(1, bb.width), from, to, L, pw);
      var act = R.filter(function (r) { return isAgent(r) && r.started_at <= tt && runEnd(r, now) >= tt; });
      var byF = {};
      act.forEach(function (r) { var f = actFamOfRun(r); byF[f] = (byF[f] || 0) + 1; });
      showTip(ev, "agents at " + fmtT(tt), [{k: "running", v: fmtN(act.length)}].concat(order.filter(function (f) { return byF[f]; }).map(function (f) { return {k: actFamName(f), v: fmtN(byF[f]), c: actFamHue(f)}; })));
      var root = svg.parentNode && svg.parentNode.parentNode;
      if (root && root.actCrossMove) root.actCrossMove(tt);
    });
    shit.addEventListener("mouseleave", function () {
      hideTip();
      var root = svg.parentNode && svg.parentNode.parentNode;
      if (root && root.actCrossHide) root.actCrossHide();
    });
    svg.appendChild(shit);
    lanes.forEach(function (lane, li) {
      var ly = lane.y, by = ly + 22;
      if (li) svg.appendChild(s("line", {x1: 0, x2: W - Rm, y1: ly - 6, y2: ly - 6, class: "gl"}));
      var lab = s("text", {x: 0, y: ly + 14, class: "tl-lane-label"});
      lab.textContent = (expanded[lane.fam] ? "\u25BE " : "\u25B8 ") + actFamName(lane.fam) + " \u00B7 " + lane.runs.length + " runs" + (lane.dense && !expanded[lane.fam] ? " (dense)" : "");
      svg.appendChild(lab);
      var laneLabel = actFamName(lane.fam) + ", " + lane.runs.length + " runs, " + (expanded[lane.fam] ? "expanded" : "collapsed");
      var hitL = s("rect", {x: 0, y: ly, width: L - 6, height: 20, fill: "transparent", class: "tl-lane-hit", "data-lane": lane.fam, tabindex: "0", role: "button", "aria-label": laneLabel + ". Activate to expand or collapse."});
      hitL.style.cursor = "pointer";
      hitL.addEventListener("click", (function (f) { return function () { expanded[f] = !expanded[f]; drawMain(); }; })(lane.fam));
      hitL.addEventListener("keydown", (function (f) { return function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); expanded[f] = !expanded[f]; drawMain(); } }; })(lane.fam));
      svg.appendChild(hitL);
      function barEndpoints(r) {
        var a = Math.max(r.started_at, from), b = Math.min(runEnd(r, now), to);
        var clip = (r.started_at < from ? "start" : "") + (r.started_at < from && runEnd(r, now) > to ? " " : "") + (runEnd(r, now) > to ? "end" : "");
        return {a: a, b: b, clip: clip || null};
      }
      if (expanded[lane.fam]) {
        lane.subs.forEach(function (sb) {
          var ml = s("text", {x: 10, y: by + sb.y + 11, class: "tl-model-label"});
          ml.textContent = sb.name + " \u00B7 " + sb.runs.length;
          svg.appendChild(ml);
          var ry = by + sb.y + 16;
          sb.rows.forEach(function (row) {
            row.runs.forEach(function (r) {
              var e = barEndpoints(r);
              actDrawBar(svg, r, X(e.a), X(e.b), ry, sb.dims.rowH, sb.name, now, e.clip);
            });
            ry += sb.dims.rowH + sb.dims.rowGap;
          });
        });
      } else if (lane.dense) {
        (function () {
          var h = 26, span = Math.max(1, to - from), nn = Math.max(60, Math.floor(pw / 3)), counts = [], i, cmx = 0;
          for (i = 0; i < nn; i++) counts.push(0);
          lane.runs.forEach(function (r) {
            var a = Math.max(0, Math.floor((Math.max(r.started_at, from) - from) / span * nn));
            var b = Math.max(a, Math.min(nn - 1, Math.floor((Math.min(runEnd(r, now), to) - from) / span * nn)));
            for (i = a; i <= b; i++) counts[i]++;
          });
          counts.forEach(function (n2) { cmx = Math.max(cmx, n2); });
          svg.appendChild(s("rect", {x: L, y: by, width: pw, height: h, rx: 4, fill: "var(--surface-2)"}));
          var hue = actFamHue(lane.fam), bw = pw / nn;
          counts.forEach(function (n2, k) {
            if (!n2) return;
            var cell = s("rect", {x: X(from + k * span / nn), y: by, width: Math.max(1.5, bw + 0.5), height: h, rx: 1, fill: hue, class: "tl-heat"});
            cell.setAttribute("data-dense-bucket", k);
            cell.setAttribute("data-dense-count", n2);
            cell.setAttribute("opacity", String(Math.round(100 * (0.12 + 0.88 * n2 / Math.max(1, cmx))) / 100));
            svg.appendChild(cell);
          });
          var label = actFamName(lane.fam) + " \u00B7 " + lane.runs.length + " runs (dense)";
          var hit = s("rect", {x: L, y: by, width: pw, height: h, fill: "transparent", class: "tl-dense-hit", "data-lane": lane.fam, tabindex: "0", role: "button", "aria-label": label + ". Activate to open one row per model."});
          hit.style.cursor = "pointer";
          hit.addEventListener("mousemove", function (ev) { showTip(ev, label, [{k: "peak slice", v: fmtN(cmx) + " overlapping"}, {k: "hint", v: "click the lane to open one row per model"}]); });
          hit.addEventListener("mouseleave", hideTip);
          hit.addEventListener("click", function () { expanded[lane.fam] = !expanded[lane.fam]; drawMain(); });
          hit.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); expanded[lane.fam] = !expanded[lane.fam]; drawMain(); } });
          svg.appendChild(hit);
        })();
      } else {
        var ry2 = by, ddz = lane.dims;
        lane.rows.forEach(function (row) {
          row.runs.forEach(function (r) {
            var e = barEndpoints(r);
            actDrawBar(svg, r, X(e.a), X(e.b), ry2, ddz.rowH, fullName(tierKey(r)) + " \u00B7 " + ADM.harnessName(ADM.harnessOf(r)), now, e.clip);
          });
          ry2 += ddz.rowH + ddz.rowGap;
        });
      }
    });
    axes(svg, {L: L, T: T, pw: pw, ph: plotB - T, xTicks: gTicks, xTitle: withX ? "time" : null, yTitle: null});
    if (now >= from && now <= to) svg.appendChild(s("line", {x1: X(now), x2: X(now), y1: T, y2: plotB, class: "tl-now"}));
    actCrossOf(svg, T, plotB);
    wrap.appendChild(svg);
    return {node: wrap, hasData: true};
  }

  /* Progress-over-time strip: the Done plus Merged-waiting-on-prod step
     series as % of tasks on the shared x, with its own 0-100% y axis.
     On a workstream tab it shows that workstream's series; on the
     overall view it shows the project total (all non-archived
     workstreams weighted by task count) as one series pair. The payload
     loads once per scope and refetches only when the plan stamp moves;
     the fetch URL carries no time params, so the conditional-GET layer
     answers 304 while nothing changed, and a brush never fetches (the
     loaded series clips locally on release). No plan data means no
     strip at all: the stack skips it with no empty frame. */
  var actProgInflight = null;
  function actProgStamp(scope) {
    function one(id) {
      try { return (typeof progressFor === "function") ? progressFor(id) : null; }
      catch (e) { return null; }
    }
    if (scope === "all") {
      var rows = state.wss || [];
      try { if (typeof shown === "function") rows = shown(state.wss || []); } catch (e) {}
      return rows.map(function (w) {
        var pr = one(w.ws);
        return w.ws + ":" + (pr ? pr.done + "/" + pr.total : "-");
      }).join(",");
    }
    var single = one(scope);
    return single ? single.done + "/" + single.total : "none";
  }
  /* Section-key fragment for the committed progress payload: the scope,
     the plan stamp behind it, and whether data is in hand (loading and
     loaded key apart, so the arrival redraws the stack once). */
  function actProgKey() {
    var scope = actScope();
    var c = state.actProg;
    var loaded = (c && c.scope === scope && c.data) ? "1" : "0";
    return scope + "|" + actProgStamp(scope) + "|" + loaded;
  }
  function actProgEnsure(scope, stamp) {
    var c = state.actProg;
    if (c && c.scope === scope && c.stamp === stamp) return;
    if (actProgInflight === scope + "|" + stamp) return;
    actProgInflight = scope + "|" + stamp;
    api("/api/ws/progress", {ws: scope}).then(function (j) {
      actProgInflight = null;
      if (actScope() !== scope || state.view !== "exec") return;
      state.actProg = {scope: scope, stamp: stamp, data: (j && j.wsprogress) || null};
      try { delete secCache.activity; } catch (e) {}
      drawMain();
    }, function () {
      actProgInflight = null;
      if (actScope() !== scope || state.view !== "exec") return;
      /* A failed fetch (including the 404 for no plan data) leaves no
         strip: the progress series is supplementary, and snapshot
         failures already surface loudly elsewhere. */
      state.actProg = {scope: scope, stamp: stamp, data: null};
      try { delete secCache.activity; } catch (e2) {}
      drawMain();
    });
  }
  /* Percent of tasks to a y pixel on a 0-100% strip of top T and height
     ph. Pure: no DOM, no state. */
  function actProgY(pct, T, ph) {
    var v = Math.max(0, Math.min(100, +pct || 0));
    return T + ph - v / 100 * ph;
  }
  /* Zoom clipping for step points: the vertices inside [from, to], with
     the last vertex before from carried in clamped to from (marked
     edge, so no dot draws on a synthetic point) so the step-after line
     starts at the right level. Pure: no DOM, no state. */
  function actProgClip(pts, from, to) {
    var out = [], prior = null;
    (pts || []).forEach(function (pt) {
      if (pt.t < from) prior = pt;
      else if (pt.t <= to) out.push(pt);
    });
    if (prior && (!out.length || out[0].t > from))
      out.unshift({t: from, done: prior.done, prog: prior.prog || 0, edge: true});
    return out;
  }
  /* Hidden-when-empty predicate: no plan data (or no total, or no
     recorded moves) means no strip, never an empty frame. Pure. */
  function actProgHas(p) {
    return !!(p && (p.total || 0) > 0 && (p.points || []).length);
  }
  function actProgStrip(range, loaded, g, withX) {
    var scope = actScope();
    var stamp = actProgStamp(scope);
    var c = state.actProg;
    if (!c || c.scope !== scope || c.stamp !== stamp) {
      actProgEnsure(scope, stamp);
      return {node: null, hasData: false};
    }
    var p = c.data;
    if (!actProgHas(p)) return {node: null, hasData: false};
    var from = range.from, to = range.to;
    /* Strip id exec-prog: the plan progress map card already owns the
       longer id (static/cards/plan-progress.js), and ids stay unique. */
    var wrap = el("div", {class: "act-strip", id: "exec-prog"});
    var head = el("div", {class: "prog-head"});
    head.appendChild(el("h3", {text: scope === "all" ? "Progress over time · project total" : "Progress over time"}));
    var total = Math.max(1, p.total || 0);
    var pts = p.points || [];
    var progNow = pts.length ? (pts[pts.length - 1].prog || 0) : 0;
    var lg = el("div", {class: "legend tok-legend prog-legend"});
    lg.appendChild(el("span", {class: "ltotal", "data-legend-total": String(p.done || 0), "data-chart-sum": String(p.done || 0), title: "Tasks done now in this scope"}, [el("b", {text: "Done " + fmtN(p.done || 0)})]));
    [["done", "✓ Done", "var(--s1)", p.done || 0], ["prog", "⇡ Merged, waiting on prod", "var(--s5)", progNow]].forEach(function (row) {
      lg.appendChild(el("span", {class: "lentry", "data-key": row[0], "data-v": String(row[3]), title: progLegendTip(row[1], row[3] / total)}, [
        (function () { var sw = el("i", {class: "sw"}); sw.style.background = row[2]; return sw; })(),
        el("span", {class: "lname", text: row[1]}),
        el("span", {class: "lv", text: fmtN(row[3])})
      ]));
    });
    head.appendChild(lg);
    wrap.appendChild(head);
    var W = g.W, L = g.L, pw = g.pw, H = withX ? 196 : 160, T = 8, Bm = withX ? 36 : 10, ph = H - T - Bm;
    var st = actSharedTicks(range, g), X = st.X;
    var Y = function (pct) { return actProgY(pct, T, ph); };
    var clipped = actProgClip(pts, from, to);
    var doneOf = function (pt) { return pt.done / total * 100; };
    var withProg = function (pt) { return (pt.done + (pt.prog || 0)) / total * 100; };
    var aria = (scope === "all" ? "Project total progress over time" : "Progress over time for " + scope) +
      ": " + progTakeaway(p.done, p.total, p.pct || 0, progBest(p), progBestElapsed(p), p.runs_total || 0);
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "data-strip": "progress", "data-x-from": String(Math.round(from)), "data-x-to": String(Math.round(to)), "aria-label": aria});
    actXGrid(svg, st.ticks, T, T + ph);
    /* Prog band between the done line and done+prog, then the done area
       and both step lines (progStep draws step-after through the
       shared X, same as the old standalone chart). */
    var focal = {L: L};
    if (clipped.length) {
      var top = progStep(focal, X, Y, clipped, withProg);
      var line = progStep(focal, X, Y, clipped, doneOf);
      var first = clipped[0], last = clipped[clipped.length - 1];
      svg.appendChild(s("path", {d: top + " L" + actR1(X(last.t)) + "," + actR1(Y(doneOf(last))) + " L" + actR1(X(first.t)) + "," + actR1(Y(doneOf(first))) + " Z", fill: "var(--s5)", opacity: ".12"}));
      svg.appendChild(s("path", {d: line + " L" + actR1(X(last.t)) + "," + actR1(T + ph) + " L" + actR1(X(first.t)) + "," + actR1(T + ph) + " Z", fill: "var(--s1)", opacity: ".16"}));
      svg.appendChild(s("path", {d: line, fill: "none", stroke: "var(--s1)", "stroke-width": "2.5"}));
      svg.appendChild(s("path", {d: top, fill: "none", stroke: "var(--s5)", "stroke-width": "1.6", "stroke-dasharray": "5 4"}));
    }
    clipped.forEach(function (pt) {
      if (pt.edge) return;
      var dot = s("circle", {cx: actR1(X(pt.t)), cy: actR1(Y(doneOf(pt))), r: "2.6", fill: "var(--s1)", "data-t": String(pt.t), "data-v": String(pt.done)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(pt.t), progStepRows(pt.done, total, pt.prog || 0));
        var root = svg.parentNode && svg.parentNode.parentNode;
        if (root && root.actCrossMove) root.actCrossMove(pt.t);
      });
      dot.addEventListener("mouseleave", function () {
        hideTip();
        var root = svg.parentNode && svg.parentNode.parentNode;
        if (root && root.actCrossHide) root.actCrossHide();
      });
      dot.addEventListener("focus", function (ev) {
        showTip(ev, fmtDate(pt.t), progStepRows(pt.done, total, pt.prog || 0));
      });
      dot.addEventListener("blur", hideTip);
      dot.setAttribute("tabindex", "0");
      svg.appendChild(dot);
    });
    /* Milestone dots with % labels for reached marks inside the range. */
    (p.milestones || []).forEach(function (m) {
      if (!m.reached || m.t == null || m.t < from || m.t > to) return;
      var dot = s("circle", {cx: actR1(X(m.t)), cy: actR1(Y(m.pct)), r: "4.5", fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": "1.5", "data-milestone": String(m.pct), "data-t": String(m.t), "data-v": String(m.done)});
      dot.addEventListener("mousemove", function (ev) {
        showTip(ev, fmtDate(m.t) + " — " + m.pct + "% first reached", progMileRows(m.done, total, m.elapsed));
      });
      dot.addEventListener("mouseleave", hideTip);
      dot.addEventListener("focus", function (ev) {
        showTip(ev, fmtDate(m.t) + " — " + m.pct + "% first reached", progMileRows(m.done, total, m.elapsed));
      });
      dot.addEventListener("blur", hideTip);
      dot.setAttribute("tabindex", "0");
      svg.appendChild(dot);
      var lab = s("text", {x: Math.min(actR1(X(m.t)) + 8, L + pw - 4), y: actR1(Y(m.pct)) + 4, class: "plab"});
      lab.textContent = m.pct + "%";
      svg.appendChild(lab);
    });
    axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: 100, yTicks: 4, yFmt: progPctFmt, xTicks: withX ? st.ticks : [], yTitle: "% of tasks", xTitle: withX ? "time" : null});
    actCrossOf(svg, T, T + ph);
    wrap.appendChild(svg);
    /* Capped history: the series starts before the loaded window, so
       the strip clips and names the cut instead of implying full
       history. Only when such data exists. */
    if (p.start < loaded.t0)
      wrap.appendChild(el("p", {class: "prog-note", text: "Earlier history before " + fmtDate(loaded.t0)}));
    return {node: wrap, hasData: true};
  }

  /* Live-agents strips: one gradient strip per model family on its own
     scale and the shared x, with peak dot and pk annotation. Only the
     bottom strip of the stack carries x labels. */
  var ACT_TOOLFAM = {grok: "cursor", muse: "muse", glm: "glm", claude: "claude", deepseek: "deepseek", beam: "beam", codex: "codex", commandcode: "commandcode", other: "other"};
  function actMods(p) {
    if (p.models) return p.models;
    var o = {};
    Object.keys(ACT_TOOLFAM).forEach(function (t) { if (p[t]) o[ACT_TOOLFAM[t] + "-unknown"] = p[t]; });
    return o;
  }
  /* Strip families come from the window's own model keys in ADM.FAMS order,
     never a hard-coded tool list: a family with no runs in the window gets
     no strip. Job rows never appear among the keys (they are not counted
     agents), so no family needs excluding by name. Pure: no DOM, no state. */
  function actConcFams(keys) {
    var fams = [];
    (keys || []).forEach(function (k) {
      var f = ADM.seriesFam(k);
      if (fams.indexOf(f) < 0) fams.push(f);
    });
    return ADM.FAMS.filter(function (f) { return fams.indexOf(f) >= 0; });
  }
  function actConcStrips(range, loaded, g, withX) {
    var P = (loaded.conc || []).filter(function (p) { return p.t >= range.from - 60 && p.t <= range.to; });
    if (!P.length) return [];
    var seen = {};
    P.forEach(function (p) { var m = actMods(p); for (var k in m) if (m[k]) seen[k] = 1; });
    var fams = actConcFams(Object.keys(seen));
    if (!fams.length) return [];
    return fams.map(function (f, i) { return actConcStrip(range, loaded, g, f, false, P, seen, i === fams.length - 1); });
  }
  function actConcStrip(range, loaded, g, fam, withX, P, seen, isLast) {
    var from = range.from, to = range.to;
    P = P || (loaded.conc || []).filter(function (p) { return p.t >= from - 60 && p.t <= to; });
    seen = seen || {};
    var all = ADM.sortSeries(Object.keys(seen).length ? Object.keys(seen) : Object.keys((function () { var o = {}; P.forEach(function (p) { var m = actMods(p); for (var k in m) if (m[k]) o[k] = 1; }); return o; })()));
    function famKeys(f) { return all.filter(function (k) { return ADM.seriesFam(k) === f; }); }
    var ks = famKeys(fam);
    var series = P.map(function (p) { var m = actMods(p), sum = 0; ks.forEach(function (k) { sum += m[k] || 0; }); return sum; });
    var peak = 0;
    series.forEach(function (v) { peak = Math.max(peak, v); });
    var mx = Math.max(2, Math.ceil(peak / 2) * 2);
    var attrs = {class: "conc-strip act-strip", "data-fam": fam, "data-peak": String(peak)};
    if (isLast) attrs.id = "exec-live";
    var wrap = el("div", attrs);
    var head = el("div", {class: "conc-head"});
    var dot = el("i", {class: "sw"}); dot.style.background = ADM.famColor(fam); head.appendChild(dot);
    head.appendChild(el("span", {class: "conc-fam", text: ADM.FAM[fam].name}));
    /* The model list omits runs without a recorded model by default; the
       curve above still includes them, so a fully unattributed strip
       counts them instead of reading empty. */
    var dispKs = AdUnatt.shown(actScope()) ? ks : AdUnatt.filterKnown(ks);
    var hidKs = ks.length - dispKs.length;
    head.appendChild(el("span", {class: "conc-models", text: dispKs.length ? dispKs.map(function (k) { return ADM.seriesName(k); }).join(", ") + (hidKs ? " · +" + fmtN(hidKs) + " unattributed" : "") : (ks.length ? "+" + fmtN(ks.length) + " unattributed" : "no models seen")}));
    head.appendChild(el("span", {class: "conc-pk", text: "pk " + peak}));
    wrap.appendChild(head);
    var W = g.W, L = g.L, R = g.R, H = withX ? 112 : 72, T = 20, Bm = withX ? 36 : 10, pw = g.pw, ph = H - T - Bm;
    var svg = s("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, style: "display:block;max-width:100%", role: "img", "data-strip": "conc", "data-fam": fam, "data-peak": String(peak), "data-x-from": String(Math.round(from)), "data-x-to": String(Math.round(to)), "aria-label": actConcAria(ADM.FAM[fam].name, peak)});
    var st = actSharedTicks(range, g), X = st.X, Y = function (v) { return T + ph - ph * v / mx; };
    /* Gridlines on the shared ticks in every strip; labels only bottom. */
    actXGrid(svg, st.ticks, T, T + ph);
    var gid = "ad-act-" + fam;
    var defs = s("defs");
    var grad = s("linearGradient", {id: gid, x1: "0", y1: "0", x2: "0", y2: "1"});
    grad.appendChild(s("stop", {offset: "0", "stop-color": ADM.famColor(fam), "stop-opacity": ".45"}));
    grad.appendChild(s("stop", {offset: "1", "stop-color": ADM.famColor(fam), "stop-opacity": "0"}));
    defs.appendChild(grad); svg.appendChild(defs);
    var pts = P.map(function (p, i) { return Math.round(X(p.t)) + "," + Math.round(Y(series[i])); }).join(" ");
    svg.appendChild(s("polygon", {points: L + "," + (T + ph) + " " + pts + " " + (L + pw) + "," + (T + ph), fill: "url(#" + gid + ")"}));
    svg.appendChild(s("polyline", {points: pts, fill: "none", stroke: ADM.famColor(fam), "stroke-width": 1.8, "stroke-linejoin": "round"}));
    var pi = 0;
    series.forEach(function (v, i) { if (v === peak) pi = i; });
    var px = X(P[pi].t), py = Y(peak);
    svg.appendChild(s("circle", {cx: Math.round(px), cy: Math.round(py), r: 3, fill: ADM.famColor(fam)}));
    var pl = s("text", {y: Math.max(py - 6, T - 8), class: "conc-pk-lab"});
    if (px > L + pw - 46) { pl.setAttribute("x", Math.round(px) - 8); pl.setAttribute("text-anchor", "end"); }
    else { pl.setAttribute("x", Math.round(px) + 8); pl.setAttribute("text-anchor", "start"); }
    pl.textContent = "pk " + peak; svg.appendChild(pl);
    axes(svg, {L: L, T: T, pw: pw, ph: ph, yMax: mx, yTicks: 2, xTicks: withX ? st.ticks : [], yTitle: "agents", xTitle: withX ? "time" : null});
    var hit = s("rect", {x: L, y: T, width: pw, height: ph, fill: "transparent"});
    hit.addEventListener("mousemove", function (ev) {
      var bb = svg.getBoundingClientRect();
      var tt = actXToTime((ev.clientX - bb.left) * W / Math.max(1, bb.width), from, to, L, pw);
      var best = P[0];
      P.forEach(function (p) { if (Math.abs(p.t - tt) < Math.abs(best.t - tt)) best = p; });
      var m = actMods(best), rows = [];
      var showCU = AdUnatt.shown(actScope());
      actConcFams(all).forEach(function (f) {
        var fks = all.filter(function (k) { return ADM.seriesFam(k) === f; });
        var tot = fks.reduce(function (a, k) { return a + (m[k] || 0); }, 0);
        rows.push({k: ADM.FAM[f].name, v: fmtN(tot), c: ADM.famColor(f)});
        var hid = 0;
        fks.forEach(function (k) {
          if (!m[k]) return;
          if (!showCU && AdUnatt.isUnattributed(k)) { hid += m[k]; return; }
          rows.push({k: "\u2003" + ADM.seriesName(k), v: fmtN(m[k]), c: ADM.seriesColor(k, all)});
        });
        if (hid) rows.push({k: "\u2003Unattributed", v: fmtN(hid), c: "var(--muted)"});
      });
      showTip(ev, fmtT(best.t), rows);
      var root = svg.parentNode && svg.parentNode.parentNode;
      if (root && root.actCrossMove) root.actCrossMove(best.t);
    });
    hit.addEventListener("mouseleave", function () {
      hideTip();
      var root = svg.parentNode && svg.parentNode.parentNode;
      if (root && root.actCrossHide) root.actCrossHide();
    });
    svg.appendChild(hit);
    actCrossOf(svg, T, T + ph);
    wrap.appendChild(svg);
    return {node: wrap, fam: fam};
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.activityCard = activityCard;
  AdPage.cards.actTokenStrip = actTokenStrip;
  AdPage.cards.actTimelineStrip = actTimelineStrip;
  AdPage.cards.actConcStrips = actConcStrips;
  AdPage.cards.actBrush = actBrush;
  AdPage.activityCard = activityCard;
  AdPage.actTokenStrip = actTokenStrip;
  AdPage.actTimelineStrip = actTimelineStrip;
  AdPage.actConcStrips = actConcStrips;
  AdPage.actConcStrip = actConcStrip;
  AdPage.actConcFams = actConcFams;
  AdPage.actBrush = actBrush;
  AdPage.actClampRange = actClampRange;
  AdPage.actPresetRange = actPresetRange;
  AdPage.actTimeToX = actTimeToX;
  AdPage.actXToTime = actXToTime;
  AdPage.actDragHandle = actDragHandle;
  AdPage.actDragMove = actDragMove;
  AdPage.actTickStep = actTickStep;
  AdPage.cards.actProgStrip = actProgStrip;
  AdPage.actProgStrip = actProgStrip;
  AdPage.actProgKey = actProgKey;
  AdPage.actProgStamp = actProgStamp;
  AdPage.actProgY = actProgY;
  AdPage.actProgClip = actProgClip;
  AdPage.actProgHas = actProgHas;
  AdPage.actRangeKey = actRangeKey;
  AdPage.actRangeLabel = actRangeLabel;
  AdPage.actLegendTip = actLegendTip;
  AdPage.actTimelineAria = actTimelineAria;
  AdPage.actConcAria = actConcAria;
  global.activityCard = activityCard;
  global.actTokenStrip = actTokenStrip;
  global.actTimelineStrip = actTimelineStrip;
  global.actConcStrips = actConcStrips;
  global.actConcStrip = actConcStrip;
  global.actConcFams = actConcFams;
  global.actBrush = actBrush;
  global.actClampRange = actClampRange;
  global.actPresetRange = actPresetRange;
  global.actTimeToX = actTimeToX;
  global.actXToTime = actXToTime;
  global.actDragHandle = actDragHandle;
  global.actDragMove = actDragMove;
  global.actTickStep = actTickStep;
  global.actProgStrip = actProgStrip;
  global.actProgKey = actProgKey;
  global.actProgStamp = actProgStamp;
  global.actProgY = actProgY;
  global.actProgClip = actProgClip;
  global.actProgHas = actProgHas;
  global.actRangeKey = actRangeKey;
  global.actRangeLabel = actRangeLabel;
  global.actLegendTip = actLegendTip;
  global.actTimelineAria = actTimelineAria;
  global.actConcAria = actConcAria;
})(window);

