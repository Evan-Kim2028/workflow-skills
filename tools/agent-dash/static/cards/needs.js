/* Page card: Needs you (approvals).
   Renders pending decisions for this view from state.approvals.
   Currently unreferenced by the registry (registering it would add a
   visible card, a behaviour change); kept here so enabling it is one
   registry line.
   Reads: state.approvals, state.ws.
   Exports (on window.AdPage.cards, also kept as plain globals): needsCard.
   Needs: el, openApprovals, fmtN (plain globals). */
(function (global) {
  "use strict";
  /* layout */
  function needsCard() {
    var list = state.approvals || [];
    var c = el("div", {class: "card needs-you", id: "exec-needs", "data-nav": "Needs you"}, [el("h2", {text: "Needs you"}), el("p", {class: "sub", text: "Pending decisions for this view."})]);
    if (!list.length) { c.appendChild(el("p", {class: "needs-line", text: "No approvals recorded."})); return c; }
    list.forEach(function (block) {
      var items = block.items || [];
      var pending = items.filter(function (item) { return item.status === "pending"; });
      var approved = items.filter(function (item) { return item.status === "approved"; }).length;
      var denied = items.filter(function (item) { return item.status === "denied"; }).length;
      if (!pending.length) {
        var line = block.ws + ": " + fmtN(approved) + " approved";
        if (denied) line += ", " + fmtN(denied) + " denied";
        line += ", " + fmtN(0) + " pending";
        c.appendChild(el("p", {class: "needs-line", text: line}));
        return;
      }
      pending.forEach(function (item) {
        var row = el("div", {class: "needs-row"});
        var href = "?view=plan&ws=" + encodeURIComponent(block.ws) + "#" + String(block.ws).toLowerCase() + "-approvals";
        var label = (state.ws === "all" ? block.ws + " · " : "") + (item.step || item.id);
        var link = el("a", {href: href, text: label});
        link.addEventListener("click", function (ev) { ev.preventDefault(); openApprovals(block.ws); });
        row.appendChild(link);
        row.appendChild(el("span", {class: "ap-badge " + (item.reversible ? "rev" : "oneway"), text: item.reversible ? "reversible" : "one-way"}));
        if (item.conditions) row.appendChild(el("span", {class: "ap-cond", text: item.conditions}));
        c.appendChild(row);
      });
    });
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.needs = needsCard;
  global.needsCard = needsCard;
})(window);
