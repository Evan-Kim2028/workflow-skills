/* Page core: one shared pager for every finished agents/runs list.
   The Finished (24 h) tables (Execution overall plus each workstream tab)
   and the Recently done shelf (Project home) all render this footer instead
   of their own paging: a "Showing 11-20 of 134" label, real Prev/Next
   buttons (disabled at the ends, aria-labels naming the list), numbered
   page buttons with ellipsis past 7 pages (first, last, current +-1), and
   a rows-per-page selector (10 default, 25, 50) persisted per list in
   sessionStorage behind try/catch. The footer hides when the list fits on
   one page (10 or fewer rows at the default size). Pages stay put across
   the 2-5 s ticks: the state lives outside the render, so new rows shift
   into page 1 as usual while a later page keeps its page; a new scope
   starts on page 1 and a shrunken total clamps down.
   Reads: window.sessionStorage (guarded); state, since, api, secCache,
   drawMain as plain globals at call time only.
   Exports (on window.AdPage, also kept as plain globals): DEFAULT_PAGE_SIZE,
   PAGE_SIZES, pgCount, pgClamp, pgSlice, pgWindow, pgRange, pgLabel,
   pagerKey, pagerState, pagerPage, readPageSize, writePageSize, resetPager,
   renderPager, redrawSection, finishedTotal, resolveFinished.
   Needs: el (plain global, call time only). */
