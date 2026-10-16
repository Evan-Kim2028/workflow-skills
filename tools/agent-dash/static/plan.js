(function () {
  "use strict";
  var Q = new URLSearchParams(location.search);
  var current = Q.get("ws") || "";
  var bundles = [];
  var taskmap = {};
  var sources = [];
  var SERIES = {opus: "--s1", haiku: "--s2", sonnet: "--s3", grok: "--s4", muse: "--s5", glm: "--s6", fable: "--s7", other: "--other"};

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "text") node.textContent = attrs[key];
        else node.setAttribute(key, attrs[key]);
      });
    }
    (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }
  function svg(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }
  function darkNow() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set === "dark";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function fmtDur(seconds) {
    seconds = Math.max(0, +seconds || 0);
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    return Math.floor(seconds / 3600) + "h " + Math.round((seconds % 3600) / 60) + "m";
  }
  function tierKey(run) {
    var text = ((run && (run.tier || run.model)) || "").toLowerCase();
    var key;
    for (key in SERIES) if (text.indexOf(key) >= 0) return key;
    return "other";
  }
  function taskOf(run, ws) {
    var label = run.label || "";
    var table = taskmap[ws] || {};
    var match;
    if (table[label]) return table[label];
    Object.keys(table).forEach(function (key) {
      if (match || key.charAt(0) !== "/" || key.length < 3) return;
      try { if (new RegExp(key.slice(1, -1), "i").test(label)) match = table[key]; } catch (e) {}
    });
    if (match) return match;
    if (ws === "WS1") {
      match = /(?:^|[^a-z0-9])ws1[-._]?(\d+[a-z]?)/i.exec(label) || /WS1\.(\d+[a-z]?)/.exec(label);
      if (match) return match[1].toLowerCase();
    }
    if (ws === "WS2") {
      match = /ws2[-._]([0-9]+[a-z0-9]*)/i.exec(label);
      if (match) return "WS2." + match[1];
    }
    if (ws === "WS7") {
      match = /\b([PS]0-\d+)\b/i.exec(label);
      if (match) return match[1].toUpperCase();
      match = /ws7[-._]([0-9a-z]+)/i.exec(label);
      if (match) return "WS7." + match[1].toUpperCase();
    }
    return null;
  }

  function renderTabs() {
    var nav = document.getElementById("tabs");
    nav.innerHTML = "";
    bundles.forEach(function (bundle) {
      var button = el("button", {type: "button", class: "plan-tab", role: "tab", "aria-selected": String(bundle.id === current)});
      button.textContent = bundle.kind === "program" ? bundle.title : bundle.id + " " + bundle.title;
      button.addEventListener("click", function () { select(bundle.id); });
      nav.appendChild(button);
    });
  }

  function execHref(id) {
    var bundle = bundles.filter(function (item) { return item.id === id; })[0];
    var ws = !bundle || bundle.kind === "program" ? "all" : id;
    return "/static/index.html?ws=" + encodeURIComponent(ws);
  }

  function select(id) {
    current = id;
    var plan = document.getElementById("plan-link");
    plan.href = "/plan?ws=" + encodeURIComponent(id);
    document.getElementById("exec-link").href = execHref(id);
    if (history.replaceState) history.replaceState(null, "", plan.href);
    renderTabs();
    loadPage(id);
  }

  function loadPage(id) {
    var main = document.getElementById("body");
    main.textContent = "Loading.";
    fetch("/bundle/" + encodeURIComponent(id) + "/page.html", {cache: "no-store"}).then(function (response) {
      if (!response.ok) throw new Error("missing page");
      return response.text();
    }).then(function (html) {
      if (current !== id) return;
      main.innerHTML = html;
      captureMermaid(main);
      runMermaid();
      return refreshProgress(id);
    }).catch(function () {
      if (current === id) main.textContent = "This bundle has no page yet.";
    });
  }

  function captureMermaid(root) {
    sources = [];
    root.querySelectorAll("pre.mermaid").forEach(function (node, index) {
      sources.push(node.textContent);
      node.setAttribute("data-midx", String(index));
    });
  }

  function runMermaid() {
    if (!window.mermaid || !sources.length) return;
    var nodes = [];
    document.querySelectorAll("pre.mermaid").forEach(function (node) {
      var index = +node.getAttribute("data-midx");
      if (sources[index] == null) return;
      node.removeAttribute("data-processed");
      node.textContent = sources[index];
      nodes.push(node);
    });
    if (!nodes.length) return;
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: darkNow() ? "dark" : "default",
      fontFamily: "system-ui, sans-serif"
    });
    try {
      var pending = window.mermaid.run({nodes: nodes});
      if (pending && pending.catch) pending.catch(function () {});
    } catch (e) {}
  }

  function dagOf(payload, ws) {
    var plan = (payload && payload.plan) || {};
    var dag = plan.dag || plan;
    return {ws: ws, cols: dag.cols || [], groups: dag.groups || [], edges: dag.edges || []};
  }

  function drawProgress(host, dag, snap) {
    host.innerHTML = "";
    if (!dag.groups.length) {
      host.appendChild(el("p", {class: "sub", text: "This plan has no task groups yet."}));
      return;
    }
    var live = {};
    var just = {};
    var unassigned = [];
    var now = Date.now() / 1000;
    ((snap && snap.running) || []).forEach(function (run) {
      if (run.workstream && run.workstream !== dag.ws) return;
      var task = taskOf(run, dag.ws);
      if (task) (live[task] = live[task] || []).push(run);
      else if (run.workstream === dag.ws) unassigned.push(run);
    });
    ((snap && snap.recent) || []).forEach(function (run) {
      if (run.ended_at && now - run.ended_at < 900) {
        var task = taskOf(run, dag.ws);
        if (task) just[task] = run;
      }
    });
    if (unassigned.length) {
      var line = el("p", {class: "sub", text: "Running, not mapped to a task: " + unassigned.map(function (run) { return run.label || run.run_id; }).join(", ")});
      host.appendChild(line);
    }
    var legend = el("div", {class: "pp-legend"});
    [
      ["live", "agent working now"],
      ["pr", "waiting to merge (open PR)"],
      ["prog", "merged, waiting on a prod step"],
      ["done", "done"],
      ["just", "finished in the last 15 min"],
      ["todo", "not started"],
      ["deferred", "deferred or moved to another workstream"]
    ].forEach(function (pair) {
      var sample = pair[0] === "just" ? "done just" : pair[0];
      legend.appendChild(el("span", {}, [el("span", {class: "pp-chip " + sample}, [document.createTextNode("\u2007")]), document.createTextNode(pair[1])]));
    });
    host.appendChild(legend);
    var totals = {done: 0, prog: 0, pr: 0, todo: 0, deferred: 0};
    var scroll = el("div", {class: "pp-scroll"});
    var board = el("div", {class: "pp-dag"});
    var edges = svg("svg", {class: "pp-edges"});
    board.appendChild(edges);
    board.style.gridTemplateColumns = "repeat(" + Math.max(dag.cols.length, 1) + ",1fr)";
    var columns = (dag.cols.length ? dag.cols : ["Plan"]).map(function (name) {
      var column = el("div", {class: "pp-col"}, [el("div", {class: "pp-colh", text: name})]);
      board.appendChild(column);
      return column;
    });
    var nodes = {};
    dag.groups.forEach(function (group) {
      var counts = {done: 0, prog: 0, pr: 0, todo: 0, deferred: 0};
      (group.tasks || []).forEach(function (task) {
        var status = task.status || "todo";
        counts[status] = (counts[status] || 0) + 1;
      });
      var active = (group.tasks || []).length - (counts.deferred || 0);
      var nodeClass = "pp-node";
      if (active && counts.done === active) nodeClass += " s-done";
      else if (counts.done + counts.prog + counts.pr) nodeClass += " s-prog";
      var node = el("div", {class: nodeClass});
      node.appendChild(el("h4", {text: group.title || group.id || ""}));
      var meta = counts.done + " done · " + counts.pr + " waiting to merge · " + counts.prog + " waiting on prod · " + counts.todo + " not started";
      if (counts.deferred) meta += " · " + counts.deferred + " deferred";
      node.appendChild(el("div", {class: "m", text: meta}));
      var mini = el("div", {class: "pp-mini"});
      [["done", "var(--good)"], ["pr", "var(--warning)"], ["prog", "var(--s5)"]].forEach(function (pair) {
        var bar = el("span");
        bar.style.width = (active ? 100 * (counts[pair[0]] || 0) / active : 0) + "%";
        bar.style.background = pair[1];
        mini.appendChild(bar);
      });
      node.appendChild(mini);
      var chips = el("div", {class: "pp-chips"});
      var groupLive = 0;
      (group.tasks || []).forEach(function (task) {
        var status = task.status || "todo";
        var runs = live[task.id] || [];
        var recent = just[task.id];
        var chipClass = "pp-chip " + status + (runs.length ? " live" : "") + (recent && !runs.length ? " just" : "");
        var label = (task.title || task.id) + " · " + status + (task.moved_to ? " · moved to " + task.moved_to : "") + (task.note ? " · " + task.note : "");
        if (runs.length) label += " · working: " + runs.map(function (run) { return (run.label || run.run_id) + " (" + (run.tier || "") + ", " + fmtDur(run.elapsed_s) + ")"; }).join(", ");
        var chip = el("span", {class: chipClass, title: label, "aria-label": label}, [document.createTextNode(task.id)]);
        if (runs.length) {
          groupLive += runs.length;
          var badge = el("span", {class: "ag", text: String(runs.length)});
          badge.style.background = "var(" + SERIES[tierKey(runs[0])] + ")";
          chip.appendChild(badge);
        }
        chips.appendChild(chip);
      });
      if (groupLive) {
        node.insertBefore(el("div", {class: "pp-live", text: "● " + groupLive + " working"}), node.children[1]);
        node.classList.add("s-live");
      }
      node.appendChild(chips);
      var column = columns[group.col];
      if (column) column.appendChild(node);
      nodes[group.id] = node;
      ["done", "prog", "pr", "todo", "deferred"].forEach(function (key) { totals[key] += counts[key] || 0; });
    });
    var all = totals.done + totals.prog + totals.pr + totals.todo;
    var summary = totals.done + " of " + all + " done · " + totals.pr + " waiting to merge · " + totals.prog + " waiting on prod · " + totals.todo + " not started";
    if (totals.deferred) summary += " · " + totals.deferred + " deferred";
    host.appendChild(el("p", {class: "pp-sum", text: summary}));
    scroll.appendChild(board);
    host.appendChild(scroll);
    function draw() {
      while (edges.firstChild) edges.removeChild(edges.firstChild);
      var box = board.getBoundingClientRect();
      if (!box.width) return;
      edges.setAttribute("width", board.scrollWidth);
      edges.setAttribute("height", board.scrollHeight);
      (dag.edges || []).forEach(function (edge) {
        if (!nodes[edge[0]] || !nodes[edge[1]]) return;
        var from = nodes[edge[0]].getBoundingClientRect();
        var to = nodes[edge[1]].getBoundingClientRect();
        var path;
        if (Math.abs(from.left - to.left) < 4) {
          var x1 = from.left + from.width * 0.85 - box.left;
          var y1 = from.bottom - box.top;
          var x2 = to.left + to.width * 0.85 - box.left;
          var y2 = to.top - box.top;
          path = "M" + x1 + "," + y1 + " C" + (x1 + 14) + "," + (y1 + 10) + " " + (x2 + 14) + "," + (y2 - 10) + " " + x2 + "," + y2;
        } else {
          var ax = from.right - box.left;
          var ay = from.top + Math.min(26, from.height / 2) - box.top;
          var bx = to.left - box.left - 2;
          var by = to.top + Math.min(26, to.height / 2) - box.top;
          var mid = (ax + bx) / 2;
          path = "M" + ax + "," + ay + " C" + mid + "," + ay + " " + mid + "," + by + " " + bx + "," + by;
        }
        edges.appendChild(svg("path", {d: path}));
      });
    }
    requestAnimationFrame(function () { requestAnimationFrame(draw); });
    window.addEventListener("resize", draw);
  }

  function refreshProgress(id) {
    var host = document.querySelector("[data-plan-progress]");
    if (!host || current !== id) return Promise.resolve();
    return Promise.all([
      fetch("/api/bundle?ws=" + encodeURIComponent(id), {cache: "no-store"}).then(function (response) { return response.ok ? response.json() : null; }),
      fetch("/api/snapshot?ws=" + encodeURIComponent(id), {cache: "no-store"}).then(function (response) { return response.ok ? response.json() : {running: [], recent: []}; }).catch(function () { return {running: [], recent: []}; })
    ]).then(function (pair) {
      if (current !== id) return;
      var again = document.querySelector("[data-plan-progress]");
      if (!again || !pair[0]) return;
      drawProgress(again, dagOf(pair[0], id), pair[1]);
    }).catch(function () {});
  }

  document.getElementById("theme").addEventListener("click", function () {
    var root = document.documentElement;
    var next = darkNow() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("agent-dash-theme", next); } catch (e) {}
    runMermaid();
    if (current) refreshProgress(current);
  });

  fetch("/static/taskmap.json", {cache: "no-store"}).then(function (response) { return response.ok ? response.json() : {}; }).then(function (json) { taskmap = json || {}; }).catch(function () {});
  fetch("/api/bundles", {cache: "no-store"}).then(function (response) { return response.json(); }).then(function (payload) {
    bundles = payload.bundles || [];
    if (!bundles.length) {
      document.getElementById("body").textContent = "No plan bundles yet. ws new creates one.";
      return;
    }
    if (!bundles.some(function (bundle) { return bundle.id === current; })) current = bundles[0].id;
    select(current);
  }).catch(function () {
    document.getElementById("body").textContent = "The bundle list did not load.";
  });
  setInterval(function () { if (current) refreshProgress(current); }, 30000);
})();
