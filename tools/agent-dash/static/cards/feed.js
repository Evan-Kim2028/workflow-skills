/* Page card: What changed (project feed).
   Day-grouped newest-first lines from GET /api/project/feed (history task
   and state moves, decided approvals with dates, archive/restore moves,
   first agent runs, per-workstream-per-hour finish summaries). Registered
   once (scope "project", slot "exec-feed" in registry.js) where it mounts
   inside the home slot; the slot stays an empty container until then.
   The load resolves after the first paint: while loading the card reads
   "Loading", on failure it reads "request failed" with a retry, and a
   background refresh keeps the old lines until the new ones land. The
   footer names the oldest recorded stamp ("No history before ...") so a
   pre-history gap shows honestly instead of filling in.
   Reads: state.feed, state.feedError, state.feedSeq, state.seq, state.view.
   Exports (on window.AdPage.cards, also kept as plain globals): feedSpec,
   feedCard, dayLabel.
   Needs: el, fmtT, fmtDate, fmtN, api, keyOf, openPlan, drawMain,
   secCache, state (plain globals). */
(function (global) {
  "use strict";
  var FEED_URL = "/api/project/feed";
  var FEED_SEC = "exec-feed";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var feedLoading = false;

  /* Pure: "2026-10-17" to "Oct 17" with a fixed month table (no locale
     number formatting, so the compact-number rule never fires). */
  function dayLabel(day) {
    var parts = String(day == null ? "" : day).split("-");
    if (parts.length !== 3) return String(day == null ? "" : day);
    var month = +parts[1], date = +parts[2];
    if (!(month >= 1 && month <= 12) || !(date >= 1 && date <= 31)) return String(day);
    return MONTHS[month - 1] + " " + date;
  }

  /* Pure: UTC "YYYY-MM-DD" for one epoch stamp (the footer gap line). */
  function dayOf(ts) {
    try {
      return new Date(ts * 1000).toISOString().slice(0, 10);
    } catch (e) {
      return "";
    }
  }

  function redrawFeed() {
    try {
      if (typeof secCache !== "undefined" && secCache) delete secCache[FEED_SEC];
    } catch (e) {}
    if (state.view === "project" && typeof drawMain === "function") drawMain();
  }

  function loadFeed() {
    if (feedLoading) return;
    feedLoading = true;
    state.feedSeq = state.seq;
    api(FEED_URL, {}).then(function (j) {
      feedLoading = false;
      state.feedError = null;
      state.feed = (j && typeof j === "object") ? j : null;
      state.feedSeq = state.seq;
      redrawFeed();
    }).catch(function () {
      feedLoading = false;
      state.feedError = "request failed";
      redrawFeed();
    });
  }

  function ensureFeed() {
    if (feedLoading) return;
    if (!state.feed || state.feedError || state.feedSeq !== state.seq) loadFeed();
  }

  function feedLine(item) {
    var li = el("li", {class: "fd-line"});
    var ts = +item.ts || 0;
    li.appendChild(el("span", {class: "fd-t", text: ts ? fmtT(ts) : "--", title: ts ? fmtDate(ts) : "no recorded time"}));
    if (item.ws && item.target) {
      var chip = el("button", {type: "button", class: "fd-ws", title: (item.name ? item.name + " " : "") + "(" + item.ws + ") — open the plan"});
      chip.textContent = item.ws;
      (function (target) {
        chip.addEventListener("click", function () { openPlan(target); });
      })(item.target);
      li.appendChild(chip);
    }
    li.appendChild(el("span", {class: "fd-x", text: item.text || ""}));
    return li;
  }

  /* Pure shape; the grouped list node. Consecutive same-day lines share
     one day header; every line carries its exact stamp on hover. */
  function feedCard() {
    var c = el("div", {class: "card fd-card", id: "exec-feed-card", "data-nav": "What changed"});
    var head = el("div", {class: "pj-head"});
    head.appendChild(el("h2", {text: "What changed"}));
    var body = state.feed || {};
    var items = Array.isArray(body.feed) ? body.feed : [];
    var total = +body.total || items.length;
    head.appendChild(el("p", {class: "sub", text: items.length && total > items.length ? "Newest " + fmtN(items.length) + " of " + fmtN(total) : "Newest first, from recorded history."}));
    c.appendChild(head);
    if (state.feedError && !items.length) {
      var err = el("p", {class: "pj-note", text: "The feed request failed — showing nothing rather than stale lines. "});
      var retry = el("button", {type: "button", text: "Retry"});
      retry.addEventListener("click", function () { state.feedError = null; state.feed = null; loadFeed(); });
      err.appendChild(retry);
      c.appendChild(err);
      return c;
    }
    if (!items.length) {
      c.appendChild(el("p", {class: "pj-note", text: feedLoading ? "Loading the feed…" : "No recorded changes yet."}));
      return c;
    }
    var list = el("ol", {class: "fd-list"});
    var lastDay = null;
    items.forEach(function (item) {
      var day = item.day || dayOf(item.ts);
      if (day !== lastDay) {
        lastDay = day;
        list.appendChild(el("li", {class: "fd-day", text: dayLabel(day)}));
      }
      list.appendChild(feedLine(item));
    });
    c.appendChild(list);
    if (body.earliest_ts != null && isFinite(+body.earliest_ts)) {
      c.appendChild(el("p", {class: "pj-note", text: "No history before " + dayLabel(dayOf(+body.earliest_ts)) + "."}));
    }
    return c;
  }

  function feedSpec(passedState, snap) {
    ensureFeed();
    return ["feed", keyOf([state.feed, state.feedError, state.seq, state.view]), feedCard, ""];
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.feed = feedSpec;
  global.feedSpec = feedSpec;
  global.feedCard = feedCard;
  global.dayLabel = dayLabel;
})(window);
