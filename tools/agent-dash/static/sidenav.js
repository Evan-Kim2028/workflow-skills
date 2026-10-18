(function () {
  "use strict";

  var hot = document.createElement("div");
  hot.className = "sidenav-hot";
  hot.id = "sidenav-hot";
  hot.setAttribute("aria-hidden", "true");

  var nav = document.createElement("nav");
  nav.className = "sidenav";
  nav.id = "sidenav";
  nav.setAttribute("aria-label", "Sections");

  var list = document.createElement("div");
  list.className = "sidenav-list";
  var foot = document.createElement("div");
  foot.className = "sidenav-foot";
  nav.appendChild(list);
  nav.appendChild(foot);
  /* Dock modes: compact (dots, expands on hover), pinned (names always), hidden (slim handle). Remembered per browser. */
  var MODES = ["compact", "pinned", "hidden"];
  var mode = "compact";
  try { mode = localStorage.getItem("agent-dash-dock") || "compact"; } catch (e) {}
  if (MODES.indexOf(mode) < 0) mode = "compact";
  var modeBtn = document.createElement("button");
  modeBtn.type = "button";
  modeBtn.className = "dock-mode";
  function applyMode() {
    MODES.forEach(function (m) { document.documentElement.classList.toggle("dock-" + m, m === mode); });
    modeBtn.textContent = mode === "compact" ? "\u21E5" : mode === "pinned" ? "\u21E4" : "\u2630";
    modeBtn.title = mode === "compact" ? "Pin section names" : mode === "pinned" ? "Hide the section dock" : "Show the section dock";
    try { localStorage.setItem("agent-dash-dock", mode); } catch (e) {}
  }
  modeBtn.addEventListener("click", function (ev) { ev.stopPropagation(); mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length]; applyMode(); });
  nav.appendChild(modeBtn);
  applyMode();
  document.body.appendChild(hot);
  document.body.appendChild(nav);

  var btn = document.getElementById("nav-sections");
  var pinned = false;
  var hover = false;
  var suppress = false;
  var items = [];
  var sectionSig = "";
  var tabSig = "";
  var currentId = "";
  var timer = 0;
  var scrollTick = 0;

  function slug(text) {
    var out = String(text || "part").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/g, "");
    return out || "part";
  }

  /* At most four top-level groups; a group's sections show only while you are in it.
     No group names owner calls: the plan tab renders no such section. */
  function planGroup(label) {
    var l = String(label || "").toLowerCase();
    if (l === "where we are" || l === "workstreams") return "Where we are";
    if (l === "why") return "Why";
    if (l === "end state" || l === "end outcome" || l === "plan" || l === "progress" || l === "merge trains") return "Plan";
    return "Reference";
  }
  function execGroup(label) {
    var l = String(label || "").toLowerCase();
    if (l === "where we are" || l === "summary" || l.indexOf("running now") === 0) return "Now";
    if (l.indexOf("plan progress") >= 0) return "Plan progress";
    return "Activity";
  }
  function groupOfId(id) { for (var i = 0; i < items.length; i++) if (items[i].id === id) return items[i].group; return null; }
  var renderedGroup = null;

  function heading(node) {
    var eb = node.querySelector(".eb");
    return String((eb ? eb.textContent : node.textContent) || "").replace(/\s+/g, " ").trim();
  }

  function planOn() {
    var view = document.getElementById("plan-view");
    return !!(view && !view.hidden);
  }

  function viewRoot() {
    return document.getElementById(planOn() ? "plan-body" : "main");
  }

  function nextId(base, label) {
    var part = slug(label);
    var id = base + "-" + part;
    var n = 2;
    while (document.getElementById(id)) {
      id = base + "-" + part + "-" + n;
      n += 1;
    }
    return id;
  }

  function collectPlan(root) {
    var out = [];
    [].forEach.call(root.querySelectorAll("section[id]"), function (section) {
      var h2 = section.querySelector("h2");
      if (!h2) return;
      var label = heading(h2);
      if (!label) return;
      out.push({id: section.id, label: label, depth: 1, group: planGroup(label)});
    });
    return out;
  }

  function collectExec(root) {
    var out = [];
    [].forEach.call(root.querySelectorAll("[data-nav]"), function (node) {
      var label = node.getAttribute("data-nav");
      if (!label) return;
      if (!node.id) node.id = "exec-" + slug(label);
      out.push({id: node.id, label: label, depth: 1, group: execGroup(label)});
    });
    return out;
  }

  function collect() {
    var root = viewRoot();
    if (!root) return [];
    return planOn() ? collectPlan(root) : collectExec(root);
  }

  function switchBits() {
    var parts = [];
    ["view-plan", "view-exec"].forEach(function (id) {
      var node = document.getElementById(id);
      parts.push(id + ":" + (node ? node.getAttribute("aria-pressed") : ""));
    });
    var tabs = document.getElementById("tabs");
    if (tabs) {
      [].forEach.call(tabs.querySelectorAll("button"), function (node) {
        parts.push((node.getAttribute("aria-selected") || "") + ":" + node.textContent);
      });
    }
    return parts.join("|");
  }

  function opened() {
    return pinned || (hover && !suppress);
  }

  function sync() {
    var on = opened();
    nav.classList.toggle("is-open", on);

    if (btn) btn.setAttribute("aria-expanded", on ? "true" : "false");
  }

  function over(x, y) {
    var zone = hot.getBoundingClientRect();
    if (x >= zone.left && x <= zone.right && y >= zone.top && y <= zone.bottom) return true;
    if (!nav.classList.contains("is-open")) return false;
    var box = nav.getBoundingClientRect();
    return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
  }

  function renderList() {
    list.textContent = "";
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "sidenav-empty";
      empty.textContent = "No sections yet.";
      list.appendChild(empty);
      return;
    }
    var cur = groupOfId(currentId) || items[0].group;
    renderedGroup = cur;
    var order = [], byGroup = {};
    items.forEach(function (item) { var g = item.group || item.label; if (!byGroup[g]) { byGroup[g] = []; order.push(g); } byGroup[g].push(item); });
    order.forEach(function (g) {
      var kids = byGroup[g];
      var head = document.createElement("a");
      head.href = "#" + kids[0].id;
      head.className = "is-group" + (g === cur ? " is-active-group" : "");
      head.textContent = g;
      head.title = g;
      head.addEventListener("click", function (ev) { go(kids[0], ev); });
      list.appendChild(head);
      if (g !== cur || kids.length < 2) return;
      kids.forEach(function (item) {
        var link = document.createElement("a");
        link.href = "#" + item.id;
        link.className = "is-sub";
        link.textContent = item.label;
        link.title = item.label;
        link.addEventListener("click", function (ev) { go(item, ev); });
        list.appendChild(link);
      });
    });
  }


  function buttonLabel(node) {
    var parts = [];
    [].forEach.call(node.childNodes, function (kid) {
      var bit = String(kid.textContent || "").replace(/\s+/g, " ").trim();
      if (bit) parts.push(bit);
    });
    return parts.join(" ");
  }

  function fillFoot() {
    foot.textContent = "";
    var views = document.createElement("div");
    views.className = "sidenav-views";
    views.setAttribute("role", "group");
    views.setAttribute("aria-label", "View");
    ["view-plan", "view-exec"].forEach(function (id) {
      var src = document.getElementById(id);
      if (!src) return;
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = buttonLabel(src);
      b.setAttribute("aria-pressed", src.getAttribute("aria-pressed") === "true" ? "true" : "false");
      b.addEventListener("click", function () { src.click(); });
      views.appendChild(b);
    });
    foot.appendChild(views);

    var kicker = document.createElement("div");
    kicker.className = "sidenav-kicker";
    kicker.textContent = "Workstreams";
    foot.appendChild(kicker);

    var tabs = document.createElement("div");
    tabs.className = "sidenav-tabs";
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "Workstreams");
    var srcTabs = document.getElementById("tabs");
    if (srcTabs) {
      [].forEach.call(srcTabs.querySelectorAll("button"), function (src, index) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "sidenav-tab";
        b.textContent = buttonLabel(src);
        b.setAttribute("aria-selected", src.getAttribute("aria-selected") === "true" ? "true" : "false");
        b.addEventListener("click", function () {
          var live = document.getElementById("tabs");
          var hit = live && live.querySelectorAll("button")[index];
          if (hit) hit.click();
        });
        tabs.appendChild(b);
      });
    }
    foot.appendChild(tabs);
  }

  function rebuild() {
    var next = collect();
    var nextSection = next.map(function (item) { return item.depth + ":" + item.id + ":" + item.label; }).join("|");
    var nextTabs = switchBits();
    if (nextSection !== sectionSig) {
      sectionSig = nextSection;
      items = next;
      currentId = "";
      renderList();
    }
    if (nextTabs !== tabSig) {
      tabSig = nextTabs;
      if (false) fillFoot();
    }
    mark();
  }

  function schedule() {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      timer = 0;
      rebuild();
    }, 40);
  }

  function setCurrent(id) {
    var changed = id !== currentId;
    currentId = id || "";
    if (groupOfId(currentId) && groupOfId(currentId) !== renderedGroup) renderList();
    var active = null;
    [].forEach.call(list.querySelectorAll("a"), function (link) {
      if (currentId && link.getAttribute("href") === "#" + currentId) {
        link.setAttribute("aria-current", "location");
        active = link;
      } else link.removeAttribute("aria-current");
    });
    if (!changed || !active) return;
    var box = list.getBoundingClientRect();
    var row = active.getBoundingClientRect();
    if (row.top < box.top) list.scrollTop -= box.top - row.top;
    else if (row.bottom > box.bottom) list.scrollTop += row.bottom - box.bottom;
  }

  function mark() {
    var rows = [];
    items.forEach(function (item) {
      var node = document.getElementById(item.id);
      if (!node || !node.getClientRects().length) return;
      rows.push({id: item.id, top: node.getBoundingClientRect().top});
    });
    if (!rows.length) {
      setCurrent("");
      return;
    }
    var doc = document.documentElement;
    var overflows = doc.scrollHeight > window.innerHeight + 8;
    var atBottom = overflows && window.innerHeight + window.scrollY >= doc.scrollHeight - 8;
    var best = rows[0];
    if (atBottom) best = rows[rows.length - 1];
    else {
      rows.forEach(function (row) {
        if (row.top > 96) return;
        if (best.top > 96 || row.top > best.top + 8) best = row;
      });
    }
    setCurrent(best.id);
  }

  function reveal(node) {
    var parent = node.parentElement;
    while (parent) {
      if (parent.tagName === "DETAILS") parent.open = true;
      parent = parent.parentElement;
    }
  }

  function go(item, ev) {
    var node = document.getElementById(item.id);
    if (!node) return;
    ev.preventDefault();
    reveal(node);
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({behavior: reduce ? "auto" : "smooth", block: "start"});
    var url = new URL(location.href);
    url.hash = item.id;
    var href = url.pathname + url.search + url.hash;
    if (href !== location.pathname + location.search + location.hash) history.replaceState(history.state, "", href);
    setCurrent(item.id);
  }

  function isMouse(ev) {
    return !ev.pointerType || ev.pointerType === "mouse";
  }

  function typing(node) {
    if (!node || !node.tagName) return false;
    var tag = node.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return !!node.isContentEditable;
  }

  function closeNav(fromKey) {
    pinned = false;
    hover = false;
    suppress = true;
    sync();
    if (fromKey && btn && nav.contains(document.activeElement)) btn.focus({preventScroll: true});
  }

  function openNav(fromKey) {
    pinned = true;
    suppress = false;
    sync();
    if (!fromKey) return;
    var first = list.querySelector("a");
    if (first) first.focus({preventScroll: true});
  }

  function toggle(fromKey) {
    if (opened()) closeNav(fromKey);
    else openNav(fromKey);
  }

  if (btn) btn.addEventListener("click", function () { toggle(false); });

  hot.addEventListener("pointerenter", function (ev) {
    if (!isMouse(ev) || suppress) return;
    hover = true;
    sync();
  });

  hot.addEventListener("pointerup", function (ev) {
    if (isMouse(ev)) return;
    if (opened()) closeNav(false);
    else openNav(false);
  });

  document.addEventListener("pointermove", function (ev) {
    if (!isMouse(ev)) return;
    if (over(ev.clientX, ev.clientY)) {
      if (!suppress && !hover) {
        hover = true;
        sync();
      }
      return;
    }
    if (hover || suppress) {
      hover = false;
      suppress = false;
      sync();
    }
  }, {passive: true});

  document.documentElement.addEventListener("pointerleave", function (ev) {
    if (ev.pointerType && ev.pointerType !== "mouse") return;
    hover = false;
    suppress = false;
    sync();
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      if (!opened()) return;
      closeNav(true);
      return;
    }
    if (ev.repeat || ev.key !== "[" || ev.metaKey || ev.ctrlKey || ev.altKey || typing(ev.target)) return;
    ev.preventDefault();
    toggle(true);
  });

  window.addEventListener("scroll", function () {
    if (scrollTick) return;
    scrollTick = window.requestAnimationFrame(function () {
      scrollTick = 0;
      mark();
    });
  }, {passive: true});
  window.addEventListener("hashchange", mark);
  window.addEventListener("resize", schedule);
  window.addEventListener("popstate", schedule);

  var obs = new MutationObserver(schedule);
  ["main", "plan-body", "tabs"].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) obs.observe(node, {childList: true, subtree: true});
  });
  ["exec-view", "plan-view"].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) obs.observe(node, {attributes: true, attributeFilter: ["hidden"]});
  });

  sync();
  schedule();
  try { if (new URLSearchParams(location.search).get("nav") === "open") setTimeout(function () { openNav(false); }, 700); } catch (e) {}
})();
