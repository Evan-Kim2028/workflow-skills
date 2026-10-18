/* Page card: headline stat tiles.
   Renders the four tiles (agents live, agents finished, tokens, cost)
   for this view's scope, plus the request-failed state for a dead scope.
   Reads: state.ws and state.snapError; state.runs and state.conc (hourly
   shapes only, never headline totals); the snap passed in (server
   snapshot totals, never re-summed client buckets), including snap.usage
   (the hourly buckets riding the same response) and snap.prev_24h when
   the server provides it.
   Exports (on window.AdPage.cards, also kept as plain globals): tiles,
   plus the tile* helpers below (shared only inside this file).
   Needs: el, s, tierKey, cachedPct, isAgent (plain globals); window.ADC/ADM. */
(function (global) {
  "use strict";
  /* Token and cost tiles read the server snapshot totals for this scope and
     the one fixed 24 h window (windowed and reconciled server-side). They
     never re-sum usage buckets on the client, so the tile and the chart
     legend cannot drift. Delta chips prefer the exact rolling values in
     snap.prev_24h; without them the chips compare halves of this window's
     own hour buckets and say approx. in their tooltip, never a fake exact. */
  /* A failed scope never reads as zero: when the snapshot fetch failed and
     no cached response exists, the tiles say "request failed" instead of
     "0 in 24 h", so an error is loud instead of silently empty. */
  var TILE_HOUR = 3600;

  /* 24 complete-hour starts (unix seconds), oldest first, ending at hEnd. */
  function tileHours(hEnd) {
    var out = [];
    for (var i = 24; i >= 1; i--) out.push(hEnd - i * TILE_HOUR);
    return out;
  }

  /* Window anchor: the current wall-clock hour, unless every timed series
     ends more than a day back (stale fixtures, retention) — then the
     newest data hour, so previews draw the real shape, not flat zeros. */
  function tileAnchor(nowS, maxT) {
    var wall = Math.floor(nowS / TILE_HOUR) * TILE_HOUR;
    if (maxT && maxT < wall - 24 * TILE_HOUR) return Math.floor(maxT / TILE_HOUR) * TILE_HOUR + TILE_HOUR;
    return wall;
  }

  function tileIsNum(x) { return typeof x === "number" && isFinite(x); }

  /* One-bucket billed tokens: the same four fields ADC.billed sums, so a
     bucket sum and the tile headline agree by construction. */
  function tileBilled(b) {
    b = b || {};
    return (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0);
  }

  /* One concurrency point's live total: every numeric field but t. */
  function tileConcTot(p) {
    var n = 0;
    for (var k in p) { if (k !== "t" && typeof p[k] === "number" && isFinite(p[k])) n += p[k]; }
    return n;
  }

  /* One decimal without toFixed (the page rule bans it): 109.34 -> "+109.3%". */
  function tilePct1(pc) {
    var r = Math.abs(pc) >= 100 ? Math.round(pc) : Math.round(pc * 10) / 10;
    return (pc > 0 ? "+" : "") + String(r) + "%";
  }

  /* SVG decimals without toFixed, for the same rule. */
  function tileDec(x) { return String(Math.round(x * 10) / 10); }

  /* Gradient sparkline, 24 points old -> now. Decorative on purpose:
     aria-hidden with no role="img", because the tile text already carries
     every value — the chart-axes rule does not apply to this mini. */
  function tileSpark(vals, color, gid) {
    var w = 220, h = 44, p = 2, i, mx = 1, mn = 0;
    for (i = 0; i < vals.length; i++) { if (vals[i] > mx) mx = vals[i]; if (vals[i] < mn) mn = vals[i]; }
    var span = (mx - mn) || 1, n = Math.max(1, vals.length - 1);
    function X(i) { return p + i * (w - 2 * p) / n; }
    function Y(v) { return h - p - (v - mn) / span * (h - 2 * p); }
    var pts = vals.map(function (v, j) { return tileDec(X(j)) + "," + tileDec(Y(v)); }).join(" ");
    var svg = s("svg", {class: "spark", viewBox: "0 0 " + w + " " + h, preserveAspectRatio: "none", "aria-hidden": "true"});
    var defs = s("defs", {}), grad = s("linearGradient", {id: gid, x1: "0", y1: "0", x2: "0", y2: "1"});
    var stop0 = s("stop", {offset: "0"}), stop1 = s("stop", {offset: "1"});
    stop0.setAttribute("style", "stop-color:" + color + ";stop-opacity:.45");
    stop1.setAttribute("style", "stop-color:" + color + ";stop-opacity:0");
    grad.appendChild(stop0); grad.appendChild(stop1); defs.appendChild(grad); svg.appendChild(defs);
    var area = s("polygon", {points: p + "," + (h - p) + " " + pts + " " + (w - p) + "," + (h - p)});
    area.setAttribute("style", "fill:url(#" + gid + ")");
    svg.appendChild(area);
    var line = s("polyline", {points: pts});
    line.setAttribute("style", "fill:none;stroke:" + color + ";stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round");
    svg.appendChild(line);
    var dot = s("circle", {cx: tileDec(X(vals.length - 1)), cy: tileDec(Y(vals[vals.length - 1])), r: "3"});
    dot.setAttribute("style", "fill:" + color);
    svg.appendChild(dot);
    return svg;
  }

  /* Delta chip: colour plus symbol plus word, never colour alone. Flat is
     "◆ flat", new-prior-zero is "▲ new past 24 h", never an infinite percent. */
  function tileChip(cls, symbol, valueText, word, title) {
    var c = el("span", {class: "delta " + cls, title: title});
    c.appendChild(document.createTextNode(symbol + " " + valueText));
    c.appendChild(el("span", {class: "w", text: word}));
    return c;
  }

  /* Percent chip for the finished/tokens/cost tiles. cur/prev are window
     sums in the same unit; word names the comparison ("vs prior 24 h" for
     exact rolling values, "vs earlier 12 h" for the bucket approximation). */
  function tilePctChip(cur, prev, word, title) {
    if (!tileIsNum(prev) || prev === 0) {
      if (cur > 0) return tileChip("new", "▲", "new", "past 24 h", title);
      return tileChip("flat", "◆", "flat", word, title);
    }
    var pc = 100 * (cur - prev) / prev;
    if (Math.abs(pc) < 0.5) return tileChip("flat", "◆", "flat", word, title);
    return tileChip(pc > 0 ? "up" : "down", pc > 0 ? "▲" : "▼", tilePct1(pc), word, title);
  }

  /* Count chip for the live tile: a signed count, never a percent. A null
     prior means no data at all, so the chip is omitted instead of faked. */
  function tileCountChip(cur, prev, word, title) {
    if (prev == null || !tileIsNum(prev)) return null;
    if (prev === 0) {
      if (cur > 0) return tileChip("new", "▲", "new", "past 24 h", title);
      return tileChip("flat", "◆", "flat", word, title);
    }
    var d = cur - prev;
    if (d === 0) return tileChip("flat", "◆", "flat", word, title);
    return tileChip(d > 0 ? "up" : "down", d > 0 ? "▲" : "▼", (d > 0 ? "+" : "") + fmtN(d), word, title);
  }

  /* Pure hover-text builders for the delta chips and tile tooltips:
     compact numbers only (fmtN/fmtUsd). */
  function tileTokensTitle(allTok, prev) { return "Change vs the previous rolling 24 h (exact API totals): this window " + fmtN(allTok) + ", prior " + fmtN(prev); }
  function tileTokensApproxTitle(cur12, old12) { return "Approx. change from this window's hour buckets (approx.): recent 12 h " + fmtN(cur12) + " vs earlier 12 h " + fmtN(old12) + " — the exact rolling delta arrives with snap.prev_24h"; }
  function tileCostTitle(cost, prev) { return "Change vs the previous rolling 24 h (exact API totals): this window " + fmtUsd(cost) + ", prior " + fmtUsd(prev); }
  function tileCostApproxTitle(cur12, old12) { return "Approx. change from this window's hour buckets (approx.): recent 12 h " + fmtUsd(cur12) + " vs earlier 12 h " + fmtUsd(old12) + " — the exact rolling delta arrives with snap.prev_24h"; }
  function tileFinTitle(now, prev) { return "Change vs the previous rolling 24 h (exact API count): this window " + fmtN(now) + ", prior " + fmtN(prev); }
  function tileFinApproxTitle(cur12, old12) { return "Approx. change from runs ended per hour (approx.): recent 12 h " + fmtN(cur12) + " vs earlier 12 h " + fmtN(old12) + " — the exact rolling delta arrives with snap.prev_24h"; }
  function tileLiveTitle(now, prev) { return "Live agents vs 24 h ago (exact API count): now " + fmtN(now) + ", then " + fmtN(prev); }
  function tileLiveApproxTitle(now, ago) { return "Approx. vs the earliest sampled hour in this window (approx.): now " + fmtN(now) + ", then ~" + fmtN(ago); }

  /* One tile card: label, headline value, delta chip, sparkline, and one or
     two tertiary lines. dataAttrs carry the server-total attributes the
     browser check reads; deltaCls records the chip state for tests. */
  function tileCard(label, valueText, chip, chipState, sparkEl, terLines, dataAttrs, tipRows) {
    var attrs = {class: "card tile"};
    if (dataAttrs) for (var a in dataAttrs) attrs[a] = dataAttrs[a];
    if (chipState) attrs["data-delta"] = chipState;
    var card = el("div", attrs, [el("div", {class: "l", text: label}), el("div", {class: "v", text: valueText})]);
    if (chip) card.appendChild(chip);
    if (sparkEl) card.appendChild(sparkEl);
    var x = el("div", {class: "x"});
    terLines.forEach(function (t) { x.appendChild(el("div", {class: "xline", text: t})); });
    card.appendChild(x);
    if (tipRows && tipRows.length) {
      card.addEventListener("mousemove", function (ev) { showTip(ev, label, tipRows); });
      card.addEventListener("mouseleave", hideTip);
    }
    return card;
  }

  function tiles(snap) {
    snap = snap || {};
    if (state.snapError && !snap.counts) {
      var failed = el("div", {class: "grid g-tiles", id: "exec-tiles", "data-nav": "Summary", "data-failed-scope": state.ws});
      [["Agents live now", "request failed", state.snapError, null],
       ["Agents finished (24 h)", "request failed", state.snapError, null],
       ["Tokens (24 h)", "request failed", state.snapError, {"data-tile": "tokens", "data-ws": state.ws, "data-hours": "24", "data-failed-scope": state.ws}],
       ["Est. cost (24 h)", "request failed", state.snapError, {"data-tile": "cost", "data-ws": state.ws, "data-hours": "24", "data-failed-scope": state.ws}]
      ].forEach(function (t) {
        var attrs = {class: "card tile"};
        if (t[3]) for (var a in t[3]) attrs[a] = t[3][a];
        failed.appendChild(el("div", attrs, [el("div", {class: "l", text: t[0]}), el("div", {class: "v", text: String(t[1])}), el("div", {class: "x", text: t[2]})]));
      });
      return failed;
    }
    var nowS = Date.now() / 1000;
    var run = snap.running || [], byTier = {};
    run.forEach(function (r) { var k = tierKey(r); byTier[k] = (byTier[k] || 0) + 1; });
    var tk = ((snap.totals || {}).tokens) || {};
    var allTok = ADC.billed(tk);
    var cost = (snap.totals && snap.totals.cost_usd) || 0;
    var unpTok = (snap.totals && snap.totals.unpriced_tokens) || 0;
    var unpModels = (snap.totals && snap.totals.unpriced_models) || [];
    var at = ((snap.totals_all_time || {}).tokens) || {};
    var allTimeTok = ADC.billed(at);
    var allTimeCost = (snap.totals_all_time && snap.totals_all_time.cost_usd) || 0;
    var agoTxt = ADC.ago(nowS, snap.last_activity);
    var cn = snap.counts || {}, lbt = cn.live_by_tool || {}, fv = cn.finished_by_verdict || {};
    var liveNow = cn.live_agents != null ? cn.live_agents : run.filter(isAgent).length;
    var finNow = cn.finished_in_range || 0;

    /* Exact rolling priors land in snap.prev_24h (another worker adds the
       field); until then the chips fall back to this window's own hour
       buckets and say approx. in their tooltip. */
    var p24 = (snap.prev_24h && typeof snap.prev_24h === "object") ? snap.prev_24h : null;
    var buckets = Array.isArray(snap.usage) ? snap.usage : [];
    var conc = (typeof state !== "undefined" && Array.isArray(state.conc)) ? state.conc : [];
    var runs = (typeof state !== "undefined" && Array.isArray(state.runs)) ? state.runs : [];
    var maxT = 0;
    buckets.forEach(function (b) { if (tileIsNum(b.t) && b.t > maxT) maxT = b.t; });
    conc.forEach(function (p) { if (tileIsNum(p.t) && p.t > maxT) maxT = p.t; });
    runs.forEach(function (r) { if (tileIsNum(r.ended_at) && r.ended_at > maxT) maxT = r.ended_at; });
    var hEnd = tileAnchor(nowS, maxT), hours = tileHours(hEnd);

    /* 24-point series, oldest first: hourly means of concurrency totals,
       runs ended per hour, billed tokens and cost per hour. */
    var liveHas = [];
    var liveSeries = hours.map(function (h) {
      var vals = [];
      conc.forEach(function (p) { if (tileIsNum(p.t) && p.t >= h && p.t < h + TILE_HOUR) vals.push(tileConcTot(p)); });
      liveHas.push(vals.length);
      if (!vals.length) return 0;
      return vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
    });
    function inHour(t, h) { return tileIsNum(t) && t >= h && t < h + TILE_HOUR; }
    var finSeries = hours.map(function (h) {
      var n = 0;
      /* Prod-job rows never reach this list: /api/runs leaves them out
         unless include=jobs, which no refresh path passes. */
      runs.forEach(function (r) {
        if ((r.status === "done" || r.status === "killed" || r.status === "died_early") && inHour(r.ended_at, h)) n++;
      });
      return n;
    });
    var tokSeries = hours.map(function (h) {
      var n = 0;
      buckets.forEach(function (b) { if (inHour(b.t, h)) n += tileBilled(b); });
      return n;
    });
    var costSeries = hours.map(function (h) {
      var n = 0;
      buckets.forEach(function (b) { if (inHour(b.t, h)) n += (b.cost_usd || 0); });
      return n;
    });
    function sumHalf(series, lo, hi) {
      var n = 0;
      for (var i = lo; i < hi; i++) n += series[i];
      return n;
    }
    var tokCur12 = sumHalf(tokSeries, 12, 24), tokOld12 = sumHalf(tokSeries, 0, 12);
    var costCur12 = sumHalf(costSeries, 12, 24), costOld12 = sumHalf(costSeries, 0, 12);
    var finCur12 = sumHalf(finSeries, 12, 24), finOld12 = sumHalf(finSeries, 0, 12);
    /* Fallback live prior: the earliest hour in this window that actually
       has samples (with full retention that is the hour 24 h ago, so the
       chip still reads "vs 24 h ago"); null when there are no samples, in
       which case the chip is omitted instead of faked. */
    var liveAgo = null;
    for (var hi = 0; hi < hours.length; hi++) {
      if (liveHas[hi]) { liveAgo = Math.round(liveSeries[hi]); break; }
    }

    /* Deltas: exact rolling priors when the server sends them, else the
       hour-bucket approximation (recent 12 h vs the earlier 12 h), whose
       chip tooltip always carries "approx.". */
    var tokDelta, tokWord, tokTitle, tokPrevTxt;
    if (p24 && tileIsNum(p24.tokens)) {
      tokWord = "vs prior 24 h";
      tokTitle = tileTokensTitle(allTok, p24.tokens);
      tokDelta = tilePctChip(allTok, p24.tokens, tokWord, tokTitle);
      tokPrevTxt = fmtN(p24.tokens);
    } else {
      tokWord = "vs earlier 12 h";
      tokTitle = tileTokensApproxTitle(tokCur12, tokOld12);
      tokDelta = tilePctChip(tokCur12, tokOld12, tokWord, tokTitle);
      tokPrevTxt = fmtN(tokOld12) + " (approx.)";
    }
    var costDelta, costWord, costTitle, costPrevTxt;
    function costStr(x) { return fmtUsd(x); }
    if (p24 && tileIsNum(p24.cost_usd)) {
      costWord = "vs prior 24 h";
      costTitle = tileCostTitle(cost, p24.cost_usd);
      costDelta = tilePctChip(cost, p24.cost_usd, costWord, costTitle);
      costPrevTxt = costStr(p24.cost_usd);
    } else {
      costWord = "vs earlier 12 h";
      costTitle = tileCostApproxTitle(costCur12, costOld12);
      costDelta = tilePctChip(costCur12, costOld12, costWord, costTitle);
      costPrevTxt = costStr(costOld12) + " (approx.)";
    }
    var finDelta, finWord, finTitle, finPrevTxt;
    if (p24 && tileIsNum(p24.finished)) {
      finWord = "vs prior 24 h";
      finTitle = tileFinTitle(finNow, p24.finished);
      finDelta = tilePctChip(finNow, p24.finished, finWord, finTitle);
      finPrevTxt = fmtN(p24.finished);
    } else {
      finWord = "vs earlier 12 h";
      finTitle = tileFinApproxTitle(finCur12, finOld12);
      finDelta = tilePctChip(finCur12, finOld12, finWord, finTitle);
      finPrevTxt = fmtN(finOld12) + " (approx.)";
    }
    var liveDelta, liveTitle;
    if (p24 && tileIsNum(p24.live_agents)) {
      liveTitle = tileLiveTitle(liveNow, p24.live_agents);
      liveDelta = tileCountChip(liveNow, p24.live_agents, "vs 24 h ago", liveTitle);
    } else {
      liveTitle = liveAgo == null ? "" : tileLiveApproxTitle(liveNow, liveAgo);
      liveDelta = tileCountChip(liveNow, liveAgo, "vs 24 h ago", liveTitle);
    }
    function chipState(chip) {
      if (!chip) return "none";
      var cls = chip.getAttribute("class") || "";
      if (cls.indexOf("delta new") >= 0) return "new";
      if (cls.indexOf("delta up") >= 0) return "up";
      if (cls.indexOf("delta down") >= 0) return "down";
      return "flat";
    }

    /* Tertiary lines: one fact per line, at most two lines, never crammed.
       Live agents with no model yet (a process seen before its session,
       manifest, or usage match) count in the headline but never in a
       model bucket: the family line reads them as "starting", never as
       "model not recorded". */
    var lbm = cn.live_by_model || {}, lbt = cn.live_by_tool || {}, byFam = {};
    if (!Object.keys(lbm).length) run.forEach(function (r) { if (!isAgent(r)) return; if (!r.model && !r.verdict && !ADC.billed(r.tokens || {})) return; var k = tierKey(r); lbm[k] = (lbm[k] || 0) + 1; });
    ADM.sortKeys(Object.keys(lbm).filter(function (k) { return lbm[k]; })).forEach(function (k) { var f = ADM.famOf(k); (byFam[f] = byFam[f] || []).push(k); });
    var toolFam = {grok: "cursor", muse: "muse", glm: "glm", claude: "claude", deepseek: "deepseek", other: "other"};
    Object.keys(lbt || {}).forEach(function (t) { if (lbt[t] && toolFam[t] && !byFam[toolFam[t]]) byFam[toolFam[t]] = []; });
    var liveLines = [ADM.FAMS.filter(function (f) { return byFam[f]; }).map(function (f) { var ks = byFam[f], n = ks.reduce(function (a, k) { return a + lbm[k]; }, 0); var toolN = 0; Object.keys(lbt || {}).forEach(function (t) { if (toolFam[t] === f) toolN += lbt[t]; }); var bits = ks.map(function (k) { return ADM.nameOf(k) + " " + fmtN(lbm[k]); }); if (toolN > n) bits.push("starting " + fmtN(toolN - n)); return ADM.FAM[f].name + " " + fmtN(Math.max(n, toolN)) + " (" + bits.join(", ") + ")"; }).join(" · ") || "idle"];
    /* Stalled workers are stuck, not live: their own words, never folded
       into the live count. */
    if (cn.stalled) liveLines.push("◔ " + fmtN(cn.stalled) + " stalled");
    var jobBits = [];
    if (cn.prod_jobs) jobBits.push(fmtN(cn.prod_jobs) + " prod jobs");
    if (cn.prod_jobs_finished) jobBits.push(fmtN(cn.prod_jobs_finished) + " prod jobs ran");
    if (jobBits.length) liveLines.push(jobBits.join(" · "));
    /* One derivation for the caption too: finished_by_verdict carries
       outcome classes (dash/outcomes.py), so blocked (⊘ serious orange,
       a reported verdict) and died (✕ critical red, the session ended
       without one) never share a bucket here either. */
    var passN = fv.ok || 0, part = fv.part || 0, blockedN = fv.blocked || 0;
    var diedN = fv.died || 0, cancN = fv.cancelled || 0, unkN = fv.unk || 0;
    /* No outcomes recorded means no outcome line, not a row of zeros. */
    var finBits = ["✓ " + fmtN(passN) + " pass", "◐ " + fmtN(part) + " partial", "⊘ " + fmtN(blockedN) + " blocked", "✕ " + fmtN(diedN) + " died"];
    if (cancN) finBits.push("⏹ " + fmtN(cancN) + " cancelled");
    if (unkN) finBits.push("○ " + fmtN(unkN) + " no verdict");
    var finLines = (passN + part + blockedN + diedN + cancN + unkN) ? [finBits.join(" · ") + " (24 h)"] : [];
    var tokVal = allTok ? fmtN(allTok) : "0 in 24 h";
    var tokLines = allTok
      ? [cachedPct(tk) + "% cached · out " + fmtN(tk.output || 0), fmtN(allTimeTok) + " all time"]
      : (agoTxt ? ["last run " + agoTxt, fmtN(allTimeTok) + " all time"] : ["no runs yet"]);
    var costLines = [
      unpTok ? "excl. " + fmtN(unpTok) + " tokens unpriced (" + unpModels.join(", ") + ")" : "every model priced",
      fmtUsd(allTimeCost) + " all time"
    ];

    var g = el("div", {class: "grid g-tiles", id: "exec-tiles", "data-nav": "Summary"});
    var livePeak = liveSeries.reduce(function (a, b) { return Math.max(a, b); }, 0);
    g.appendChild(tileCard("Agents live now", fmtN(liveNow), liveDelta, chipState(liveDelta),
      tileSpark(liveSeries, "var(--s1)", "tls-live"), liveLines, null,
      [{k: "Live now", v: fmtN(liveNow)}, {k: "24 h ago", v: liveAgo == null ? "n/a" : fmtN(liveAgo)}, {k: "Peak hourly mean", v: fmtN(livePeak)}]));
    g.appendChild(tileCard("Agents finished (24 h)", fmtN(finNow), finDelta, chipState(finDelta),
      tileSpark(finSeries, "var(--good)", "tls-fin"), finLines, null,
      [{k: "This 24 h (hour buckets)", v: fmtN(sumHalf(finSeries, 0, 24))}, {k: "Prior 24 h", v: finPrevTxt}, {k: "Rolling 24 h (API)", v: fmtN(finNow)}]));
    g.appendChild(tileCard("Tokens (24 h)", tokVal, tokDelta, chipState(tokDelta),
      tileSpark(tokSeries, "var(--s4)", "tls-tok"), tokLines,
      {"data-tile": "tokens", "data-ws": state.ws, "data-hours": "24", "data-tokens": String(allTok), "data-tokens-all-time": String(allTimeTok)},
      [{k: "This 24 h (hour buckets)", v: fmtN(sumHalf(tokSeries, 0, 24))}, {k: "Prior 24 h", v: tokPrevTxt}, {k: "Rolling 24 h (API)", v: fmtN(allTok)}]));
    g.appendChild(tileCard("Est. cost (24 h)", fmtUsd(cost), costDelta, chipState(costDelta),
      tileSpark(costSeries, "var(--s5)", "tls-cost"), costLines,
      {"data-tile": "cost", "data-ws": state.ws, "data-hours": "24", "data-cost-usd": String(cost), "data-cost-all-time": String(allTimeCost), "data-unpriced-tokens": String(unpTok)},
      [{k: "This 24 h (hour buckets)", v: costStr(sumHalf(costSeries, 0, 24))}, {k: "Prior 24 h", v: costPrevTxt}, {k: "Rolling 24 h (API)", v: costStr(cost)}]));
    return g;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.tiles = tiles;
  AdPage.tileHours = tileHours;
  AdPage.tileAnchor = tileAnchor;
  AdPage.tileSpark = tileSpark;
  AdPage.tilePctChip = tilePctChip;
  AdPage.tileCountChip = tileCountChip;
  global.tiles = tiles;
  global.tileHours = tileHours;
  global.tileAnchor = tileAnchor;
  global.tileSpark = tileSpark;
  global.tilePctChip = tilePctChip;
  global.tileCountChip = tileCountChip;
  AdPage.cards.tileTokensTitle = tileTokensTitle;
  AdPage.cards.tileTokensApproxTitle = tileTokensApproxTitle;
  AdPage.cards.tileCostTitle = tileCostTitle;
  AdPage.cards.tileCostApproxTitle = tileCostApproxTitle;
  AdPage.cards.tileFinTitle = tileFinTitle;
  AdPage.cards.tileFinApproxTitle = tileFinApproxTitle;
  AdPage.cards.tileLiveTitle = tileLiveTitle;
  AdPage.cards.tileLiveApproxTitle = tileLiveApproxTitle;
  global.tileTokensTitle = tileTokensTitle;
  global.tileTokensApproxTitle = tileTokensApproxTitle;
  global.tileCostTitle = tileCostTitle;
  global.tileCostApproxTitle = tileCostApproxTitle;
  global.tileFinTitle = tileFinTitle;
  global.tileFinApproxTitle = tileFinApproxTitle;
  global.tileLiveTitle = tileLiveTitle;
  global.tileLiveApproxTitle = tileLiveApproxTitle;
})(window);
