/* Page card: Archive shelf (?view=archive).
   Frozen summary cards from /api/archive (newest first), the project
   lifetime totals including archived work, and an honest empty state.
   Each card shows dates, duration, tasks, tokens, and cost through the
   shared fmt helpers, the closing-report basename with a Report link to
   the plan view, a Plan button, and a real Restore button (confirm-free,
   toast on restore) when the server allows writes, else the copy-command
   button. Archived workstream plan
   pages stay reachable read-only with an "Archived <date>" banner (the
   plan fetch needs no scope change: /api/plan and /api/bundle serve
   archived bundles directly).
   Reads: state.archive, state.snap.
   Exports (on window.AdPage.cards, also kept as plain globals):
   archiveSpecs, sortArchive, durStr.
   Needs: el, fmtN, fmtUsd, fmtDur, openPlan, copyCmd, reportFor, keyOf
   (plain globals). */
(function (global) {
  "use strict";
  /* Pure: newest-first order (archived_at descending, nulls last), the
     same order the server uses. */
  function sortArchive(cards) {
    return (cards || []).slice().sort(function (a, b) {
      var at = (a && a.archived_at) || 0, bt = (b && b.archived_at) || 0;
      if (at !== bt) return bt - at;
      var ai = String((a && (a.id || a.ws)) || ""), bi = String((b && (b.id || b.ws)) || "");
      return ai < bi ? -1 : ai > bi ? 1 : 0;
    });
  }

  /* Pure: a duration in seconds as days once past a day, else the shared
     duration helper. No toFixed anywhere near a number. */
  function durStr(s) {
    if (s == null || !isFinite(+s)) return "\u2013";
    if (+s >= 86400) return Math.floor(+s / 86400) + "d";
    return fmtDur(s);
  }

  /* Pure: UTC calendar day for an epoch, built from date parts (never a
     locale string, so screenshots and tests agree). */
  function dayStr(t) {
    if (t == null || !isFinite(+t)) return "\u2013";
    var d = new Date(+t * 1000);
    var m = d.getUTCMonth() + 1, day = d.getUTCDate();
    return d.getUTCFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (day < 10 ? "0" + day : day);
  }

  function secHead(title, sub) {
    var h = el("div", {class: "pj-head"});
    h.appendChild(el("h2", {text: title}));
    if (sub) h.appendChild(el("p", {class: "sub", text: sub}));
    return h;
  }

  function kvRow(k, v, title) {
    var tr = el("tr");
    tr.appendChild(el("td", {text: k}));
    var cell = el("td", {class: "n"});
    cell.appendChild(typeof v === "string" ? document.createTextNode(v) : v);
    if (title) cell.setAttribute("title", title);
    tr.appendChild(cell);
    return tr;
  }

  function archiveCard(card) {
    var id = card.id || card.ws || "?";
    var d = el("div", {class: "card pj-arch"});
    var head = el("div", {class: "pj-arch-head"});
    head.appendChild(el("div", {class: "pj-wn", text: id}));
    head.appendChild(el("span", {class: "pj-pill arch", text: "\u2713 Archived " + dayStr(card.archived_at), title: "Archived " + dayStr(card.archived_at) + " — nothing was deleted"}));
    d.appendChild(head);
    d.appendChild(el("div", {class: "pj-wt", text: card.title || ""}));
    var tasks = card.tasks || {done: 0, total: 0, pct: 0};
    var usage = card.usage || {};
    var trains = card.trains || {total: 0, by_status: {}};
    var appr = card.approvals || {pending: 0, approved: 0, denied: 0, total: 0};
    var table = el("table", {class: "pj-kv", "aria-label": "Archive summary for " + id});
    var body = el("tbody");
    body.appendChild(kvRow("Archived", dayStr(card.archived_at)));
    body.appendChild(kvRow("Duration", durStr(card.duration_s), "First run to archive"));
    body.appendChild(kvRow("Tasks", (tasks.done || 0) + "/" + (tasks.total || 0) + " (" + (tasks.pct || 0) + "%)"));
    body.appendChild(kvRow("Tokens", fmtN(usage.tokens_all_time || 0) + " all-time"));
    body.appendChild(kvRow("Cost", fmtUsd(usage.cost_usd_all_time || 0) + " all-time"));
    var merged = (trains.by_status && trains.by_status.merged) || 0;
    body.appendChild(kvRow("Trains", fmtN(trains.total || 0) + " total · " + fmtN(merged) + " merged"));
    body.appendChild(kvRow("Approvals", fmtN(appr.approved || 0) + " approved · " + fmtN(appr.denied || 0) + " denied"));
    body.appendChild(kvRow("Report", card.report || "none yet", card.report ? "Closing report basename — the body never reaches the dashboard" : "No closing report recorded"));
    table.appendChild(body);
    d.appendChild(table);
    var acts = el("div", {class: "pj-btns"});
    var bp = el("button", {type: "button", text: "Plan view", title: "Open the read-only plan page for " + id});
    bp.addEventListener("click", function () { openPlan(id); });
    acts.appendChild(bp);
    if (card.report) {
      var rl = el("button", {type: "button", text: "Open report", title: card.report + " lives in the bundle; opening the plan view"});
      rl.addEventListener("click", function () { openPlan(id); });
      acts.appendChild(rl);
    }
    if (typeof restoreControl === "function") acts.appendChild(restoreControl(id, card.title));
    else {
      var rb = el("button", {type: "button", class: "pj-copy", title: "Copy the exact command (read-only: nothing runs)", text: "Restore"});
      rb.addEventListener("click", function (ev) { ev.stopPropagation(); copyCmd("python3 agent_dash.py ws restore " + id); });
      acts.appendChild(rb);
    }
    d.appendChild(acts);
    return d;
  }

  function lifetimeLine(snap, archive) {
    var all = (snap && snap.totals_all_time) || {};
    var totals = (archive && archive.totals) || {workstreams: 0, tokens: 0, cost_usd: 0.0, runs: 0};
    return "Lifetime " + fmtN(ADC.billed((all.tokens) || {})) + " tokens \u00B7 " + fmtUsd(all.cost_usd || 0)
      + " \u00B7 " + fmtN(totals.runs || 0) + " archived runs"
      + " across " + fmtN(totals.workstreams || 0) + " archived " + ((totals.workstreams || 0) === 1 ? "workstream" : "workstreams")
      + " — archived work stays in every lifetime total";
  }

  /* Spec list for drawMain: lifetime header, shelf, and the rules card. */
  function archiveSpecs(passedState, snap) {
    var payload = state.archive || {archive: [], totals: {workstreams: 0, tokens: 0, cost_usd: 0.0, runs: 0}};
    var cards = sortArchive(payload.archive);
    return [["archive-shelf", keyOf([cards, payload.totals, snap && snap.totals_all_time]), function () {
      var wrap = el("div", {class: "pj-arch-wrap"});
      wrap.appendChild(secHead("Archive shelf", "Frozen summary cards — dates, duration, tasks, tokens, cost, report and plan links."));
      wrap.appendChild(el("p", {class: "pj-note", text: lifetimeLine(snap, payload)}));
      if (!cards.length) {
        var empty = el("div", {class: "card pj-empty"});
        empty.appendChild(el("p", {text: "No archived workstreams yet. Nothing was deleted: archiving moves a done workstream here after its closing report."}));
        var code = el("code", {text: "python3 agent_dash.py ws archive <ID>"});
        empty.appendChild(code);
        wrap.appendChild(empty);
        return wrap;
      }
      var shelf = el("div", {class: "pj-shelf"});
      cards.forEach(function (card) { shelf.appendChild(archiveCard(card)); });
      wrap.appendChild(shelf);
      var rules = el("div", {class: "card pj-rules"});
      rules.appendChild(el("h3", {text: "How archive works"}));
      var table = el("table", {class: "pj-kv", "aria-label": "Archive rules"});
      var body = el("tbody");
      body.appendChild(kvRow("Hidden from", "tab bar, Current and Planned views, default snapshot scope"));
      body.appendChild(kvRow("Kept in", "bundles, run rows, lifetime totals, plan pages (read-only, with a banner)"));
      body.appendChild(kvRow("Archive needs", "plan 100% (or --force with a note) and a closing report in the bundle"));
      body.appendChild(kvRow("Restore", "ws restore <ID> — the frozen card stays as previously archived"));
      table.appendChild(body);
      rules.appendChild(table);
      wrap.appendChild(rules);
      return wrap;
    }, ""]];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.archive = archiveSpecs;
  global.archiveSpecs = archiveSpecs;
  global.sortArchive = sortArchive;
  global.durStr = durStr;
})(window);