(function (global) {
  "use strict";
  var DEFAULT_PAGE_SIZE = 10;
  var PAGE_SIZES = [10, 25, 50];
  /* The snapshot's finished window: the client holds this many newest rows,
     so a page past it fetches its own window from the server instead. */
  var HELD_WINDOW = 50;

  /* Pure: how many pages a total needs at this size (at least 1, so an
     empty list still labels "Showing 0 of 0" instead of dividing by zero). */
  function pgCount(total, size) {
    if (!(size > 0)) size = DEFAULT_PAGE_SIZE;
    return Math.max(1, Math.ceil(Math.max(0, total || 0) / size));
  }
  /* Pure: keep a page inside the list (min 1, max last page). A total that
     shrank under the current page lands on the new last page. */
  function pgClamp(page, total, size) {
    var pages = pgCount(total, size);
    if (!(page > 0)) return 1;
    return Math.min(Math.floor(page), pages);
  }
  /* Pure: the held rows for one page (newest first, like the server sends
     them). Out-of-range pages clamp instead of emptying. */
  function pgSlice(rows, page, size) {
    rows = rows || [];
    if (!(size > 0)) size = DEFAULT_PAGE_SIZE;
    var p = pgClamp(page, rows.length, size);
    return rows.slice((p - 1) * size, p * size);
  }
  /* Pure: the numbered buttons for many pages: first, last, current +-1,
     with "…" where numbers are skipped. Seven or fewer pages show every
     number, never an ellipsis. */
  function pgWindow(cur, pages) {
    pages = Math.max(1, Math.floor(pages || 1));
    cur = Math.min(pages, Math.max(1, Math.floor(cur || 1)));
    var keep = [];
    var want = {};
    [1, pages, cur - 1, cur, cur + 1].forEach(function (n) {
      if (n >= 1 && n <= pages && !want[n]) { want[n] = 1; keep.push(n); }
    });
    keep.sort(function (a, b) { return a - b; });
    if (pages <= 7 && keep.length === pages) return keep;
    if (pages <= 7) {
      var all = [];
      for (var i = 1; i <= pages; i++) all.push(i);
      return all;
    }
    var out = [];
    keep.forEach(function (n, i) {
      if (i && n - keep[i - 1] > 1) out.push("…");
      out.push(n);
    });
    return out;
  }
  /* Pure: the 1-based row span one page shows ("Showing {from}-{to} of
     {total}"); an empty list spans 0-0. */
  function pgRange(page, size, total) {
    total = Math.max(0, total || 0);
    if (!total) return {from: 0, to: 0};
    if (!(size > 0)) size = DEFAULT_PAGE_SIZE;
    var p = pgClamp(page, total, size);
    return {from: (p - 1) * size + 1, to: Math.min(p * size, total)};
  }
  /* Pure: the footer label text. Row spans name rows, never bare counts:
     "Showing 11-20 of 134". */
  function pgLabel(page, size, total) {
    var r = pgRange(page, size, total);
    return "Showing " + r.from + "-" + r.to + " of " + Math.max(0, total || 0);
  }

  /* Pager state lives outside the render (module memory keyed by scope and
     list), so ticks rebuild the section without moving the page. A scope
     change is a new key, which starts on page 1; only the size persists
     across loads, per list, guarded for private mode. */
  var _mem = {};
  function pagerKey(listId, scope) { return (scope || "all") + "|" + listId; }
  function readPageSize(listId) {
    try {
      var v = parseInt(window.sessionStorage.getItem("agent-dash-pagesize:" + listId), 10);
      if (PAGE_SIZES.indexOf(v) >= 0) return v;
    } catch (e) {}
    return DEFAULT_PAGE_SIZE;
  }
  function writePageSize(listId, size) {
    try {
      if (PAGE_SIZES.indexOf(size) >= 0) window.sessionStorage.setItem("agent-dash-pagesize:" + listId, String(size));
    } catch (e) {}
  }
  function pagerState(listId, scope) {
    var k = pagerKey(listId, scope);
    if (!_mem[k]) _mem[k] = {page: 1, size: readPageSize(listId)};
    if (PAGE_SIZES.indexOf(_mem[k].size) < 0) _mem[k].size = DEFAULT_PAGE_SIZE;
    if (!(_mem[k].page > 0)) _mem[k].page = 1;
    return _mem[k];
  }
  /* The clamped current page for a total (mutates the state when the total
     shrank). Spec keys and renders read this, never the raw field. */
  function pagerPage(listId, scope, total) {
    var ps = pagerState(listId, scope);
    ps.page = pgClamp(ps.page, total, ps.size);
    return ps.page;
  }
  function resetPager(listId, scope) {
    _mem[pagerKey(listId, scope)] = {page: 1, size: readPageSize(listId)};
  }

  /* The footer row. Hidden (null) when the list fits on one page. Every
     control is a real button or select, keyboard-focusable by default;
     data-pg marks each control so a redraw can hand focus back. */
  function renderPager(listId, listName, scope, total, onChange) {
    var ps = pagerState(listId, scope);
    var pages = pgCount(total, ps.size);
    if (pages <= 1) return null;
    var foot = el("div", {class: "pg-foot", "data-pg-footer": listId});
    foot.appendChild(el("span", {class: "pg-label", text: pgLabel(ps.page, ps.size, total)}));
    var nav = el("div", {class: "pg-pages", role: "group", "aria-label": "Pages for " + listName});
    var fire = function () { if (typeof onChange === "function") onChange(); };
    var prev = el("button", {type: "button", class: "pg-btn", "data-pg": "prev", "aria-label": "Previous page of " + listName, text: "‹ Prev"});
    if (ps.page <= 1) prev.setAttribute("disabled", "");
    prev.addEventListener("click", function () {
      ps.page = pgClamp(ps.page - 1, total, ps.size);
      fire();
    });
    nav.appendChild(prev);
    pgWindow(ps.page, pages).forEach(function (n) {
      if (n === "…") {
        nav.appendChild(el("span", {class: "pg-gap", "aria-hidden": "true", text: "…"}));
        return;
      }
      var b = el("button", {type: "button", class: "pg-btn pg-num", "data-pg": "page-" + n, "aria-label": "Page " + n + " of " + listName, text: String(n)});
      if (n === ps.page) b.setAttribute("aria-current", "page");
      (function (want) {
        b.addEventListener("click", function () {
          ps.page = pgClamp(want, total, ps.size);
          fire();
        });
      })(n);
      nav.appendChild(b);
    });
    var next = el("button", {type: "button", class: "pg-btn", "data-pg": "next", "aria-label": "Next page of " + listName, text: "Next ›"});
    if (ps.page >= pages) next.setAttribute("disabled", "");
    next.addEventListener("click", function () {
      ps.page = pgClamp(ps.page + 1, total, ps.size);
      fire();
    });
    nav.appendChild(next);
    foot.appendChild(nav);
    var lab = el("label", {class: "pg-size"});
    lab.appendChild(document.createTextNode("Rows per page "));
    var sel = el("select", {"data-pg": "size", "aria-label": "Rows per page for " + listName});
    PAGE_SIZES.forEach(function (n) {
      var opt = el("option", {value: String(n), text: String(n)});
      if (n === ps.size) opt.setAttribute("selected", "");
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () {
      var v = parseInt(sel.value, 10);
      if (PAGE_SIZES.indexOf(v) < 0) return;
      ps.size = v;
      writePageSize(listId, v);
      ps.page = pgClamp(ps.page, total, ps.size);
      fire();
    });
    lab.appendChild(sel);
    foot.appendChild(lab);
    return foot;
  }

  /* Section redraw after a pager click: rebuilds only that section (the key
     carries page and size) and hands keyboard focus back to the same
     pager control. Each global is touched inside try/catch so the pure
     helpers stay node-runnable. */
  function redrawSection(section) {
    return function () {
      var active = null;
      try {
        var ae = document.activeElement;
        var f = ae && ae.closest ? ae.closest("[data-pg-footer]") : null;
        if (f && ae.getAttribute) active = {list: f.getAttribute("data-pg-footer"), ctl: ae.getAttribute("data-pg")};
      } catch (e) {}
      try { delete secCache[section]; } catch (e) {}
      try { drawMain(); } catch (e) {}
      if (active && active.ctl) {
        try {
          var next = document.querySelector('[data-pg-footer="' + active.list + '"] [data-pg="' + active.ctl + '"]');
          if (next && !next.disabled && next.focus) next.focus();
        } catch (e) {}
      }
    };
  }

  /* The true finished total is a server number: recent_total first, then
     the windowed count, then the held rows. */
  function finishedTotal(snap) {
    var held = (snap && snap.recent) || [];
    if (snap && snap.recent_total != null) return Math.max(0, snap.recent_total);
    if (snap && snap.counts && snap.counts.finished_in_range != null) return Math.max(0, snap.counts.finished_in_range);
    return held.length;
  }

  /* One page of finished rows for a scope: a held slice while the page sits
     inside the snapshot's newest-50 window, else a server page fetched with
     recent_offset/recent_limit (small, conditional-GET). The fetch resolves
     into a cache and redraws only when the rows actually changed, so ticks
     never storm. Callers key their section on rows, total, page and size. */
  var _pages = {};
  var _inflight = {};
  function pageCacheKey(scope, page, size) { return pagerKey("finished", scope) + "|" + page + "x" + size; }
  function fetchFinishedPage(scope, page, size, total, seq) {
    var ck = pageCacheKey(scope, page, size);
    if (_inflight[ck]) return;
    _inflight[ck] = true;
    var params = {recent_limit: size, recent_offset: (page - 1) * size};
    try { params.since = since(); } catch (e) {}
    try { if (scope && scope !== "all") params.ws = scope; } catch (e) {}
    var done = function () { _inflight[ck] = false; };
    try {
      api("/api/snapshot", params).then(function (j) {
        done();
        if (!j || !j.recent) return;
        var cur = pagerState("finished", scope);
        if (cur.page !== page || cur.size !== size) return;
        var rows = j.recent || [];
        var tot = (j.recent_total != null) ? j.recent_total : total;
        var ids = rows.map(function (r) { return r && r.run_id; }).join(",");
        var prev = _pages[ck];
        _pages[ck] = {rows: rows, total: tot, seq: seq};
        var same = prev && prev.total === tot &&
          prev.rows.map(function (r) { return r && r.run_id; }).join(",") === ids;
        if (!same) {
          try { delete secCache.finished; } catch (e) {}
          try { drawMain(); } catch (e) {}
        }
      }).catch(done);
    } catch (e) { done(); }
  }
  function resolveFinished(snap, scope) {
    var held = (snap && snap.recent) || [];
    var total = finishedTotal(snap);
    var ps = pagerState("finished", scope);
    ps.page = pgClamp(ps.page, total, ps.size);
    if ((ps.page - 1) * ps.size < Math.min(held.length, HELD_WINDOW) || held.length >= total) {
      return {rows: pgSlice(held, ps.page, ps.size), total: total, page: ps.page, size: ps.size, pending: false};
    }
    /* A parked server page refetches only when the store wrote (data_seq
       moves on writes alone, never on poll clocks), rendering the cached
       rows meanwhile so ticks never flash. */
    var seq = snap && snap.data_seq;
    var hit = _pages[pageCacheKey(scope, ps.page, ps.size)];
    if (!hit || hit.seq !== seq) fetchFinishedPage(scope, ps.page, ps.size, total, seq);
    if (hit) return {rows: hit.rows, total: total, page: ps.page, size: ps.size, pending: false};
    return {rows: [], total: total, page: ps.page, size: ps.size, pending: true};
  }

  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.paginate = {
    DEFAULT_PAGE_SIZE: DEFAULT_PAGE_SIZE, PAGE_SIZES: PAGE_SIZES,
    pgCount: pgCount, pgClamp: pgClamp, pgSlice: pgSlice,
    pgWindow: pgWindow, pgRange: pgRange, pgLabel: pgLabel,
    pagerKey: pagerKey, pagerState: pagerState, pagerPage: pagerPage,
    readPageSize: readPageSize, writePageSize: writePageSize, resetPager: resetPager,
    renderPager: renderPager, redrawSection: redrawSection,
    finishedTotal: finishedTotal, resolveFinished: resolveFinished
  };
  global.DEFAULT_PAGE_SIZE = DEFAULT_PAGE_SIZE;
  global.PAGE_SIZES = PAGE_SIZES;
  global.pgCount = pgCount;
  global.pgClamp = pgClamp;
  global.pgSlice = pgSlice;
  global.pgWindow = pgWindow;
  global.pgRange = pgRange;
  global.pgLabel = pgLabel;
  global.pagerKey = pagerKey;
  global.pagerState = pagerState;
  global.pagerPage = pagerPage;
  global.readPageSize = readPageSize;
  global.writePageSize = writePageSize;
  global.resetPager = resetPager;
  global.renderPager = renderPager;
  global.redrawSection = redrawSection;
  global.finishedTotal = finishedTotal;
  global.resolveFinished = resolveFinished;
})(window);
