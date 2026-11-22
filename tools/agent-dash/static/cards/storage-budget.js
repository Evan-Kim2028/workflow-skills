/* Page card: Storage budget (read-only).
   The daily footprint of the agent-log stack: the VictoriaLogs store, the
   session archive and its manifest, the log archive, the agentq job
   folders, worker scratch and the session originals, plus the disk share
   and the days to 70/78/85% at the 7-day growth rate.

   The numbers come from GET /api/storage/budget, which reads the last line
   of the history file the hourly archive job appends to
   (dash/storagebudget.py). The card never measures and never writes: one
   fetch on first render, then a redraw only when a new sample lands.

   State is never colour alone: the pill carries a glyph and a word, and
   the thresholds are printed under the table. A missing sample shows the
   command that takes one, never an error page.

   Reads: state (not used for data), window.drawMain, secCache.
   Exports (on window.AdPage.cards, also kept as plain globals):
   storageBudgetSpec, storageBudgetCard, storageBudgetPill,
   storageBudgetDays.
   Needs: el, api, keyOf, drawMain, secCache, fmtBytes, fmtN, fmtDate
   (plain globals, call time only). */
(function (global) {
  "use strict";
  var SB_SEC = "storagebudget";
  var SB_URL = "/api/storage/budget";
  var SB_INSTALL = "python3 agent_dash.py storage budget";
  var SB_COMPONENTS = [
    ["vlogs_compressed", "VictoriaLogs store"],
    ["session_archive", "Session archive"],
    ["session_manifest", "Session manifest"],
    ["log_archive", "Log archive"],
    ["agentq_jobs", "agentq job folders"],
    ["scratch", "Worker scratch"],
    ["worktrees", "Git worktrees"],
    ["session_originals", "Session originals"]
  ];

  /* Module state. Survives card rebuilds, so a redraw shows the same
     sample instead of refetching. */
  var sb = {payload: null, loading: false, error: ""};

  /* Pure: the state pill (glyph plus word, never colour alone). */
  function storageBudgetPill(state) {
    var word = String(state == null ? "" : state);
    if (word === "alert") return {cls: "sb-pill no", text: "\u2717 Alert"};
    if (word === "warn") return {cls: "sb-pill wait", text: "\u25B2 Warn"};
    if (word === "ok") return {cls: "sb-pill ok", text: "\u2713 OK"};
    return {cls: "sb-pill other", text: "\u25CB No sample"};
  }

  /* Pure: days as a short word ("812 d", "1.2 y", "-" when unknown). */
  function storageBudgetDays(days) {
    if (days == null || !isFinite(+days)) return "-";
    var value = +days;
    if (value < 0) return "past";
    if (value >= 730) return (Math.round(value / 36.5) / 10) + " y";
    return Math.round(value) + " d";
  }

  function sbRate(bytesPerDay) {
    if (bytesPerDay == null || !isFinite(+bytesPerDay)) return "-";
    var sign = +bytesPerDay > 0 ? "+" : "";
    return sign + fmtBytes(Math.abs(+bytesPerDay)) + "/d";
  }

  function sbPayload() {
    var p = sb.payload;
    return (p && typeof p === "object") ? p : null;
  }

  function redrawStorage() {
    try {
      if (typeof secCache !== "undefined" && secCache) delete secCache[SB_SEC];
    } catch (e) {}
    if (typeof drawMain === "function") drawMain();
  }

  function loadStorageBudget() {
    if (sb.loading) return;
    sb.loading = true;
    api(SB_URL, {}).then(function (j) {
      sb.loading = false;
      sb.error = "";
      sb.payload = (j && typeof j === "object") ? j : null;
      redrawStorage();
    }).catch(function () {
      sb.loading = false;
      sb.error = "request failed";
      redrawStorage();
    });
  }

  function sbRow(name, label, sample, rates) {
    var comp = (sample.components || {})[name] || {};
    var rate = (rates.components || {})[name];
    var tr = el("tr", {class: "sb-row"});
    tr.appendChild(el("th", {scope: "row", text: label}));
    tr.appendChild(el("td", {class: "sb-num", text: fmtBytes(comp.physical || 0)}));
    tr.appendChild(el("td", {class: "sb-num", text: fmtBytes(comp.bytes || 0)}));
    tr.appendChild(el("td", {class: "sb-num", text: sbRate(rate)}));
    tr.appendChild(el("td", {class: "sb-num", text: comp.units ? fmtN(comp.units) : "-"}));
    return tr;
  }

  function storageBudgetCard() {
    var p = sbPayload() || {};
    var sample = p.sample || null;
    var rates = p.growth || {};
    var projection = p.projection || {};
    var days = projection.days || {};
    var th = p.thresholds || {};
    var c = el("div", {class: "card sb-card", id: "exec-storage-budget", "data-nav": "Storage budget"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Storage budget"}));
    head.appendChild(el("p", {class: "sub", text: "Daily footprint of the agent-log stack, read-only."}));
    c.appendChild(head);

    if (!sample) {
      var empty = el("div", {class: "sb-empty"});
      empty.appendChild(el("p", {text: sb.error ? "The storage budget endpoint did not answer." : "No measurement yet."}));
      empty.appendChild(el("p", {class: "sb-empty-note", text: "The hourly archive job takes one a day. To take one now:"}));
      var pre = el("pre", {class: "sb-cmd"});
      pre.appendChild(el("code", {text: SB_INSTALL}));
      empty.appendChild(pre);
      c.appendChild(empty);
      return c;
    }

    var pill = storageBudgetPill(p.state);
    var top = el("div", {class: "sb-top"});
    top.appendChild(el("span", {class: pill.cls, text: pill.text}));
    var disk = sample.disk || {};
    top.appendChild(el("span", {
      class: "sb-disk",
      text: fmtBytes(disk.used || 0) + " of " + fmtBytes(disk.total || 0) + " used (" +
        (Math.round((+disk.pct || 0) * 10) / 10) + "%), " + fmtBytes(disk.free || 0) + " free"
    }));
    if (sample.iso) top.appendChild(el("span", {class: "sb-when", text: "measured " + fmtDate(sample.ts)}));
    c.appendChild(top);

    var table = el("table", {class: "sb-table"});
    var thead = el("thead");
    var hr = el("tr");
    ["Component", "On disk", "Apparent", "7-day rate", "Units"].forEach(function (label, i) {
      hr.appendChild(el("th", {scope: "col", class: i ? "sb-num" : "", text: label}));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el("tbody");
    SB_COMPONENTS.forEach(function (pair) { tbody.appendChild(sbRow(pair[0], pair[1], sample, rates)); });
    var foot = el("tr", {class: "sb-foot"});
    foot.appendChild(el("th", {scope: "row", text: "Footprint (no double count)"}));
    foot.appendChild(el("td", {class: "sb-num", text: fmtBytes(sample.footprint_physical || 0)}));
    foot.appendChild(el("td", {class: "sb-num", text: fmtBytes(sample.footprint || 0)}));
    foot.appendChild(el("td", {class: "sb-num", text: sbRate(rates.footprint)}));
    foot.appendChild(el("td", {class: "sb-num", text: "-"}));
    tbody.appendChild(foot);
    table.appendChild(tbody);
    c.appendChild(table);

    var worktrees = (sample.components || {}).worktrees || {};
    if (worktrees.count) {
      c.appendChild(el("p", {
        class: "sb-note",
        text: "Worktrees: " + fmtN(worktrees.count) + ", oldest idle " +
          (worktrees.idle_hours == null ? "-" : Math.round(worktrees.idle_hours) + " h") +
          (worktrees.idle_path ? " (" + worktrees.idle_path + ")" : "")
      }));
    }

    var proj = el("p", {class: "sb-proj"});
    proj.appendChild(el("b", {text: "Days to full: "}));
    proj.appendChild(document.createTextNode(
      "70% " + storageBudgetDays(days["70"]) + " \u00B7 78% " + storageBudgetDays(days["78"]) +
      " \u00B7 85% " + storageBudgetDays(days["85"]) + " at " + sbRate(projection.rate_bytes_per_day)
    ));
    c.appendChild(proj);
    c.appendChild(el("p", {
      class: "sb-note",
      text: "On disk counts each physical extent once (APFS clones share extents); " +
        "files under 64 KiB are counted at their apparent size. The projection uses the on-disk rate."
    }));
    c.appendChild(el("p", {
      class: "sb-note",
      text: "Warn at " + (th.warn_disk_pct || 70) + "% used or under " + (th.warn_days_to_80 || 365) +
        " days to 80%; alert at " + (th.alert_disk_pct || 78) + "% or under " + (th.alert_days_to_80 || 180) +
        " days. Nothing here deletes: logs are kept with maximum retention."
    }));
    if (sb.error) c.appendChild(el("p", {class: "sb-note", text: "Last refresh failed; showing the previous sample."}));
    return c;
  }

  function storageBudgetSpec(passedState, snap) {
    if (!sbPayload() && !sb.loading && !sb.error) loadStorageBudget();
    var sample = sbPayload() && sbPayload().sample;
    var key = keyOf(["storagebudget", sample ? sample.ts : 0, sb.error]);
    return [SB_SEC, key, storageBudgetCard, "margin-top:14px"];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.storagebudget = storageBudgetSpec;
  global.storageBudgetSpec = storageBudgetSpec;
  global.storageBudgetCard = storageBudgetCard;
  global.storageBudgetPill = storageBudgetPill;
  global.storageBudgetDays = storageBudgetDays;
})(window);
