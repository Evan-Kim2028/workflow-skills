/* Page card: Production decisions (read-only log).
   One table row per production step logged with `ws approve`, across the
   workstreams: the step, who decided it, the decision, its rollback, and
   when. Decided rows come first, newest first; undated and undecided rows
   sink to the end. Reads state.approvals (GET /api/approvals?ws=all, already
   loaded on every refresh). Nothing here waits on the owner: a row with no
   decision is a step the lead logged and has not yet recorded as decided,
   shown in a neutral pill, never as an owner ask. The card hides when no
   workstream has logged a step. Read-only; the card never writes.
   Exports (on window.AdPage.cards, also kept as plain globals):
   productionLogRows, sortProductionLog, productionLogSpec, productionLogCard.
   Needs: el, keyOf, discloseRows, discloseButton, discloseRedraw,
   readDisclosed, state (plain globals, call time only). */
(function (global) {
  "use strict";
  var PL_SEC = "production-log";

  /* Pure: flatten the approvals records into log rows. Each row carries the
     workstream, item id, step, decision word, decider, rollback and decided
     time. A row with no decision keeps decision "pending" and no decider or
     time. Records without items contribute nothing. */
  function productionLogRows(records) {
    var rows = [];
    (records || []).forEach(function (rec) {
      if (!rec || typeof rec !== "object") return;
      (rec.items || []).forEach(function (item) {
        if (!item || typeof item !== "object") return;
        var decision = String(item.status || "pending");
        var decided = decision === "approved" || decision === "denied";
        rows.push({
          ws: String(rec.ws || ""),
          id: String(item.id || ""),
          step: String(item.step || item.id || ""),
          decision: decision,
          decidedBy: decided ? String(item.decided_by || rec.decided_by || "") : "",
          rollback: item.rollback ? String(item.rollback) : "",
          at: decided ? (item.decided_at || rec.decided_at || null) : null
        });
      });
    });
    return rows;
  }

  /* Pure: decided rows (approved or denied) first, newest decided time first
     (ISO strings sort as text, undated last); undecided rows after them, in
     input order. */
  function sortProductionLog(rows) {
    var indexed = (rows || []).map(function (row, index) {
      return {row: row, index: index, decided: row.decision === "approved" || row.decision === "denied"};
    });
    indexed.sort(function (a, b) {
      if (a.decided !== b.decided) return a.decided ? -1 : 1;
      var at = a.row.at || "", bt = b.row.at || "";
      if (at !== bt) return at < bt ? 1 : -1;
      return a.index - b.index;
    });
    return indexed.map(function (entry) { return entry.row; });
  }

  /* Pure: glyph plus word for the decision. Undecided steps read as logged,
     not waiting. */
  function decisionWord(decision) {
    if (decision === "approved") return {cls: "dc-pill ok", text: "✓ Approved"};
    if (decision === "denied") return {cls: "dc-pill no", text: "✗ Denied"};
    return {cls: "dc-pill other", text: "○ Logged, no decision"};
  }

  function logRows() {
    return sortProductionLog(productionLogRows(state.approvals));
  }

  function productionLogSpec() {
    var rows = logRows();
    if (!rows.length) return null;
    return ["production-log", keyOf([state.approvals, readDisclosed(PL_SEC), state.view]), productionLogCard, "margin-top:14px"];
  }

  function logLine(row) {
    var tr = el("tr", {class: "pl-row"});
    var stepCell = el("td", {class: "c-text"});
    stepCell.appendChild(el("span", {class: "dc-ws", text: row.ws || "-"}));
    stepCell.appendChild(document.createTextNode(" " + row.step));
    tr.appendChild(stepCell);
    tr.appendChild(el("td", {class: "c-text", text: row.decidedBy || "—"}));
    var pill = decisionWord(row.decision);
    var decisionCell = el("td", {class: "c-text"});
    decisionCell.appendChild(el("span", {class: pill.cls, text: pill.text, title: row.decision}));
    tr.appendChild(decisionCell);
    tr.appendChild(el("td", {class: "c-text", text: row.rollback || "not recorded", title: row.rollback || ""}));
    tr.appendChild(el("td", {class: "c-text", text: row.at || "—"}));
    return tr;
  }

  function productionLogCard() {
    var rows = logRows();
    var c = el("div", {class: "card dc-card", id: "exec-production-log", "data-nav": "Production decisions"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "Production decisions"}));
    head.appendChild(el("p", {class: "sub", text: "Read-only log of production steps the leads decided and logged, newest decided first."}));
    c.appendChild(head);
    var info = discloseRows(rows, "production-log", "production decisions");
    var table = el("table", {class: "pl-table", id: "pl-table", "aria-label": "Production decisions"});
    var head2 = el("tr");
    ["Step", "Decided by", "Decision", "Rollback", "Time"].forEach(function (label) {
      head2.appendChild(el("th", {scope: "col", class: "c-text", text: label}));
    });
    table.appendChild(el("thead", {}, [head2]));
    var body = el("tbody", {});
    info.rows.forEach(function (row) { body.appendChild(logLine(row)); });
    table.appendChild(body);
    c.appendChild(table);
    var more = discloseButton(info, "pl-table", discloseRedraw(PL_SEC, "pl-table"));
    if (more) {
      var acts = el("div", {class: "dc-acts"});
      acts.appendChild(more);
      c.appendChild(acts);
    }
    return c;
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.productionLog = productionLogSpec;
  global.productionLogRows = productionLogRows;
  global.sortProductionLog = sortProductionLog;
  global.productionLogSpec = productionLogSpec;
  global.productionLogCard = productionLogCard;
})(window);
