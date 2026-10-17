(function () {
  "use strict";
  var current = "";
  var taskmap = {};
  var taskmapGet = function () { return taskmap; };
  var FIX = false;
  var sources = [];
  var cache = {};
  var showGen = 0;

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
    return AD.fmtDur(seconds);
  }
  function tierKey(run) {
    return ADM.info(run || {}).key;
  }

  function taskOf(run, ws) {
    var label = run.label || "";
    var table = (taskmapGet() || {})[ws] || {};
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
          badge.style.background = ADM.colorOf(tierKey(runs[0]));
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

  function fetchPage(id) {
    var url = FIX ? "../fixtures/bundles/" + encodeURIComponent(id) + "/page.html" : "/bundle/" + encodeURIComponent(id) + "/page.html";
    return fetch(url, {cache: "no-store"}).then(function (response) {
      if (!response.ok) return null;
      return response.text();
    }).catch(function () { return null; });
  }

  function fetchApprovals(id) {
    var url = FIX ? "../fixtures/approvals.json" : "/api/approvals?ws=" + encodeURIComponent(id);
    return fetch(url, {cache: "no-store"}).then(function (response) {
      if (!response.ok) return {ws: id, decided_at: null, decided_by: null, items: []};
      return response.json();
    }).then(function (payload) {
      if (payload && payload.approvals) {
        var hit = payload.approvals.filter(function (row) { return row.ws === id; })[0];
        return hit || {ws: id, decided_at: null, decided_by: null, items: []};
      }
      if (payload && payload.items) return payload;
      return {ws: id, decided_at: null, decided_by: null, items: []};
    }).catch(function () { return {ws: id, decided_at: null, decided_by: null, items: []}; });
  }

  function fetchBundle(id) {
    if (FIX) {
      return fetch("../fixtures/bundles/" + encodeURIComponent(id) + "/plan.json", {cache: "no-store"}).then(function (response) {
        if (!response.ok) return null;
        return response.json().then(function (plan) { return {plan: plan}; });
      }).then(function (hit) {
        if (!hit) return null;
        return fetch("../fixtures/plan-" + encodeURIComponent(id) + ".json", {cache: "no-store"}).then(function (response) {
          return response.ok ? response.json() : null;
        }).then(function (view) {
          if (view && view.progress) hit.progress = view.progress;
          return hit;
        }).catch(function () { return hit; });
      }).catch(function () { return null; });
    }
    return fetch("/api/bundle?ws=" + encodeURIComponent(id), {cache: "no-store"}).then(function (response) {
      return response.ok ? response.json() : null;
    }).catch(function () { return null; });
  }

  function fetchSnap(id) {
    if (!FIX) {
      return fetch("/api/snapshot?ws=" + encodeURIComponent(id), {cache: "no-store"}).then(function (response) {
        return response.ok ? response.json() : {running: [], recent: []};
      }).catch(function () { return {running: [], recent: []}; });
    }
    return fetch("../fixtures/snapshot-" + encodeURIComponent(id) + ".json", {cache: "no-store"}).then(function (response) {
      if (response.ok) return response.json();
      return fetch("../fixtures/snapshot.json", {cache: "no-store"}).then(function (fallback) {
        return fallback.ok ? fallback.json() : {running: [], recent: []};
      });
    }).catch(function () { return {running: [], recent: []}; });
  }

  function sectionSlug(text) {
    var slug = String(text || "part").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/g, "");
    return slug || "part";
  }

  function stampSectionIds(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("section[id] h3").forEach(function (h3) {
      if (h3.id) return;
      var section = h3.closest ? h3.closest("section") : null;
      var base = section && section.id ? section.id : "plan";
      var slug = sectionSlug(h3.textContent);
      var id = base + "-" + slug;
      var n = 2;
      while (document.getElementById(id)) {
        id = base + "-" + slug + "-" + n;
        n += 1;
      }
      h3.id = id;
    });
  }

  function paint(id, hit) {
    var main = document.getElementById("plan-body");
    if (!main || current !== id) return;
    if (hit.html) main.innerHTML = hit.html;
    else main.innerHTML = '<p class="sub">This bundle has no page yet.</p>';
    if (String(id).toUpperCase() === "OVERVIEW" && window.DashProgram) {
      var top = document.createElement("section"); top.id = "ov-where"; top.className = "program-host"; top.style.margin = "4px 0 18px";
      main.insertBefore(top, main.firstChild); window.DashProgram.mount(top);
    }
    if (hit.html) {
      captureMermaid(main);
      runMermaid();
      var host = main.querySelector("[data-plan-progress]");
      if (host && hit.bundle) drawSummary(host, dagOf(hit.bundle, id), hit.snap || {running: [], recent: []}, id, hit.bundle && hit.bundle.start_prompt, hit.bundle && hit.bundle.progress);
      drawTrains(main, id, hit.bundle);
    }
    stampSectionIds(main);
  }

  /* The live progress map lives on Execution only; the Plan page shows a compact summary and a jump there.
     Overall progress comes from the server progress object, never recomputed here. */
  function drawSummary(host, dag, snap, id, startPrompt, progress) {
    var pr = progress || {done: 0, total: 0, counts: {}, pct: 0};
    var c = pr.counts || {};
    var tot = pr.total || 0, live = ((snap && snap.running) || []).length;
    host.innerHTML = "";
    var wrap = el("div", {class: "pp-sum"});
    var bar = el("div", {class: "pp-bar", role: "img", "aria-label": (pr.done || 0) + " of " + tot + " tasks done"});
    [["done", "var(--good)"], ["prog", "var(--s5)"], ["pr", "var(--warning)"]].forEach(function (p) { if (!c[p[0]] || !tot) return; var sp = el("span"); sp.style.width = (100 * c[p[0]] / tot) + "%"; sp.style.background = p[1]; bar.appendChild(sp); });
    wrap.appendChild(bar);
    var pct = pr.pct || 0;
    wrap.appendChild(el("div", {class: "pp-nums", text: pct + "% done \u00B7 " + c.done + " of " + tot + " tasks \u00B7 " + (c.wip ? c.wip + " in progress \u00B7 " : "") + c.prog + " waiting on prod \u00B7 " + c.pr + " waiting to merge \u00B7 " + c.todo + " not started" + (c.deferred ? " \u00B7 " + c.deferred + " deferred" : "") + (live ? " \u00B7 " + live + " running now" : "")}));
    var btn = el("button", {class: "pp-go", text: "Open live progress \u2192"});
    btn.addEventListener("click", function () { if (window.DashNav) window.DashNav.openExec(id); });
    var row = el("div", {class: "pp-actions"}); row.appendChild(btn);
    if (startPrompt && window.DashCopyBtn) row.appendChild(window.DashCopyBtn(startPrompt));
    wrap.appendChild(row);
    host.appendChild(wrap);
  }

  function refreshProgress(id) {
    return Promise.all([fetchApprovals(id), fetchBundle(id), fetchSnap(id)]).then(function (parts) {
      if (current !== id) return;
      var hit = cache[id] || {html: null};
      hit.approvals = parts[0];
      hit.bundle = parts[1];
      hit.snap = parts[2];
      cache[id] = hit;
      if (window.DashCache) window.DashCache.snap[id] = parts[2];
      var main = document.getElementById("plan-body");
      if (!main) return;
      var host = main.querySelector("[data-plan-progress]");
      if (host && parts[1]) drawSummary(host, dagOf(parts[1], id), parts[2] || {running: [], recent: []}, id, parts[1] && parts[1].start_prompt, parts[1] && parts[1].progress);
      drawTrains(main, id, parts[1]);
      stampSectionIds(main);
    }).catch(function () {});
  }

  var TRAIN_SYM = {planned: "\u25CB", assembling: "\u25D0", gate: "\u25D1", pr: "\u29D7", merged: "\u2713", dropped: "\u2715"};

  function trainsOf(bundle) {
    var plan = (bundle && bundle.plan) || {};
    if (!plan.trains || !plan.trains.length) return [];
    return plan.trains.filter(function (t) { return t && typeof t === "object"; });
  }

  function dagTasks(dag) {
    var out = [];
    ((dag && dag.groups) || []).forEach(function (g) {
      (g.tasks || []).forEach(function (t) { if (t && t.id != null) out.push(t); });
    });
    return out;
  }

  function buildTrains(id, bundle) {
    var dag = dagOf(bundle, id);
    var trains = trainsOf(bundle);
    var tasks = {};
    dagTasks(dag).forEach(function (t) { tasks[t.id] = t; });
    var sec = el("section", {id: String(id).toLowerCase() + "-trains", class: "pl-sec lay"});
    sec.appendChild(el("h2", {}, [el("span", {class: "eb", text: "Merge trains"}), document.createTextNode("How the work ships")]));
    if (!trains.length) {
      sec.appendChild(el("p", {class: "sub", text: "No trains planned yet. The lead adds them with ws train."}));
      return sec;
    }
    var inTrain = {};
    trains.forEach(function (t) {
      var row = el("div", {class: "tr-row"});
      var head = el("div", {class: "tr-head"});
      head.appendChild(el("span", {class: "tr-title", text: t.title || t.id || "Untitled train"}));
      if (t.id) head.appendChild(el("span", {class: "mono tr-id", text: "(" + t.id + ")"}));
      if (t.branch) head.appendChild(el("span", {class: "mono tr-branch", text: t.branch}));
      var status = t.status || "planned";
      var sym = TRAIN_SYM[status] || "";
      head.appendChild(el("span", {class: "tr-status tr-s-" + status, text: (sym ? sym + " " : "") + status}));
      if (t.pr != null) head.appendChild(el("span", {class: "tr-pr", text: "PR " + t.pr}));
      row.appendChild(head);
      var chips = el("div", {class: "tr-chips"});
      (t.members || []).forEach(function (mid) {
        inTrain[mid] = 1;
        var task = tasks[mid] || {};
        var st = task.status || "todo";
        var label = mid + " \u00B7 " + (task.title || "") + " \u00B7 " + st;
        chips.appendChild(el("span", {class: "tr-chip " + st, title: label, "aria-label": label}, [document.createTextNode(mid)]));
      });
      row.appendChild(chips);
      var files = t.shared_files || [];
      var shared = el("span", {class: "tr-shared", title: files.length ? files.join(", ") : "no shared files yet"});
      shared.textContent = files.length + " shared file" + (files.length === 1 ? "" : "s");
      var meta = el("div", {class: "tr-meta"}, [shared]);
      row.appendChild(meta);
      if (t.gate) {
        var gate = el("div", {class: "tr-gate"});
        gate.appendChild(document.createTextNode("Gate: "));
        gate.appendChild(el("span", {class: "mono", text: t.gate}));
        row.appendChild(gate);
      }
      sec.appendChild(row);
    });
    var alone = dagTasks(dag).filter(function (t) {
      var st = t.status || "todo";
      return !inTrain[t.id] && (st === "pr" || st === "wip" || st === "done");
    }).map(function (t) { return t.id; });
    if (alone.length) sec.appendChild(el("p", {class: "sub tr-alone", text: "Ships alone: " + alone.join(", ")}));
    return sec;
  }

  function drawTrains(main, id, bundle) {
    var old = document.getElementById(String(id).toLowerCase() + "-trains");
    if (old && old.parentNode) old.parentNode.removeChild(old);
    if (!main || !bundle) return;
    var sec = buildTrains(id, bundle);
    var host = main.querySelector("[data-plan-progress]");
    var anchor = host && host.closest ? host.closest("section") : null;
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(sec, anchor.nextSibling);
    else main.appendChild(sec);
  }

  function show(id) {
    current = id;
    var gen = ++showGen;
    var hit = cache[id] || (window.DashCache && window.DashCache.bundle && window.DashCache.bundle[id]);
    var main = document.getElementById("plan-body");
    if (hit && hit.html !== undefined) paint(id, hit);
    else if (main) main.textContent = "Loading.";
    return Promise.all([fetchPage(id), fetchApprovals(id), fetchBundle(id), fetchSnap(id)]).then(function (parts) {
      if (current !== id || gen !== showGen) return true;
      var next = {html: parts[0], approvals: parts[1], bundle: parts[2], snap: parts[3]};
      cache[id] = next;
      if (window.DashCache) {
        window.DashCache.snap[id] = parts[3];
        window.DashCache.bundle[id] = next;
      }
      paint(id, next);
      return true;
    }).catch(function () { return false; });
  }

  window.PlanView = {
    configure: function (opts) {
      FIX = !!(opts && opts.fixtures);
      if (opts && opts.taskmapGet) taskmapGet = opts.taskmapGet;
    },
    show: show,
    refresh: function () { return current ? refreshProgress(current) : Promise.resolve(); },
    restyle: function () {
      runMermaid();
      if (current) return refreshProgress(current);
    }
  };
})();
