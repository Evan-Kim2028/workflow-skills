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
  function darkNow() {
    /* Same rule as the shell (static/core/theme.js): an explicit choice wins,
       otherwise dark. No prefers-color-scheme fallback. */
    if (window.AdTheme && window.AdTheme.isDark) return window.AdTheme.isDark();
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set === "dark";
    return true;
  }

  /* Same rule as the execution map and dash/taskmatch.py: exact label,
     /regex/ keys, WS1/WS2/WS7 built-ins, then plan task-id substring. */
  function taskOf(run, ws, ids) {
    var label = run.label || "";
    var list = ids || [];
    var hit = (function () {
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
      match = /WS2\.([0-9]+[a-z0-9]*|[A-Z][0-9]*)/i.exec(label) || /ws2[-._]([0-9]+[a-z0-9]*)/i.exec(label);
      if (match) return "WS2." + (/^[a-z]/i.test(match[1]) ? match[1].toUpperCase() : match[1]);
    }
    if (ws === "WS7") {
      match = /\b([PS]0-\d+)\b/i.exec(label);
      if (match) return match[1].toUpperCase();
      match = /WS7\.([0-9a-z]+)/i.exec(label) || /ws7[-._]([0-9a-z]+)/i.exec(label);
      if (match) return "WS7." + match[1].toUpperCase();
    }
    var list = ids || [];
    if (list.length) {
      var lower = label.toLowerCase(), best = null;
      list.forEach(function (id) {
        var i = lower.indexOf(String(id).toLowerCase());
        if (i < 0) return;
        var before = i === 0 ? "" : lower.charAt(i - 1), after = lower.charAt(i + id.length);
        if ((before === "" || /[^a-z0-9]/.test(before)) && (after === "" || /[^a-z0-9]/.test(after)) && (!best || id.length > best.length)) best = id;
      });
      if (best) return best;
    }
    return null;
    })();
    /* Canonical label ws<N>-<mark>-<slug>[-rK] names WS<N>.<slug>, else the
       longest dash-bounded prefix of the slug that is a plan task. Same rule
       as dash/taskmatch.py _canonical_label_task. */
    function canonicalTask() {
      var m = /^ws(\d+)-(?:plan|exec|review|coord)-(.+?)(?:-r\d+)?$/i.exec(label);
      if (!m || !list.length || String(ws || "").toUpperCase() !== "WS" + m[1]) return null;
      var prefix = "WS" + m[1] + ".", byLower = {}, parts = m[2].split("-"), k, found;
      list.forEach(function (id) {
        var text = String(id), key = text.toLowerCase();
        if (key.indexOf(prefix.toLowerCase()) === 0 && !byLower[key]) byLower[key] = text;
      });
      for (k = parts.length; k > 0; k--) {
        found = byLower[(prefix + parts.slice(0, k).join("-")).toLowerCase()];
        if (found) return found;
      }
      return null;
    }
    if (hit != null && (!list.length || list.indexOf(hit) !== -1)) return hit;
    return canonicalTask() || hit;
  }
  /* Phantom-task guard, mirroring dash/taskmatch.task_of_verified: a mapped
     id that names no task in this plan stays unmapped instead of claiming a
     chip that does not exist (e.g. WS7.OPS). With no plan ids to check
     against, verification is impossible and the raw mapping stands. */
  function taskOfVerified(run, ws, ids) {
    var hit = taskOf(run, ws, ids);
    if (hit == null) return null;
    var list = ids || [];
    if (!list.length) return hit;
    return list.indexOf(hit) !== -1 ? hit : null;
  }

  function captureMermaid(root) {
    sources = [];
    root.querySelectorAll("pre.mermaid").forEach(function (node, index) {
      sources.push(node.textContent);
      node.setAttribute("data-midx", String(index));
    });
  }

  /* Mermaid (3.5 MB) loads on the first plan render, never with the
     page: the Execution first paint skips parsing it entirely. */
  var mermaidWaiters = [];
  function ensureMermaid(done) {
    if (window.mermaid) { done(); return; }
    mermaidWaiters.push(done);
    if (mermaidWaiters.length > 1) return;
    var script = document.createElement("script");
    script.src = "/static/vendor/mermaid.min.js";
    script.onload = function () {
      var fns = mermaidWaiters;
      mermaidWaiters = [];
      fns.forEach(function (fn) { try { fn(); } catch (e) {} });
    };
    script.onerror = function () { mermaidWaiters = []; };
    document.head.appendChild(script);
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

  /* Same endpoint the Execution tab reads, so the map below the summary
     is the same live task-chip map: progress, status colors, chips and
     dependency arrows from the one planCard renderer. window.DashApi
     covers fixture preview too (/api/plan maps to plan-<id>.json). */
  function planApi(path, params) {
    if (window.DashApi) return window.DashApi(path, params);
    var qs = new URLSearchParams(params || {}).toString();
    return fetch(path + (qs ? "?" + qs : ""), {cache: "no-store"}).then(function (response) {
      if (!response.ok) throw new Error("http " + response.status);
      return response.json();
    });
  }

  function fetchPlan(id) {
    return planApi("/api/plan", {ws: id}).then(function (plan) {
      return plan && plan.groups ? plan : null;
    }).catch(function () { return null; });
  }

  function fetchRuns(id) {
    var dayAgo = Math.floor(Date.now() / 1000) - 24 * 3600;
    return planApi("/api/runs", {ws: id, since: dayAgo, limit: 5000}).then(function (payload) {
      return (payload && payload.runs) || [];
    }).catch(function () { return []; });
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
      var planP = fetch("../fixtures/bundles/" + encodeURIComponent(id) + "/plan.json", {cache: "no-store"}).then(function (response) {
        if (response.ok) return response.json().then(function (plan) { return {plan: plan}; });
        /* Planned and archived fixture bundles have no bundle directory:
           fall back to the generated plan view (same groups and phases). */
        return fetch("../fixtures/plan-" + encodeURIComponent(id) + ".json", {cache: "no-store"}).then(function (r2) {
          if (!r2.ok) return null;
          return r2.json().then(function (view) {
            return {plan: {dag: {cols: view.cols || [], groups: view.groups || [], edges: view.edges || []}, narrative: {phases: view.phases || []}, trains: view.trains || []}};
          });
        });
      }).catch(function () { return null; });
      var metaP = fetch("../fixtures/bundles/" + encodeURIComponent(id) + "/bundle.json", {cache: "no-store"}).then(function (response) {
        return response.ok ? response.json() : null;
      }).catch(function () { return null; });
      return Promise.all([planP, metaP]).then(function (parts) {
        var hit = parts[0];
        if (!hit) return null;
        /* Bundle meta travels beside the plan (never inside it) so the
           trains section keeps reading the plan shape it always did. */
        if (parts[1]) hit.bundleMeta = parts[1];
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

  /* Archived bundles stay reachable read-only at the same URL, with an
     "Archived <date>" banner. The bundle payload already carries the
     lifecycle fields, so no scope change on the fetch was needed. */
  function archBanner(meta) {
    if (!meta || meta.state !== "archived") return null;
    var stamp = meta.archived_at;
    var day = "an unknown date";
    if (typeof stamp === "number" && isFinite(stamp)) {
      var d = new Date(stamp * 1000);
      var m = d.getUTCMonth() + 1, dday = d.getUTCDate();
      day = d.getUTCFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (dday < 10 ? "0" + dday : dday);
    }
    var b = document.createElement("div");
    b.className = "arch-banner";
    b.setAttribute("role", "note");
    b.textContent = "\u25A0 Archived " + day + " — read-only";
    return b;
  }
  function paint(id, hit) {
    var main = document.getElementById("plan-body");
    if (!main || current !== id) return;
    if (hit.html) main.innerHTML = hit.html;
    else main.innerHTML = '<p class="sub">This bundle has no page yet.</p>';
    var banner = archBanner((hit.bundle && hit.bundle.bundle) || hit.bundleMeta);
    if (banner) main.insertBefore(banner, main.firstChild);
    if (String(id).toUpperCase() === "OVERVIEW" && window.DashProgram) {
      var top = document.createElement("section"); top.id = "ov-where"; top.className = "program-host"; top.style.margin = "4px 0 18px";
      main.insertBefore(top, main.firstChild); window.DashProgram.mount(top);
    }
    if (hit.html) {
      captureMermaid(main);
      ensureMermaid(runMermaid);
      drawTrains(main, id, hit.bundle);
    }
    stampSectionIds(main);
  }

  function refreshProgress(id) {
    return Promise.all([fetchApprovals(id), fetchBundle(id), fetchSnap(id), fetchPlan(id), fetchRuns(id)]).then(function (parts) {
      if (current !== id) return;
      var hit = cache[id] || {html: null};
      hit.approvals = parts[0];
      hit.bundle = parts[1];
      hit.snap = parts[2];
      hit.plan = parts[3];
      hit.runs = parts[4];
      cache[id] = hit;
      if (window.DashCache) window.DashCache.snap[id] = parts[2];
      var main = document.getElementById("plan-body");
      if (!main) return;
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
    /* Progress used to sit between Plan and Agent-friendly; with it gone the
       trains keep that slot by landing just before the Agent-friendly section. */
    var anchor = document.getElementById(String(id).toLowerCase() + "-agent");
    if (anchor && anchor.parentNode === main) main.insertBefore(sec, anchor);
    else main.appendChild(sec);
  }

  function show(id) {
    current = id;
    var gen = ++showGen;
    var hit = cache[id] || (window.DashCache && window.DashCache.bundle && window.DashCache.bundle[id]);
    var main = document.getElementById("plan-body");
    if (hit && hit.html !== undefined) paint(id, hit);
    else if (main) main.textContent = "Loading.";
    return Promise.all([fetchPage(id), fetchApprovals(id), fetchBundle(id), fetchSnap(id), fetchPlan(id), fetchRuns(id)]).then(function (parts) {
      if (current !== id || gen !== showGen) return true;
      var next = {html: parts[0], approvals: parts[1], bundle: parts[2], snap: parts[3], plan: parts[4], runs: parts[5]};
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
      ensureMermaid(runMermaid);
      if (current) return refreshProgress(current);
    }
  };
})();
