/* Page card: pipeline ledger.
   Four ops.asset_ledger-backed tiles (assets loaded, rows out, failed
   loads, slowest load) plus the most recent ledger rows, for the overview
   Execution tab. Row grain follows the pipeline-ledger-read contract:
   one row per (run_id, asset_key, partition, attempt)
   carrying status, error_class, duration_ms and rows_in/rows_out/
   rows_inserted/rows_updated/rows_deleted.
   Reads: the snap passed in; snap.ledger is the ledger payload a server
   lane provides (absent until that lane lands, so the card stays dark).
   Null contract: ledgerSpec returns null when snap.ledger is missing or
   empty (the card does not apply: no data). A failed snapshot fetch with
   no cached response renders "request failed" with data-failed-scope,
   never zeros.
   Exports (on window.AdPage.cards, also kept as plain globals): ledgerSpec,
   ledgerCard, plus the ledgerSum/ledgerRows/ledgerRecent data helpers and
   the led* hover builders below (shared only inside this file).
   Needs: el, fmtN, fmtDur, showTip, hideTip, keyOf, state (plain globals). */
(function (global) {
  "use strict";
  var LEDGER_TOP_N = 10;

  function ledgerIsNum(x) { return typeof x === "number" && isFinite(x); }

  /* Ledger rows ride the snapshot as snap.ledger; anything else means the
     server lane has not landed yet, so there is no card. */
  function ledgerRows(snap) {
    var rows = (snap || {}).ledger;
    return Array.isArray(rows) ? rows : [];
  }

  function ledgerNum(row, key) {
    var v = row ? row[key] : null;
    return ledgerIsNum(v) ? v : 0;
  }

  /* Pure rollup over ledger rows: distinct assets, jobs and runs, row
     counts, failed loads with their top error class, and the slowest
     load by duration_ms. */
  function ledgerSum(rows) {
    var assets = {}, jobs = {}, runs = {}, fails = {};
    var rowsIn = 0, rowsOut = 0, rowsChanged = 0, failed = 0;
    var slowest = {asset_key: null, duration_ms: null};
    rows.forEach(function (r) {
      if (r.asset_key != null) assets[r.asset_key] = true;
      if (r.job != null) jobs[r.job] = true;
      if (r.run_id != null) runs[r.run_id] = true;
      rowsIn += ledgerNum(r, "rows_in");
      rowsOut += ledgerNum(r, "rows_out");
      rowsChanged += ledgerNum(r, "rows_inserted") + ledgerNum(r, "rows_updated") + ledgerNum(r, "rows_deleted");
      if (r.status === "failed") {
        failed++;
        var cls = r.error_class || "unknown";
        fails[cls] = (fails[cls] || 0) + 1;
      }
      var ms = (r && ledgerIsNum(r.duration_ms)) ? r.duration_ms : null;
      if (ms != null && (slowest.duration_ms == null || ms > slowest.duration_ms)) {
        slowest = {asset_key: r.asset_key || "unknown", duration_ms: ms};
      }
    });
    var topClass = null, topN = 0;
    for (var cls in fails) {
      if (fails[cls] > topN) { topClass = cls; topN = fails[cls]; }
    }
    return {
      assets: Object.keys(assets).length,
      jobs: Object.keys(jobs).length,
      runs: Object.keys(runs).length,
      rowsIn: rowsIn, rowsOut: rowsOut, rowsChanged: rowsChanged,
      failed: failed, topClass: topClass, topN: topN, slowest: slowest
    };
  }

  /* Most recent first by finished_at (started_at when unfinished), capped
     at n for the table. */
  function ledgerRecent(rows, n) {
    return rows.slice().sort(function (a, b) {
      var ta = (a && (a.finished_at || a.started_at)) || 0;
      var tb = (b && (b.finished_at || b.started_at)) || 0;
      return tb - ta;
    }).slice(0, n);
  }

  /* One-line clip for long asset keys in tertiary lines (never a tooltip:
     the full key stays in the table cell and its hover text). */
  function ledShort(text, n) {
    var s = String(text == null ? "" : text);
    if (s.length <= n) return s;
    return s.slice(0, Math.max(0, n - 1)) + "\u2026";
  }

  /* Pure hover-text builders: compact numbers only (fmtN/fmtDur). */
  function ledAssetsTitle(assets, jobs, runs) {
    return "Loads in this window (ops.asset_ledger): " + fmtN(assets) + " assets across " + fmtN(jobs) + " jobs and " + fmtN(runs) + " runs";
  }
  function ledRowsTitle(out, changed) {
    return "Row counts in this window: " + fmtN(out) + " rows out, " + fmtN(changed) + " inserted, updated or deleted";
  }
  function ledFailTitle(failed, cls, n) {
    if (!failed) return "No failed loads in this window";
    return fmtN(failed) + " failed loads in this window, most often " + cls + " (" + fmtN(n) + ")";
  }
  function ledSlowTitle(asset, ms) {
    if (ms == null) return "No timed loads in this window";
    return "Slowest load in this window: " + asset + " took " + fmtDur(ms / 1000);
  }
  function ledRowTip(r) {
    var tip = [{k: "Asset", v: String(r.asset_key == null ? "-" : r.asset_key)}];
    if (r.job != null) tip.push({k: "Job", v: String(r.job)});
    if (r.run_id != null) tip.push({k: "Run", v: ledShort(r.run_id, 24)});
    if (r.attempt != null) tip.push({k: "Attempt", v: fmtN(r.attempt)});
    if (r.status === "failed" && r.error_class != null) tip.push({k: "Error", v: String(r.error_class)});
    tip.push({k: "Rows", v: fmtN(ledgerNum(r, "rows_in")) + " in, " + fmtN(ledgerNum(r, "rows_out")) + " out"});
    tip.push({k: "Duration", v: ledgerIsNum(r.duration_ms) ? fmtDur(r.duration_ms / 1000) : "-"});
    return tip;
  }

  /* Status as glyph plus word, never color alone. */
  function ledStatusMark(status) {
    if (status === "failed") return "\u2715 failed";
    if (status === "skipped") return "\u25CB skipped";
    return "\u2713 ok";
  }

  /* One ledger tile: label, headline value, tertiary lines and hover rows.
     Mirrors the .card.tile markup the tiles.css rules already style. */
  function ledgerTile(label, valueText, terLines, dataAttrs, title, tipRows) {
    var attrs = {class: "card tile"};
    if (dataAttrs) for (var a in dataAttrs) attrs[a] = dataAttrs[a];
    if (title) attrs.title = title;
    var card = el("div", attrs, [el("div", {class: "l", text: label}), el("div", {class: "v", text: valueText})]);
    var x = el("div", {class: "x"});
    terLines.forEach(function (t) { x.appendChild(el("div", {class: "xline", text: t})); });
    card.appendChild(x);
    if (tipRows && tipRows.length) {
      card.addEventListener("mousemove", function (ev) { showTip(ev, label, tipRows); });
      card.addEventListener("mouseleave", hideTip);
    }
    return card;
  }

  /* Status chip: colour plus symbol plus word, never colour alone. */
  function ledgerChip(failed) {
    if (failed > 0) return el("span", {class: "delta down", title: ledFailTitle(failed, null, 0)}, ["\u25BC ", fmtN(failed), el("span", {class: "w", text: "failed"})]);
    return el("span", {class: "delta flat", title: ledFailTitle(0, null, 0)}, ["\u25C6 flat", el("span", {class: "w", text: "no failures"})]);
  }

  function ledgerCard(rows) {
    var sum = ledgerSum(rows);
    var recent = ledgerRecent(rows, LEDGER_TOP_N);
    var c = el("div", {class: "card", id: "exec-ledger", "data-nav": "Ledger"});
    c.appendChild(el("h2", {text: "Pipeline ledger"}));
    c.appendChild(el("p", {class: "sub", text: "One row per load attempt (run, asset, retry): status, wall-clock time and row counts from ops.asset_ledger."}));
    var grid = el("div", {class: "grid g-tiles"});
    grid.appendChild(ledgerTile(
      "Ledger assets (24 h)", fmtN(sum.assets),
      [fmtN(sum.runs) + " loads \u00B7 " + fmtN(sum.jobs) + " jobs"],
      {"data-ledger": "assets", "data-v": sum.assets},
      ledAssetsTitle(sum.assets, sum.jobs, sum.runs),
      [{k: "Assets", v: fmtN(sum.assets)}, {k: "Jobs", v: fmtN(sum.jobs)}, {k: "Runs", v: fmtN(sum.runs)}]
    ));
    grid.appendChild(ledgerTile(
      "Rows out (24 h)", fmtN(sum.rowsOut),
      ["changed " + fmtN(sum.rowsChanged)],
      {"data-ledger": "rows", "data-v": sum.rowsOut},
      ledRowsTitle(sum.rowsOut, sum.rowsChanged),
      [{k: "Rows out", v: fmtN(sum.rowsOut)}, {k: "Rows in", v: fmtN(sum.rowsIn)}, {k: "Changed", v: fmtN(sum.rowsChanged)}]
    ));
    var failTile = ledgerTile(
      "Failed loads (24 h)", fmtN(sum.failed),
      [sum.failed ? ("top: " + sum.topClass + " (" + fmtN(sum.topN) + ")") : "no failures"],
      {"data-ledger": "failed", "data-v": sum.failed},
      ledFailTitle(sum.failed, sum.topClass, sum.topN),
      sum.failed ? [{k: "Failed", v: fmtN(sum.failed)}, {k: "Top error", v: String(sum.topClass)}] : null
    );
    failTile.insertBefore(ledgerChip(sum.failed), failTile.childNodes[2] || null);
    grid.appendChild(failTile);
    grid.appendChild(ledgerTile(
      "Slowest load (24 h)",
      sum.slowest.duration_ms == null ? "-" : fmtDur(sum.slowest.duration_ms / 1000),
      [ledShort(sum.slowest.asset_key || "no timed loads", 40)],
      {"data-ledger": "slowest", "data-v": sum.slowest.duration_ms == null ? 0 : sum.slowest.duration_ms},
      ledSlowTitle(sum.slowest.asset_key, sum.slowest.duration_ms),
      sum.slowest.duration_ms == null ? null : [{k: "Asset", v: String(sum.slowest.asset_key)}, {k: "Duration", v: fmtDur(sum.slowest.duration_ms / 1000)}]
    ));
    c.appendChild(grid);
    c.appendChild(el("p", {class: "sub", text: "Newest " + recent.length + " of " + rows.length + " ledger rows."}));
    var t = el("table", {class: "dt", "aria-label": "Pipeline ledger"});
    var head = el("thead", null, [el("tr", null, [
      el("th", {scope: "col", class: "c-label"}, ["Asset"]),
      el("th", {scope: "col", class: "c-status"}, ["Status"]),
      el("th", {scope: "col", class: "c-num"}, ["In"]),
      el("th", {scope: "col", class: "c-num"}, ["Out"]),
      el("th", {scope: "col", class: "c-num"}, ["Changed"]),
      el("th", {scope: "col", class: "c-time"}, ["Duration"])
    ])]);
    t.appendChild(head);
    var tb = el("tbody", null, []);
    recent.forEach(function (r) {
      var changed = ledgerNum(r, "rows_inserted") + ledgerNum(r, "rows_updated") + ledgerNum(r, "rows_deleted");
      var tr = el("tr", null, [
        el("td", {class: "c-label", "data-asset": String(r.asset_key == null ? "" : r.asset_key), text: String(r.asset_key == null ? "-" : r.asset_key)}),
        el("td", {class: "c-status", "data-status": String(r.status == null ? "" : r.status), text: ledStatusMark(r.status)}),
        el("td", {class: "c-num", "data-rows": ledgerNum(r, "rows_in"), text: fmtN(ledgerNum(r, "rows_in"))}),
        el("td", {class: "c-num", "data-rows": ledgerNum(r, "rows_out"), text: fmtN(ledgerNum(r, "rows_out"))}),
        el("td", {class: "c-num", "data-rows": changed, text: fmtN(changed)}),
        el("td", {class: "c-time", "data-ms": ledgerIsNum(r.duration_ms) ? r.duration_ms : 0, text: ledgerIsNum(r.duration_ms) ? fmtDur(r.duration_ms / 1000) : "-"})
      ]);
      (function (row) {
        var tip = ledRowTip(row);
        tr.addEventListener("mousemove", function (ev) { showTip(ev, String(row.asset_key == null ? "Ledger row" : row.asset_key), tip); }); /* page-rule-allow: asset key id, not a number */
        tr.addEventListener("mouseleave", hideTip);
      })(r);
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    c.appendChild(t);
    return c;
  }

  function ledgerFailed(message) {
    var c = el("div", {class: "card", id: "exec-ledger", "data-nav": "Ledger"});
    c.appendChild(el("h2", {text: "Pipeline ledger"}));
    var scope = (typeof state !== "undefined" && state && state.ws) || "all";
    c.appendChild(el("div", {class: "empty err", "data-failed-scope": scope, text: "request failed \u2014 " + message}));
    return c;
  }

  /* Registry spec: the card applies only when the snapshot carries ledger
     rows; a failed fetch with no cached response says so out loud. */
  function ledgerSpec(passedState, snap) {
    var rows = ledgerRows(snap);
    if (rows.length) {
      return ["ledger", keyOf(rows.map(function (r) {
        return [r.run_id, r.asset_key, r.partition, r.attempt, r.status, r.error_class, r.duration_ms, r.rows_in, r.rows_out, r.rows_inserted, r.rows_updated, r.rows_deleted];
      })), function () { return ledgerCard(rows); }, "margin-top:14px"];
    }
    if (typeof state !== "undefined" && state && state.snapError) {
      return ["ledger", keyOf(["failed", state.snapError]), function () { return ledgerFailed(state.snapError); }, "margin-top:14px"];
    }
    return null;
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.ledger = ledgerCard;
  AdPage.cards.ledgerSpec = ledgerSpec;
  AdPage.cards.ledgerSum = ledgerSum;
  AdPage.cards.ledAssetsTitle = ledAssetsTitle;
  AdPage.cards.ledRowsTitle = ledRowsTitle;
  AdPage.cards.ledFailTitle = ledFailTitle;
  AdPage.cards.ledSlowTitle = ledSlowTitle;
  global.ledgerSpec = ledgerSpec;
  global.ledgerCard = ledgerCard;
  global.ledgerSum = ledgerSum;
  global.ledgerRows = ledgerRows;
  global.ledgerRecent = ledgerRecent;
  global.ledAssetsTitle = ledAssetsTitle;
  global.ledRowsTitle = ledRowsTitle;
  global.ledFailTitle = ledFailTitle;
  global.ledSlowTitle = ledSlowTitle;
})(window);
