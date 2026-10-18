/* Page core: the workstream switcher that replaces the tab strip.
   The pinned bar stays calm: project name, Overall, the selected
   workstream as a chip (only when one is selected), a single
   "Workstreams (N)" button that opens a searchable popover, up to three
   pinned chips, Sections, Theme. No horizontal tab strip, no
   drag-reorder handles, no tab filter. (The old viewer-local tab order
   only ever ordered that strip, so it retires with it; the switcher
   lists workstreams grouped by state in workstream order.)
   The popover groups rows by state (Active, Paused, Planned, Done, then
   a collapsed Archived group), each row with a state glyph plus word,
   progress percent, live-agent dot, a pin star, and the primary quick
   action on hover and focus. "/" opens it from anywhere, arrow keys move,
   Enter picks, Escape closes. A Keys button in the footer documents every
   shortcut. Pins persist viewer-local (localStorage, try/catch, none by
   default). On phones the popover becomes a full-width sheet.
   Exports (on window.AdPage.switcher, also kept as plain globals):
   renderBar, openSwitcher, closeSwitcher, toggleSwitcher, pickWs,
   switcherRows, stateGlyph.
   Needs: el, state, ADT, selectTab, selectView, isProjectView, fmtN,
   progressFor (plain globals at call time); AdManage.primaryFor for the
   per-row quick action when loaded. */
