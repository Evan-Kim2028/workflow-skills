/* Page core: project header (name in the header and <title>).
   Exports (on window.AdPage, also kept as plain globals so the staying
   page script keeps calling every name unchanged): loadProject (fetch
   once per load and on focus) and applyProject (render the header from
   the last payload). The header shows the project name large with a
   muted "/ project tracker" suffix when /api/project has a name;
   otherwise it keeps today's "Agent dashboard" title. applyChrome is
   wrapped so a view switch keeps the project name without touching the
   shell file.
   Needs: FIX from el.js; state and applyChrome from state.js. The shell
   keeps calling its own local applyChrome on every view change, so a
   MutationObserver on the h1 re-applies the project chrome whenever the
   shell resets it (converges: a header already showing the name is left
   alone). */
(function (global) {
  "use strict";
  var project = null;
  var inflight = false;
  function projectUrl() {
    if (typeof FIX !== "undefined" && FIX) return "../fixtures/project.json";
    return "/api/project";
  }
  function projectName() {
    if (project && typeof project.name === "string" && project.name.trim()) return project.name.trim();
    return "";
  }
  function applyProject() {
    var h1 = document.getElementById("title");
    var name = projectName();
    var plan = typeof state !== "undefined" && state.view === "plan";
    if (h1) {
      if (name) {
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
      } else if (h1.textContent !== "Agent dashboard") {
        h1.textContent = "Agent dashboard";
      }
    }
    document.title = (plan ? "Plan" : "Execution") + " \u00B7 " + (name || "Agent dashboard");
  }
  function loadProject() {
    if (inflight) return;
    inflight = true;
    fetch(projectUrl(), {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (j) {
      project = j || null;
      applyProject();
    }).catch(function () {}).then(function () {
      inflight = false;
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
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("focus", function () { loadProject(); });
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.loadProject = loadProject;
  AdPage.applyProject = applyProject;
  AdPage.projectName = projectName;
  global.loadProject = loadProject;
  global.applyProject = applyProject;
  global.projectName = projectName;
})(window);
