/* Page core: project header (name in the header and <title>) and the
   project switcher (menu across registry projects).
   Exports (on window.AdPage, also kept as plain globals so the staying
   page script keeps calling every name unchanged): loadProject (fetch
   once per load and on focus) and applyProject (render the header from
   the last payload). The header shows the project name large with a
   muted "/ project tracker" suffix when /api/project has a name;
   otherwise it keeps today's "Agent dashboard" title. applyChrome is
   wrapped so a view switch keeps the project name without touching the
   shell file.
   The switcher reads /api/projects (../fixtures/projects.json with
   ?fixtures): when the registry lists 2 or more projects the name
   becomes a menu button. Each row carries a running dot (glyph plus
   word, never colour alone); a running row navigates to
   http://127.0.0.1:<port>/, a stopped row shows the launch command to
   copy. The menu is keyboard accessible (Enter/Space toggle,
   Up/Down move, Escape closes) and read-only: it never writes.
   Needs: FIX from el.js; state and applyChrome from state.js. The shell
   keeps calling its own local applyChrome on every view change, so a
   MutationObserver on the h1 re-applies the project chrome whenever the
   shell resets it (converges: a header already showing the name is left
   alone). */
(function (global) {
  "use strict";
  var project = null;
  var projects = null;
  var inflight = false;
  var projectsInflight = false;
  var docKeyHandler = false;
  var menuOpen = false;
  function projectUrl() {
    if (typeof FIX !== "undefined" && FIX) return "../fixtures/project.json";
    return "/api/project";
  }
  function projectsUrl() {
    if (typeof FIX !== "undefined" && FIX) return "../fixtures/projects.json";
    return "/api/projects";
  }
  function projectName() {
    if (project && typeof project.name === "string" && project.name.trim()) return project.name.trim();
    return "";
  }
  function projectList() {
    if (projects && typeof projects === "object" && Array.isArray(projects.projects)) return projects.projects;
    return [];
  }
  function launchCmd(id) {
    return "python3 agent_dash.py project launch " + id;
  }
  function applyProject() {
    var h1 = document.getElementById("title");
    var name = projectName();
    var plan = typeof state !== "undefined" && state.view === "plan";
    if (h1) {
      if (name && projectList().length >= 2 && h1.parentNode) {
        renderSwitcher(h1, name);
      } else if (name) {
        h1.innerHTML = "";
        var main = document.createElement("span");
        main.setAttribute("class", "proj-name");
        main.textContent = name;
        h1.appendChild(main);
        h1.appendChild(document.createTextNode(" "));
        var sub = document.createElement("span");
        sub.setAttribute("class", "proj-sub");
        sub.textContent = "/ project tracker";
        h1.appendChild(sub);
        var stale = menuEl(h1);
        if (stale && stale.parentNode) stale.parentNode.removeChild(stale);
      } else if (h1.textContent !== "Agent dashboard") {
        h1.textContent = "Agent dashboard";
      }
    }
    document.title = (plan ? "Plan" : "Execution") + " \u00B7 " + (name || "Agent dashboard");
  }
  function menuEl(h1) {
    if (!h1 || !h1.parentNode || typeof h1.parentNode.querySelector !== "function") return null;
    return h1.parentNode.querySelector(".proj-menu");
  }
  function closeMenu(h1) {
    menuOpen = false;
    var menu = menuEl(h1);
    var btn = h1 && h1.parentNode && typeof h1.parentNode.querySelector === "function" ? h1.parentNode.querySelector(".proj-switcher") : null;
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }
  function renderSwitcher(h1, name) {
    if (!h1 || typeof document.createElement !== "function") return;
    var list = projectList();
    if (list.length < 2 || !name) return;
    if (!h1.parentNode) return;
    h1.innerHTML = "";
    var wrap = document.createElement("span");
    wrap.setAttribute("class", "proj-wrap");
    var btn = document.createElement("button");
    btn.setAttribute("type", "button");
    btn.setAttribute("class", "proj-switcher");
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("title", "Switch project (" + list.length + " projects)");
    var label = document.createElement("span");
    label.setAttribute("class", "proj-name");
    label.textContent = name;
    btn.appendChild(label);
    btn.appendChild(document.createTextNode(" \u25BE"));
    var menu = document.createElement("div");
    menu.setAttribute("class", "proj-menu");
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    list.forEach(function (entry, index) {
      if (!entry || typeof entry !== "object") return;
      var pid = typeof entry.id === "string" ? entry.id : "";
      var pname = typeof entry.name === "string" && entry.name ? entry.name : pid;
      var port = entry.port;
      var running = entry.running === true;
      var row;
      if (running) {
        row = document.createElement("a");
        row.setAttribute("href", "http://127.0.0.1:" + port + "/");
        if (typeof entry.current !== "undefined" && entry.current) row.setAttribute("aria-current", "true");
      } else {
        row = document.createElement("div");
      }
      row.setAttribute("role", "menuitem");
      row.setAttribute("class", "proj-item");
      row.setAttribute("tabindex", index === 0 ? "0" : "-1");
      var dot = document.createElement("span");
      dot.setAttribute("class", "proj-dot " + (running ? "on" : "off"));
      dot.textContent = running ? "\u25CF" : "\u25CB";
      row.appendChild(dot);
      var who = document.createElement("span");
      who.setAttribute("class", "proj-item-name");
      who.textContent = pname + (entry.current ? " (current)" : "");
      row.appendChild(who);
      var st = document.createElement("span");
      st.setAttribute("class", "proj-state");
      st.textContent = running ? "running" : "stopped";
      row.appendChild(st);
      if (!running && pid) {
        var cmd = document.createElement("code");
        cmd.setAttribute("class", "proj-cmd");
        cmd.textContent = launchCmd(pid);
        row.appendChild(cmd);
        var copy = document.createElement("button");
        copy.setAttribute("type", "button");
        copy.setAttribute("class", "proj-copy");
        copy.textContent = "copy";
        copy.onclick = (function (text, el) {
          return function () {
            var done = function () { el.textContent = "copied"; };
            try {
              if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, done);
              } else { done(); }
            } catch (e) { done(); }
          };
        })(launchCmd(pid), copy);
        row.appendChild(copy);
      }
      row.onkeydown = (function (pos) {
        return function (ev) {
          var e = ev || {};
          var key = e.key || "";
          if (key === "Escape") {
            closeMenu(h1);
            if (btn.focus) btn.focus();
          } else if (key === "ArrowDown" || key === "ArrowUp") {
            if (e.preventDefault) e.preventDefault();
            var items = menu.querySelectorAll ? menu.querySelectorAll(".proj-item") : [];
            var next = key === "ArrowDown" ? pos + 1 : pos - 1;
            if (next < 0) next = items.length - 1;
            if (next >= items.length) next = 0;
            if (items[next] && items[next].focus) items[next].focus();
          } else if ((key === "Enter" || key === " ") && typeof copy !== "undefined" && copy && copy.focus) {
            /* A stopped row's action is its copy button: move focus there
               so Enter/Space reach it without a pointer. */
            if (e.preventDefault) e.preventDefault();
            copy.focus();
          }
        };
      })(index);
      menu.appendChild(row);
    });
    btn.onclick = function () {
      var willOpen = menu.hidden;
      closeMenu(h1);
      if (willOpen) {
        menuOpen = true;
        menu.hidden = false;
        btn.setAttribute("aria-expanded", "true");
      }
    };
    btn.onkeydown = function (ev) {
      var e = ev || {};
      if ((e.key === "Enter" || e.key === " ") && btn.onclick) {
        if (e.preventDefault) e.preventDefault();
        btn.onclick();
      }
    };
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    /* The shell resets the title on every refresh, which rebuilds this
       menu closed; restore the open state so a refresh never collapses
       a menu the viewer is reading. */
    if (menuOpen) {
      menu.hidden = false;
      btn.setAttribute("aria-expanded", "true");
    }
    h1.appendChild(wrap);
    h1.appendChild(document.createTextNode(" "));
    var suffix = document.createElement("span");
    suffix.setAttribute("class", "proj-sub");
    suffix.textContent = "/ project tracker";
    h1.appendChild(suffix);
    if (typeof document.addEventListener === "function" && !docKeyHandler) {
      docKeyHandler = true;
      document.addEventListener("keydown", function (ev) {
        var e = ev || {};
        if (e.key === "Escape") {
          var h = document.getElementById ? document.getElementById("title") : null;
          if (h) closeMenu(h);
        }
      });
    }
  }
  function loadProject() {
    if (inflight) return;
    inflight = true;
    /* Through the shared conditional layer (not a raw fetch): the nav
       data load wants the same URL at the same time, and sharing one
       fetch plus ETag/304 keeps the header chrome off the waterfall. */
    (typeof DashApi === "function" ? DashApi("/api/project", {}) : fetch(projectUrl(), {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    })).then(function (j) {
      project = j || null;
      applyProject();
    }).catch(function () {}).then(function () {
      inflight = false;
    });
  }
  function loadProjects() {
    if (projectsInflight) return;
    projectsInflight = true;
    (typeof DashApi === "function" ? DashApi("/api/projects", {}) : fetch(projectsUrl(), {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    })).then(function (j) {
      projects = j || null;
      applyProject();
    }).catch(function () {}).then(function () {
      projectsInflight = false;
    });
  }
  function wrapChrome() {
    var AdPage = (global.AdPage = global.AdPage || {});
    var prev = global.applyChrome || AdPage.applyChrome;
    if (typeof prev !== "function" || prev.__adProjectWrapped) return;
    function applyChromeWithProject() {
      var out = prev.apply(this, arguments);
      applyProject();
      return out;
    }
    applyChromeWithProject.__adProjectWrapped = true;
    global.applyChrome = AdPage.applyChrome = applyChromeWithProject;
  }
  function watchTitle() {
    var h1 = document.getElementById("title");
    if (!h1 || typeof MutationObserver === "undefined") return;
    var busy = false;
    new MutationObserver(function () {
      if (busy || !projectName()) return;
      var cur = h1.querySelector ? h1.querySelector(".proj-name") : null;
      if (cur && cur.textContent === projectName()) return;
      busy = true;
      try { applyProject(); } finally { busy = false; }
    }).observe(h1, {childList: true, characterData: true, subtree: true});
  }
  wrapChrome();
  watchTitle();
  loadProject();
  loadProjects();
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("focus", function () { loadProject(); loadProjects(); });
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.loadProject = loadProject;
  AdPage.loadProjects = loadProjects;
  AdPage.applyProject = applyProject;
  AdPage.projectName = projectName;
  AdPage.projectList = projectList;
  global.loadProject = loadProject;
  global.loadProjects = loadProjects;
  global.applyProject = applyProject;
  global.projectName = projectName;
  global.projectList = projectList;
})(window);