(function (global) {
  "use strict";
  var PIN_KEY = "agent-dash-pins";
  var GROUP_ORDER = ["active", "paused", "planned", "done", "archived"];
  var GROUP_TITLES = {active: "Active", paused: "Paused", planned: "Planned", done: "Done", archived: "Archived"};

  /* State glyph plus word, never color alone. Matches the page pill words. */
  function stateGlyph(st) {
    if (st === "paused") return "\u23F8 Paused";
    if (st === "planned") return "\u25CB Planned";
    if (st === "done") return "\u2713 Done";
    if (st === "archived") return "\u2713 Archived";
    if (st === "dropped") return "\u2715 Dropped";
    return "\u25CF Active";
  }

  function loadPins() {
    try {
      if (typeof localStorage === "undefined") return [];
      var raw = localStorage.getItem(PIN_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.length) return [];
      var out = [];
      for (var i = 0; i < parsed.length && out.length < 3; i++) {
        var id = String(parsed[i] == null ? "" : parsed[i]).toUpperCase();
        if (id && out.indexOf(id) < 0) out.push(id);
      }
      return out;
    } catch (e) {
      return [];
    }
  }
  function savePins(pins) {
    try {
      if (typeof localStorage === "undefined") return;
      localStorage.setItem(PIN_KEY, JSON.stringify(pins.slice(0, 3)));
    } catch (e) {}
  }
  function togglePin(id) {
    var pins = loadPins();
    var at = pins.indexOf(String(id).toUpperCase());
    if (at >= 0) pins.splice(at, 1);
    else {
      if (pins.length >= 3) return pins;
      pins.push(String(id).toUpperCase());
    }
    savePins(pins);
    return pins;
  }

  /* Every switchable row: the all-states list first (so archived rows
     resolve to their real state), then bundle-only ids, skipped program
     bundles and OVERVIEW. Dropped rows stay out of the switcher: they
     left every view. */
  function switcherRows() {
    var seen = {}, rows = [];
    var all = (state.wssAll && state.wssAll.length ? state.wssAll : state.wss) || [];
    all.forEach(function (w) {
      var key = String(w.ws).toUpperCase();
      if (seen[key] || key === "UNASSIGNED") return;
      seen[key] = 1;
      rows.push({ws: w.ws, name: titleOf(w.ws, w.name), progress: w.progress || null,
        state: ADT.wsStateOf(w), live: w.live_agents || 0,
        allowed: w.allowed_actions || []});
    });
    (state.bundles || []).forEach(function (b) {
      if (!b || b.kind === "program" || b.id === "OVERVIEW") return;
      var key = String(b.id).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      rows.push({ws: b.id, name: b.title || "", progress: (typeof progressFor === "function" ? progressFor(b.id) : null),
        state: b.state || "active", live: 0, allowed: []});
    });
    rows = rows.filter(function (r) { return r.state !== "dropped"; });
    return rows;
  }
  function openCount() {
    return switcherRows().filter(function (r) { return r.state !== "archived"; }).length;
  }

  /* Workstream order, the same number order the old tabs used:
     numeric when both ids carry a number, lexical otherwise. */
  function wsOrder(a, b) {
    var ma = /(\d+)/.exec(a.ws || ""), mb = /(\d+)/.exec(b.ws || "");
    if (ma && mb && ma[1] !== mb[1]) return (+ma[1]) - (+mb[1]);
    return String(a.ws) < String(b.ws) ? -1 : String(a.ws) > String(b.ws) ? 1 : 0;
  }

  function rowLabel(r) {
    return r.ws + (r.name ? " " + r.name : "");
  }
  /* Run-attributed rows carry no board name; the bundle title is the
     honest fallback so every row names its workstream. */
  function titleOf(ws, name) {
    if (name) return name;
    var hit = null;
    (state.bundles || []).forEach(function (b) {
      if (String(b.id).toUpperCase() === String(ws).toUpperCase()) hit = b.title || "";
    });
    return hit || "";
  }
  function pctOf(r) {
    return r.progress && typeof r.progress.pct === "number" ? r.progress.pct : null;
  }

  /* Pick a row: archived rows open the Archive view, planned rows the
     Planned view, the rest their execution tab. */
  function pickWs(id, st) {
    closeSwitcher();
    var up = String(id).toUpperCase();
    var found = null;
    switcherRows().forEach(function (r) { if (String(r.ws).toUpperCase() === up) found = r; });
    var want = (st || (found && found.state) || "active");
    if (want === "archived") {
      if (state.view !== "archive") selectView("archive");
    } else if (want === "planned") {
      if (state.view !== "planned") selectView("planned");
    } else {
      if (typeof isProjectView === "function" && isProjectView()) selectView("exec");
      selectTab(up);
    }
  }

  /* The pinned bar chrome. Kept under seven elements: project name,
     Overall, the selected chip (only with a selection), the switcher
     button, pinned chips, Sections, Theme. */
  function renderBar() {
    var overall = document.getElementById("overall-btn");
    if (overall) {
      if (state.view === "exec" && state.ws === "all") overall.setAttribute("aria-current", "page");
      else overall.removeAttribute("aria-current");
    }
    var sel = document.getElementById("selected-chip");
    if (sel) {
      sel.innerHTML = "";
      if (state.ws && state.ws !== "all") {
        var hit = null;
        switcherRows().forEach(function (r) { if (String(r.ws).toUpperCase() === String(state.ws).toUpperCase()) hit = r; });
        var label = state.ws + (hit && hit.name ? " \u00B7 " + hit.name : "");
        var b = el("button", {type: "button", class: "sel-chip", title: "Selected workstream — open the switcher",
          "aria-label": "Selected workstream " + label + " — open the workstream switcher"});
        b.appendChild(el("span", {class: "sel-id", text: state.ws}));
        if (hit && hit.name) b.appendChild(el("span", {class: "sel-name", text: hit.name}));
        /* The completed tick reuses the shared rule, as the old tabs did. */
        if (hit && typeof ADT !== "undefined" && ADT.isCompleted(hit.progress)) {
          var tick = el("span", {class: "tick", text: "\u2713", title: "Complete"});
          b.appendChild(tick);
        }
        b.addEventListener("click", function () { toggleSwitcher(); });
        sel.appendChild(b);
        sel.hidden = false;
      } else {
        sel.hidden = true;
      }
    }
    var sw = document.getElementById("ws-switcher");
    if (sw) {
      var n = openCount();
      sw.textContent = "Workstreams (" + fmtN(n) + ")";
      sw.setAttribute("aria-label", "Switch workstream, " + n + " open workstreams. Shortcut: slash.");
    }
    renderPins();
  }

  function renderPins() {
    var host = document.getElementById("ws-pins");
    if (!host) return;
    host.innerHTML = "";
    var pins = loadPins();
    if (!pins.length) { host.hidden = true; return; }
    host.hidden = false;
    var byId = {};
    switcherRows().forEach(function (r) { byId[String(r.ws).toUpperCase()] = r; });
    pins.forEach(function (id) {
      var r = byId[id];
      if (!r) return;
      var b = el("button", {type: "button", class: "pin-chip" + (String(state.ws).toUpperCase() === id ? " cur" : ""),
        title: rowLabel(r), "aria-label": "Pinned workstream " + rowLabel(r)});
      b.appendChild(el("span", {class: "pin-star", text: "\u2605"}));
      b.appendChild(el("span", {class: "pin-id", text: r.ws}));
      if (r.live) b.appendChild(el("span", {class: "pin-live", text: "\u25CF" + r.live}));
      b.addEventListener("click", function () { pickWs(r.ws, r.state); });
      host.appendChild(b);
    });
  }

  var pop = null, popInput = null, popList = null, activeIdx = -1, visibleBtns = [];
  var returnFocus = null;

  function isOpen() { return !!(pop && pop.parentNode); }

  function openSwitcher() {
    if (isOpen()) { if (popInput) popInput.focus(); return; }
    var anchor = document.getElementById("ws-switcher");
    returnFocus = anchor;
    pop = el("div", {id: "ws-switcher-pop", class: "ws-pop", role: "dialog", "aria-label": "Switch workstream"});
    var search = el("input", {type: "search", placeholder: "Filter by name or id\u2026", "aria-label": "Filter workstreams by name or id"});
    popInput = search;
    search.addEventListener("input", function () { paintList(search.value); });
    search.addEventListener("keydown", onPopKey);
    pop.appendChild(search);
    popList = el("div", {class: "ws-pop-list", role: "listbox", "aria-label": "Workstreams by state", tabindex: "-1"});
    popList.addEventListener("keydown", onPopKey);
    pop.appendChild(popList);
    var foot = el("div", {class: "ws-pop-foot"});
    var keys = el("button", {type: "button", class: "ws-keys-btn", text: "Keys", title: "Keyboard shortcuts"});
    keys.setAttribute("aria-label", "Keyboard shortcuts");
    keys.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (global.AdManage && typeof global.AdManage.keysPop === "function") global.AdManage.keysPop(keys);
    });
    foot.appendChild(keys);
    foot.appendChild(el("span", {class: "ws-pop-hint", text: "/ opens \u00B7 \u2191\u2193 move \u00B7 Enter picks \u00B7 Esc closes"}));
    pop.appendChild(foot);
    document.body.appendChild(pop);
    if (anchor) anchor.setAttribute("aria-expanded", "true");
    try {
      var r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
      if (r && window.innerWidth > 700) {
        pop.style.left = Math.max(8, Math.min(window.innerWidth - 496, r.left + (window.scrollX || 0))) + "px";
        pop.style.top = (r.bottom + 6 + (window.scrollY || 0)) + "px";
      }
    } catch (e) {}
    paintList("");
    document.addEventListener("keydown", onDocKey, true);
    document.addEventListener("mousedown", onOutside, true);
    search.focus();
  }

  function closeSwitcher() {
    if (!isOpen()) return;
    if (pop.parentNode) pop.parentNode.removeChild(pop);
    pop = null; popInput = null; popList = null; activeIdx = -1; visibleBtns = [];
    var anchor = document.getElementById("ws-switcher");
    if (anchor) anchor.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onDocKey, true);
    document.removeEventListener("mousedown", onOutside, true);
    if (returnFocus && document.body.contains(returnFocus)) returnFocus.focus();
    returnFocus = null;
  }
  function toggleSwitcher() {
    if (isOpen()) closeSwitcher();
    else openSwitcher();
  }
  function onOutside(ev) {
    if (pop && !pop.contains(ev.target)) closeSwitcher();
  }
  function onDocKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); closeSwitcher(); }
  }

  function quickAction(row) {
    /* The primary quick action per row, from the manage module. In
       read-only mode the descriptor copies the CLI command instead. */
    try {
      var M = global.AdManage;
      if (M && typeof M.primaryFor === "function") {
        var d = M.primaryFor(rowName(row));
        if (d) return {label: d.label, title: d.title,
          run: function (scope) { M.runDescriptor(d, row.ws, rowLabel(row), scope, function () { renderBar(); }); }};
      }
    } catch (e) {}
    return null;
  }
  function rowName(r) {
    return {ws: r.ws, name: r.name, state: r.state, progress: r.progress, allowed_actions: r.allowed};
  }

  function paintList(filter) {
    if (!popList) return;
    popList.innerHTML = "";
    visibleBtns = [];
    activeIdx = -1;
    var q = (filter || "").trim().toLowerCase();
    var rows = switcherRows().filter(function (r) {
      if (!q) return true;
      return String(r.ws).toLowerCase().indexOf(q) >= 0 || String(r.name || "").toLowerCase().indexOf(q) >= 0;
    });
    if (!rows.length) {
      popList.appendChild(el("p", {class: "ws-pop-empty", text: "No workstreams match."}));
      return;
    }
    var pins = loadPins();
    GROUP_ORDER.forEach(function (group) {
      var members = rows.filter(function (r) { return r.state === group; }).sort(wsOrder);
      if (!members.length) return;
      var wrap = el("div", {class: "ws-group"});
      if (group === "archived") {
        var det = el("details", {class: "ws-arch"});
        var sum = el("summary", {});
        sum.textContent = "Archived (" + members.length + ")";
        det.appendChild(sum);
        var box = el("div", {class: "ws-group-rows"});
        members.forEach(function (r) { box.appendChild(rowBtn(r, pins)); });
        det.appendChild(box);
        wrap.appendChild(det);
      } else {
        var head = el("div", {class: "ws-group-head"});
        head.textContent = GROUP_TITLES[group] + " (" + members.length + ")";
        wrap.appendChild(head);
        members.forEach(function (r) { wrap.appendChild(rowBtn(r, pins)); });
      }
      popList.appendChild(wrap);
    });
    setActive(0);
  }

  function rowBtn(r, pins) {
    var b = el("button", {type: "button", class: "ws-row", role: "option", "aria-selected": String(String(state.ws).toUpperCase() === String(r.ws).toUpperCase())});
    b.setAttribute("data-ws", r.ws);
    b.setAttribute("aria-label", r.ws + (r.name ? ", " + r.name : "") + ", " + stateGlyph(r.state) + ". Pick to open.");
    var left = el("span", {class: "ws-row-main"});
    left.appendChild(el("span", {class: "ws-state st-" + r.state, text: stateGlyph(r.state)}));
    var nm = el("span", {class: "ws-row-name"});
    nm.appendChild(el("b", {text: r.ws}));
    if (r.name) nm.appendChild(el("span", {text: r.name}));
    left.appendChild(nm);
    var pct = pctOf(r);
    left.appendChild(el("span", {class: "ws-pct", text: pct == null ? "\u2014" : pct + "%"}));
    if (r.live) left.appendChild(el("span", {class: "ws-live", text: "\u25CF " + r.live + " live"}));
    b.appendChild(left);
    var tools = el("span", {class: "ws-row-tools"});
    var qa = quickAction(r);
    if (qa) {
      var q = el("button", {type: "button", class: "ws-qa", text: qa.label, title: qa.title || qa.label});
      q.setAttribute("aria-label", qa.label + " " + rowLabel(r));
      q.addEventListener("click", function (ev) { ev.stopPropagation(); closeSwitcher(); qa.run(tools); });
      tools.appendChild(q);
    }
    var star = el("button", {type: "button", class: "ws-pin" + (pins.indexOf(String(r.ws).toUpperCase()) >= 0 ? " on" : ""),
      text: "\u2605", title: pins.indexOf(String(r.ws).toUpperCase()) >= 0 ? "Unpin " + r.ws : "Pin " + r.ws + " (up to three)"});
    var pinned = pins.indexOf(String(r.ws).toUpperCase()) >= 0;
    star.setAttribute("aria-label", (pinned ? "Unpin " : "Pin ") + rowLabel(r));
    star.setAttribute("aria-pressed", pinned ? "true" : "false");
    if (!pinned && pins.length >= 3) star.disabled = true;
    star.addEventListener("click", function (ev) {
      ev.stopPropagation();
      togglePin(r.ws);
      renderPins();
      if (popInput) paintList(popInput.value);
    });
    tools.appendChild(star);
    b.appendChild(tools);
    b.addEventListener("click", function () { pickWs(r.ws, r.state); });
    visibleBtns.push(b);
    return b;
  }

  function setActive(i) {
    if (!visibleBtns.length) return;
    activeIdx = Math.max(0, Math.min(visibleBtns.length - 1, i));
    visibleBtns.forEach(function (b, k) {
      if (k === activeIdx) { b.classList.add("hot"); try { b.scrollIntoView({block: "nearest"}); } catch (e) {} }
      else b.classList.remove("hot");
    });
    if (popList) popList.setAttribute("aria-activedescendant", visibleBtns[activeIdx].getAttribute("data-ws") || "");
  }
  function onPopKey(ev) {
    if (ev.key === "ArrowDown") { ev.preventDefault(); setActive(activeIdx + 1); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); setActive(activeIdx - 1); }
    else if (ev.key === "Enter") {
      var b = visibleBtns[activeIdx];
      if (b && ev.target !== b) { ev.preventDefault(); b.click(); }
    } else if (ev.key === "Escape") { ev.preventDefault(); closeSwitcher(); }
  }

  /* Global keys: "/" opens the switcher from anywhere except a text
     field; "g" then p/c/l/a jumps to Project, Current, Planned,
     Archive. Installed once. */
  var gAt = 0;
  function onGlobalKey(ev) {
    if (ev.defaultPrevented) return;
    var tag = ev.target && ev.target.tagName;
    var typing = tag === "INPUT" || tag === "TEXTAREA" || (ev.target && ev.target.isContentEditable);
    if (ev.key === "/" && !typing) {
      ev.preventDefault();
      toggleSwitcher();
      return;
    }
    if (typing) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    var now = Date.now();
    if (ev.key === "g" || ev.key === "G") { gAt = now; return; }
    if (now - gAt < 800) {
      var k = String(ev.key).toLowerCase();
      if (k === "p") { gAt = 0; selectView("project"); }
      else if (k === "c") { gAt = 0; selectView("exec"); }
      else if (k === "l") { gAt = 0; selectView("planned"); }
      else if (k === "a") { gAt = 0; selectView("archive"); }
    }
  }
  if (!global.__adSwitcherKeys) {
    global.__adSwitcherKeys = true;
    document.addEventListener("keydown", onGlobalKey);
  }

  var AdSw = (global.AdPage = global.AdPage || {});
  AdSw.switcher = AdSw.switcher || {};
  AdSw.switcher.renderBar = renderBar;
  AdSw.switcher.openSwitcher = openSwitcher;
  AdSw.switcher.closeSwitcher = closeSwitcher;
  AdSw.switcher.toggleSwitcher = toggleSwitcher;
  AdSw.switcher.pickWs = pickWs;
  AdSw.switcher.switcherRows = switcherRows;
  AdSw.switcher.stateGlyph = stateGlyph;
  global.AdSwitcher = AdSw.switcher;
  global.renderBar = renderBar;
  global.openSwitcher = openSwitcher;
  global.closeSwitcher = closeSwitcher;
  global.toggleSwitcher = toggleSwitcher;
  global.pickWs = pickWs;
  global.switcherRows = switcherRows;
  global.stateGlyph = stateGlyph;
})(window);
