/* Page core: the workstream management table and every lifecycle control.
   The Current view (Execution on Overall) leads with one row per
   non-archived, non-dropped workstream: name, state pill (glyph plus
   word), progress bar with percent, live agents, last activity,
   depends-on chips, and a row action cluster. Above it a toolbar filters
   by state chips with counts, searches by name or id, and runs bulk
   actions on checked rows (Archive done over every done workstream,
   Pause, Resume, Mark done). Rows that are complete but not done carry a
   Mark done primary button; done rows an Archive primary; planned rows a
   Start. The workstream tab heading carries the same primary action as a
   split button with the remaining actions in its menu, and Project home
   and Planned rows reuse the primary through primaryFor.
   Every action posts through apiPost with X-AgentDash-Action: 1, asks
   first for archive, drop, and bulk (inline confirm popover, never
   window.confirm), toasts success with an Undo button kept 10 s (undo
   posts the reverse transition; bulk undo restores all that changed),
   disables its buttons while the request is in flight, and names the
   workstream in every aria-label. Dropping is terminal and says so: its
   confirm is the stronger one and its toast carries no Undo, as does the
   Start toast (active has no way back to planned). In read-only mode
   every action button becomes a copy-command button, as archive does.
   Exports (on window.AdManage, also kept as plain globals): primaryFor,
   runDescriptor, manageSpec, wsHeaderSpec, keysPop, toastUndo,
   openConfirm, manageEnabled, reverseOf.
   Needs: el, state, ADT, apiPost, loadNav, refreshAll, refreshProject,
   isProjectView, selectTab, openPlan, showToast, fmtN, fmtUsd, fmtDate,
   keyOf, ADC (plain globals at call time). */
(function (global) {
  "use strict";

  /* Real buttons only on the live page when the server allows writes;
     fixture mode never writes. Mirrors actionsEnabled so both controls
     agree. */
  function manageEnabled() {
    try {
      if (typeof actionsEnabled === "function") return actionsEnabled();
    } catch (e) {}
    return !!(typeof state !== "undefined" && state && state.actionsArchive);
  }

  function displayName(id, name) {
    return (name ? name + " (" + id + ")" : id);
  }
  function has(allowed, action) {
    return !!(allowed && allowed.indexOf(action) >= 0);
  }
  function completedOf(row) {
    try {
      if (typeof ADT !== "undefined" && ADT.isCompleted(row && row.progress)) return true;
    } catch (e) {}
    return false;
  }

  /* Action metadata for the page vocabulary. endpoint state/archive/
     restore maps to the POST path; cmd maps to the read-only copy. */
  var ACTS = {
    start: {label: "Start", endpoint: "state", target: "active",
      title: "Start: planned becomes active", cmd: "state", cmdArg: "active"},
    pause: {label: "Pause", endpoint: "state", target: "paused",
      title: "Pause: work stops here until resumed", cmd: "state", cmdArg: "paused"},
    resume: {label: "Resume", endpoint: "state", target: "active",
      title: "Resume: paused becomes active again", cmd: "state", cmdArg: "active"},
    mark_done: {label: "Mark done", endpoint: "state", target: "done",
      title: "Mark done: the plan reads complete", cmd: "state", cmdArg: "done"},
    reopen: {label: "Reopen", endpoint: "state", target: "active", note: "Reopened from the dashboard",
      title: "Reopen: done becomes active again", cmd: "state", cmdArg: "active"},
    archive: {label: "Archive", endpoint: "archive",
      title: "Archive after the closing report — moves to the Archive view, restorable", cmd: "archive"},
    restore: {label: "Restore", endpoint: "restore",
      title: "Restore to done — the frozen card stays as previously archived", cmd: "restore"},
    drop: {label: "Drop", endpoint: "state", target: "dropped", danger: true,
      title: "Drop: terminal, the workstream leaves every view", cmd: "state", cmdArg: "dropped"}
  };
  function cliFor(action, ws) {
    var meta = ACTS[action];
    if (!meta) return "python3 agent_dash.py ws list";
    if (meta.cmd === "state") return "python3 agent_dash.py ws state " + ws + " " + meta.cmdArg;
    return "python3 agent_dash.py ws " + meta.cmd + " " + ws;
  }

  /* The emphasized primary per row: Start for planned, Mark done for
     complete-but-not-done, Archive for done, Restore for archived. The
     same descriptor feeds the table, the tab header, the home cards,
     and the switcher quick action, so the primary never disagrees. */
  function primaryFor(row) {
    if (!row || !row.ws) return null;
    var allowed = row.allowed_actions || [];
    var pick = null;
    if (row.state === "planned" && has(allowed, "start")) pick = "start";
    else if (row.state === "archived" && has(allowed, "restore")) pick = "restore";
    else if (row.state === "done" && has(allowed, "archive")) pick = "archive";
    else if (row.state !== "done" && completedOf(row) && has(allowed, "mark_done")) pick = "mark_done";
    if (!pick) return null;
    var meta = ACTS[pick];
    var label = displayName(row.ws, row.name);
    var d = {action: pick, label: meta.label, title: meta.title + " — " + label, cmd: cliFor(pick, row.ws)};
    if (pick === "archive") d.confirm = {heading: "Archive " + label + "?", lines: ["It moves to the Archive view; you can restore it."]};
    if (pick === "reopen" && meta.note) d.note = meta.note;
    return d;
  }

  /* Every other allowed action as small buttons beside the primary. The
     primary action itself is excluded; callers render it emphasized. */
  function secondaryActions(row) {
    var allowed = row.allowed_actions || [];
    var primary = primaryFor(row);
    var out = [];
    ["pause", "resume", "mark_done", "reopen", "archive", "start", "restore", "drop"].forEach(function (action) {
      if (!has(allowed, action)) return;
      if (primary && primary.action === action) return;
      var meta = ACTS[action];
      var label = displayName(row.ws, row.name);
      var d = {action: action, label: meta.label, title: meta.title + " — " + label, cmd: cliFor(action, row.ws)};
      if (action === "archive") d.confirm = {heading: "Archive " + label + "?", lines: ["It moves to the Archive view; you can restore it."]};
      if (action === "drop") {
        d.danger = true;
        d.confirm = {heading: "Drop " + label + "?", danger: true,
          lines: ["Dropping is terminal: the workstream leaves every view and cannot be undone.",
            "Archive instead when the work finished."]};
      }
      if (action === "reopen" && meta.note) d.note = meta.note;
      out.push(d);
    });
    return out;
  }

  function copyCli(cmd, label) {
    var done = function () { showToast("Copied: " + cmd); };
    try {
      if (typeof copyCmd === "function") { copyCmd(cmd); return; }
    } catch (e) {}
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(cmd).then(done, done); return; }
    } catch (e) {}
    showToast("Copy this: " + cmd);
  }

  function setBusy(scope, busy) {
    if (!scope || !scope.querySelectorAll) return;
    var btns = scope.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = !!busy;
  }

  function refreshAfterAction() {
    var nav = (typeof loadNav === "function") ? loadNav() : Promise.resolve();
    return nav.then(function () {
      if (typeof isProjectView === "function" && isProjectView()) {
        if (typeof refreshProject === "function") return refreshProject();
      } else if (typeof refreshAll === "function") {
        return refreshAll();
      }
    }).catch(function () {});
  }

  /* Toast with an Undo button on the shared #toast node, kept 10 s.
     Same node and timing as the archive toast. */
  function toastUndo(msg, onUndo) {
    var t = document.getElementById("toast");
    if (!t) {
      t = el("div", {id: "toast", role: "status"});
      document.body.appendChild(t);
    }
    if (typeof onUndo !== "function") { showToast(msg); return; }
    try { if (showToast._h) clearTimeout(showToast._h); } catch (e) {}
    try { clearTimeout(toastUndo._h); } catch (e) {}
    t.textContent = "";
    t.appendChild(document.createTextNode(msg + " "));
    var u = el("button", {type: "button", class: "toast-undo", text: "Undo"});
    u.setAttribute("aria-label", msg + " — undo");
    u.addEventListener("click", function () {
      try { clearTimeout(toastUndo._h); } catch (e) {}
      t.classList.remove("show");
      t.classList.remove("has-action");
      onUndo();
    });
    t.appendChild(u);
    t.classList.add("show");
    t.classList.add("has-action");
    toastUndo._h = setTimeout(function () { t.classList.remove("show"); t.classList.remove("has-action"); }, 10000);
  }

  function failToast(verb, err, label) {
    var msg = (err && (err.hint || err.error)) || "request failed";
    showToast(verb + " " + label + " failed: " + msg);
  }

  /* The reverse of one successful result, for Undo. Archived restores,
     done reopens with the dashboard note, transitions post their
     previous state. Start (no way back to planned) and drop (terminal)
     have no reverse: those toasts carry no Undo. */
  function reverseOf(item) {
    if (!item || !item.ws) return null;
    if (item.state === "archived") return {endpoint: "restore", body: {ws: item.ws}};
    var prev = item.prev_state;
    if (!prev || item.state === "dropped" || prev === "planned") return null;
    if (item.state === "done" && prev === "active") {
      return {endpoint: "state", body: {ws: item.ws, state: "active", note: "Undo marking done from the dashboard"}};
    }
    return {endpoint: "state", body: {ws: item.ws, state: prev}};
  }

  function postReverse(rev, label) {
    return apiPost("/api/workstreams/" + rev.endpoint, rev.body).then(function () {
      showToast("Undone: " + label);
      refreshAfterAction();
    }).catch(function (err) {
      failToast("Undo", err, label);
    });
  }

  function postOne(endpoint, body, scope) {
    setBusy(scope, true);
    return apiPost("/api/workstreams/" + endpoint, body).then(function (j) {
      return j;
    }).catch(function (err) {
      throw err;
    }).then(function (j) {
      setBusy(scope, false);
      return j;
    }, function (err) {
      setBusy(scope, false);
      throw err;
    });
  }

  /* Run one descriptor: copy the CLI command in read-only mode, else
     confirm first when the descriptor asks, then POST. after() runs on
     success (refresh chrome such as the switcher bar). */
  function runDescriptor(d, ws, label, scope, after) {
    if (!d) return;
    if (!manageEnabled()) {
      copyCli(d.cmd, d.label);
      return;
    }
    var go = function () {
      var body = d.action === "archive" ? {ws: ws} : d.action === "restore" ? {ws: ws} : {ws: ws, state: ACTS[d.action].target};
      if (d.note) body.note = d.note;
      var endpoint = ACTS[d.action].endpoint;
      postOne(endpoint, body, scope).then(function (j) {
        var verb = d.action === "mark_done" ? "Marked done" : d.action === "reopen" ? "Reopened" : d.label + (d.label.slice(-1) === "e" ? "d" : "ed");
        var rev = reverseOf({ws: ws, state: (j && j.state) || ACTS[d.action].target, prev_state: j && j.prev_state});
        if (rev) toastUndo(verb + " " + label, function () { postReverse(rev, label); });
        else showToast(verb + " " + label);
        refreshAfterAction();
        if (typeof after === "function") { try { after(); } catch (e) {} }
      }).catch(function (err) {
        failToast(d.label, err, label);
      });
    };
    if (d.confirm) {
      var anchor = scope && scope.querySelector ? scope.querySelector("button") : null;
      openConfirm({anchor: anchor || document.activeElement, heading: d.confirm.heading,
        lines: d.confirm.lines, confirmLabel: d.label, danger: d.danger, onConfirm: go});
    } else {
      go();
    }
  }

  /* Inline confirm popover (never window.confirm). Focus moves to the
     confirm button on open and back to the trigger on close; Escape,
     Cancel, or an outside click closes without acting. Bulk confirms
     list every workstream the batch covers. */
  var current = null;
  function closeConfirm() {
    if (!current) return;
    var anchor = current.anchor;
    if (current.pop && current.pop.parentNode) current.pop.parentNode.removeChild(current.pop);
    document.removeEventListener("keydown", onConfirmKey, true);
    document.removeEventListener("mousedown", onConfirmOutside, true);
    current = null;
    if (anchor && anchor.focus && document.body.contains(anchor)) { try { anchor.focus(); } catch (e) {} }
  }
  function onConfirmKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); closeConfirm(); }
  }
  function onConfirmOutside(ev) {
    if (current && current.pop && !current.pop.contains(ev.target)) closeConfirm();
  }
  function openConfirm(opts) {
    closeConfirm();
    var pop = el("div", {class: "arch-confirm" + (opts.danger ? " danger" : ""), role: "dialog"});
    pop.setAttribute("aria-label", opts.heading || "Confirm");
    if (opts.heading) pop.appendChild(el("p", {class: "arch-confirm-head", text: opts.heading}));
    (opts.lines || []).forEach(function (line) { pop.appendChild(el("p", {text: line})); });
    if (opts.list && opts.list.length) {
      var ul = el("ul", {class: "arch-confirm-list"});
      opts.list.forEach(function (name) { ul.appendChild(el("li", {text: name})); });
      pop.appendChild(ul);
    }
    var row = el("div", {class: "arch-confirm-btns"});
    var yes = el("button", {type: "button", class: "arch-btn" + (opts.danger ? " danger" : ""), text: opts.confirmLabel || "Confirm"});
    yes.setAttribute("aria-label", (opts.confirmLabel || "Confirm") + (opts.heading ? ": " + opts.heading : ""));
    var no = el("button", {type: "button", text: "Cancel"});
    no.setAttribute("aria-label", "Cancel" + (opts.heading ? ": " + opts.heading : ""));
    row.appendChild(yes);
    row.appendChild(no);
    pop.appendChild(row);
    document.body.appendChild(pop);
    try {
      var anchor = opts.anchor;
      var r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
      if (r) {
        pop.style.left = Math.max(8, Math.min(window.innerWidth - pop.offsetWidth - 8, r.left)) + "px";
        pop.style.top = (r.bottom + 6 + (window.scrollY || 0)) + "px";
        pop.style.position = "absolute";
        if (pop.getBoundingClientRect().bottom > window.innerHeight - 8) {
          pop.style.top = Math.max(8, r.top - pop.offsetHeight - 6 + (window.scrollY || 0)) + "px";
        }
      } else { pop.style.left = "8px"; pop.style.top = "8px"; pop.style.position = "absolute"; }
    } catch (e) { pop.style.left = "8px"; pop.style.top = "8px"; }
    current = {pop: pop, anchor: opts.anchor};
    document.addEventListener("keydown", onConfirmKey, true);
    document.addEventListener("mousedown", onConfirmOutside, true);
    yes.addEventListener("click", function () {
      yes.disabled = true;
      no.disabled = true;
      var done = function () { closeConfirm(); };
      try {
        var out = opts.onConfirm();
        if (out && out.then) out.then(done, done);
        else done();
      } catch (e) { done(); }
    });
    no.addEventListener("click", function () { closeConfirm(); });
    yes.focus();
  }

  /* Small "Keys" popover documenting every shortcut. */
  function keysPop(anchor) {
    closeConfirm();
    var pop = el("div", {class: "arch-confirm ws-keys", role: "dialog"});
    pop.setAttribute("aria-label", "Keyboard shortcuts");
    pop.appendChild(el("p", {class: "arch-confirm-head", text: "Keyboard shortcuts"}));
    var ul = el("ul", {class: "arch-confirm-list"});
    ["/ — open the workstream switcher",
      "\u2191 \u2193 then Enter — move and pick in the switcher",
      "Esc — close popovers",
      "g then p — Project home",
      "g then c — Current",
      "g then l — Planned",
      "g then a — Archive"].forEach(function (line) { ul.appendChild(el("li", {text: line})); });
    pop.appendChild(ul);
    var row = el("div", {class: "arch-confirm-btns"});
    var done = el("button", {type: "button", text: "Close"});
    done.setAttribute("aria-label", "Close keyboard shortcuts");
    row.appendChild(done);
    pop.appendChild(row);
    document.body.appendChild(pop);
    try {
      var r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
      if (r) {
        pop.style.position = "absolute";
        pop.style.left = Math.max(8, Math.min(window.innerWidth - 320, r.left)) + "px";
        pop.style.top = (r.bottom + 6 + (window.scrollY || 0)) + "px";
      }
    } catch (e) {}
    current = {pop: pop, anchor: anchor};
    document.addEventListener("keydown", onConfirmKey, true);
    document.addEventListener("mousedown", onConfirmOutside, true);
    done.addEventListener("click", function () { closeConfirm(); });
    done.focus();
  }

  function statePill(st) {
    var words = {planned: "\u25CB Planned", active: "\u25CF Active", paused: "\u23F8 Paused",
      done: "\u2713 Done", archived: "\u2713 Archived", dropped: "\u2715 Dropped"};
    var cls = st === "done" || st === "archived" ? "done" : st === "active" ? "active" : st === "paused" ? "paused" : "todo";
    return el("span", {class: "prog-pill " + cls, text: words[st] || st, title: "State: " + (words[st] || st)});
  }

  function lastActive(ts) {
    if (ts == null || !isFinite(+ts)) return "\u2014";
    try {
      if (typeof ADC !== "undefined" && ADC.ago) {
        var rel = ADC.ago(Date.now() / 1000, +ts);
        if (rel) return rel;
      }
    } catch (e) {}
    try { return fmtDate(+ts); } catch (e) { return "\u2014"; }
  }

  /* Toolbar UI state: filter chip, search text, checked ids. In-memory;
     redraws restore the search text, never the focus. */
  var mg = {state: "all", q: "", checked: {}};

  /* Run-attributed rows carry no board name; the bundle title is the
     honest fallback so every row names its workstream. */
  function titleOf(ws, name) {
    if (name) return name;
    var hit = "";
    (state.bundles || []).forEach(function (b) {
      if (String(b.id).toUpperCase() === String(ws).toUpperCase()) hit = b.title || "";
    });
    return hit;
  }

  function tableRows() {
    var list = ((state.wssAll && state.wssAll.length ? state.wssAll : state.wss) || [])
      .filter(function (w) {
        var st = ADT.wsStateOf(w);
        return st !== "archived" && st !== "dropped" && w.ws !== "unassigned";
      });
    var seen = {}, out = [];
    list.forEach(function (w) {
      var key = String(w.ws).toUpperCase();
      if (seen[key]) return;
      seen[key] = 1;
      if (!w.name) w = Object.assign({}, w, {name: titleOf(w.ws, w.name)});
      out.push(w);
    });
    (state.bundles || []).forEach(function (b) {
      if (!b || b.kind === "program" || b.id === "OVERVIEW") return;
      var key = String(b.id).toUpperCase();
      if (seen[key]) return;
      var hit = null;
      ((state.wssAll || []).concat(state.wss || [])).forEach(function (w) {
        if (String(w.ws).toUpperCase() === key) hit = w;
      });
      var st = hit ? ADT.wsStateOf(hit) : (b.state || "active");
      if (st === "archived" || st === "dropped") return;
      seen[key] = 1;
      out.push({ws: b.id, name: b.title || "", state: st, progress: hit ? hit.progress : null,
        live_agents: 0, last_activity: null, depends_on: b.depends_on || [], allowed_actions: []});
    });
    var q = mg.q.trim().toLowerCase();
    out = out.filter(function (w) {
      if (mg.state !== "all" && ADT.wsStateOf(w) !== mg.state) return false;
      if (q && String(w.ws).toLowerCase().indexOf(q) < 0 && String(w.name || "").toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
    out.sort(function (a, b) {
      var an = parseInt((/(\d+)/.exec(a.ws) || [])[1] || "9999", 10), bn = parseInt((/(\d+)/.exec(b.ws) || [])[1] || "9999", 10);
      return an - bn;
    });
    return out;
  }

  function counts() {
    var c = {all: 0, active: 0, paused: 0, planned: 0, done: 0};
    tableRowsUnfiltered().forEach(function (w) {
      c.all++;
      var st = ADT.wsStateOf(w);
      if (c[st] != null) c[st]++;
    });
    return c;
  }
  function tableRowsUnfiltered() {
    var keep = mg.state, q = mg.q;
    mg.state = "all"; mg.q = "";
    var out = tableRows();
    mg.state = keep; mg.q = q;
    return out;
  }

  function actionButton(d, ws, label, small) {
    var b = el("button", {type: "button", class: small ? "mg-btn" : "arch-btn mg-primary", text: d.label});
    b.setAttribute("aria-label", d.label + " " + label);
    b.title = d.title;
    if (d.danger) b.classList.add("danger");
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var scope = b.parentNode;
      runDescriptor(d, ws, label, scope, function () { paintManage(); });
    });
    return b;
  }
  function copyButton(d, ws, label) {
    var b = el("button", {type: "button", class: "arch-copy", text: d.label});
    b.setAttribute("aria-label", "Copy the " + d.label.toLowerCase() + " command for " + label + " (read-only)");
    b.title = "Copy: " + d.cmd;
    b.addEventListener("click", function (ev) { ev.stopPropagation(); copyCli(d.cmd, d.label); });
    return b;
  }

  /* The row action cluster: the primary emphasized, the rest small. In
     read-only mode every button copies its CLI command. */
  function rowCluster(row) {
    var host = el("span", {class: "mg-cluster"});
    var label = displayName(row.ws, row.name);
    var enabled = manageEnabled();
    var primary = primaryFor(row);
    if (primary) host.appendChild(enabled ? actionButton(primary, row.ws, label, false) : copyButton(primary, row.ws, label));
    secondaryActions(row).forEach(function (d) {
      host.appendChild(enabled ? actionButton(d, row.ws, label, true) : copyButton(d, row.ws, label));
    });
    if (!host.firstChild) host.appendChild(el("span", {class: "mg-none", text: "\u2014"}));
    return host;
  }

  function progressCell(row) {
    var pr = row.progress || {};
    var pct = typeof pr.pct === "number" ? pr.pct : null;
    var done = pr.done || 0, tot = pr.total || 0;
    var wrap = el("span", {class: "mg-prog"});
    var bar = el("span", {class: "prog-bar", role: "img",
      "aria-label": tot ? (row.ws + " " + done + " of " + tot + " tasks done (" + pct + "%)") : (row.ws + " has no plan")});
    if (tot && pct != null) {
      var fill = el("span");
      fill.style.width = pct + "%";
      fill.style.background = pct >= 100 ? "var(--good)" : "var(--accent, var(--s1))";
      bar.appendChild(fill);
    }
    bar.style.minWidth = "90px";
    wrap.appendChild(bar);
    wrap.appendChild(el("span", {class: "mg-pct", text: pct == null ? "\u2014" : pct + "%"}));
    return wrap;
  }

  function dependsCell(row) {
    var deps = row.depends_on || [];
    var host = el("span", {class: "mg-deps"});
    if (!deps.length) { host.appendChild(el("span", {class: "mg-none", text: "\u2014"})); return host; }
    deps.forEach(function (dep) {
      var c = el("button", {type: "button", class: "mg-dep", text: dep});
      c.setAttribute("aria-label", "Open workstream " + dep + " (depends on, from " + row.ws + ")");
      c.title = row.ws + " waits on " + dep;
      c.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (typeof pickWs === "function") pickWs(dep);
        else if (typeof go === "function") go(dep);
      });
      host.appendChild(c);
    });
    return host;
  }

  var mgSection = null;

  function paintManage() {
    if (!mgSection || !mgSection.parentNode) return;
    paintToolbarCounts();
    paintRows();
    paintBulk();
  }

  function paintToolbarCounts() {
    if (!mgSection) return;
    var c = counts();
    var chips = mgSection.querySelectorAll("[data-mg-chip]");
    for (var i = 0; i < chips.length; i++) {
      var st = chips[i].getAttribute("data-mg-chip");
      var n = c[st] == null ? 0 : c[st];
      chips[i].textContent = (st === "all" ? "All" : st[0].toUpperCase() + st.slice(1)) + " " + fmtN(n);
      if (mg.state === st) chips[i].setAttribute("aria-pressed", "true");
      else chips[i].setAttribute("aria-pressed", "false");
    }
    var arch = mgSection.querySelector("[data-mg-archdone]");
    if (arch) {
      var doneRows = tableRowsUnfiltered().filter(function (w) { return ADT.wsStateOf(w) === "done" && has(w.allowed_actions, "archive"); });
      arch.disabled = !doneRows.length;
      arch.textContent = "Archive done (" + fmtN(doneRows.length) + ")";
      arch.setAttribute("aria-label", "Archive every done workstream, " + doneRows.length + ", after one confirm");
    }
  }

  function checkedRows() {
    var rows = tableRowsUnfiltered();
    return rows.filter(function (w) { return mg.checked[String(w.ws).toUpperCase()]; });
  }

  function paintBulk() {
    if (!mgSection) return;
    var bar = mgSection.querySelector("[data-mg-bulk]");
    if (!bar) return;
    bar.innerHTML = "";
    var sel = checkedRows();
    if (!sel.length) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    bar.appendChild(el("span", {class: "mg-sel", text: fmtN(sel.length) + " selected"}));
    [["pause", "Pause"], ["resume", "Resume"], ["mark_done", "Mark done"]].forEach(function (pair) {
      var eligible = sel.filter(function (w) { return has(w.allowed_actions, pair[0]); });
      var b = el("button", {type: "button", class: "mg-btn", text: pair[1] + " (" + fmtN(eligible.length) + ")"});
      var names = eligible.map(function (w) { return displayName(w.ws, w.name); });
      b.setAttribute("aria-label", pair[1] + " " + fmtN(eligible.length) + " eligible of " + fmtN(sel.length) + " selected workstreams");
      b.title = eligible.length ? "Applies to: " + names.join(", ") : "No selected workstream can take this action";
      b.disabled = !eligible.length;
      b.addEventListener("click", function () {
        runBulkState(pair[0], eligible, b);
      });
      bar.appendChild(b);
    });
    var clear = el("button", {type: "button", class: "arch-copy", text: "Clear"});
    clear.setAttribute("aria-label", "Clear the row selection");
    clear.addEventListener("click", function () { mg.checked = {}; paintManage(); });
    bar.appendChild(clear);
  }

  function runBulkState(action, rows, anchor) {
    var meta = ACTS[action];
    var ids = rows.map(function (w) { return w.ws; });
    var names = rows.map(function (w) { return displayName(w.ws, w.name); });
    var verb = action === "mark_done" ? "Mark done" : meta.label;
    if (!manageEnabled()) {
      copyCli(ids.map(function (id) { return cliFor(action, id); }).join(" && "), verb);
      return;
    }
    openConfirm({anchor: anchor, heading: verb + " " + fmtN(ids.length) + " workstreams?",
      lines: ["Each workstream moves on its own; ones that cannot stay put and are listed after."],
      list: names, confirmLabel: verb,
      onConfirm: function () {
        setBusy(mgSection, true);
        return apiPost("/api/workstreams/bulk", {action: "state", state: meta.target, ws: ids}).then(function (j) {
          setBusy(mgSection, false);
          bulkDone(j, verb + " workstreams");
        }).catch(function (err) {
          setBusy(mgSection, false);
          showToast(verb + " failed: " + ((err && (err.hint || err.error)) || "request failed"));
        });
      }});
  }

  function bulkDone(j, what) {
    var changed = (j && j.changed) || [];
    var results = (j && j.results) || [];
    var failed = results.filter(function (r) { return !r.ok; });
    mg.checked = {};
    if (changed.length && !failed.length) {
      var revs = results.filter(function (r) { return r.ok && reverseOf(r); }).map(function (r) { return reverseOf(r); });
      toastUndo(what + ": " + fmtN(changed.length) + " changed", function () { postBulkReverse(revs, what); });
    } else if (changed.length) {
      var revs2 = results.filter(function (r) { return r.ok && reverseOf(r); }).map(function (r) { return reverseOf(r); });
      var first = failed[0];
      toastUndo(what + ": " + fmtN(changed.length) + " changed, " + fmtN(failed.length) + " failed (" + (first.ws + ": " + (first.hint || first.error || "not eligible")) + ")",
        function () { postBulkReverse(revs2, what); });
    } else {
      var f0 = failed[0] || {};
      showToast(what + ": nothing changed" + (f0.ws ? " (" + f0.ws + ": " + (f0.hint || f0.error || "not eligible") + ")" : ""));
    }
    refreshAfterAction();
    paintManage();
  }

  function postBulkReverse(revs, what) {
    var chain = Promise.resolve();
    revs.forEach(function (rev) {
      chain = chain.then(function () { return apiPost("/api/workstreams/" + rev.endpoint, rev.body).catch(function () {}); });
    });
    chain.then(function () {
      showToast("Undone: " + what);
      refreshAfterAction();
    });
  }

  function runArchiveDone(anchor) {
    var doneRows = tableRowsUnfiltered().filter(function (w) { return ADT.wsStateOf(w) === "done" && has(w.allowed_actions, "archive"); });
    if (!doneRows.length) { showToast("Nothing done to archive."); return; }
    var ids = doneRows.map(function (w) { return w.ws; });
    var names = doneRows.map(function (w) { return displayName(w.ws, w.name); });
    if (!manageEnabled()) {
      copyCli(ids.map(function (id) { return "python3 agent_dash.py ws archive " + id; }).join(" && "), "Archive done");
      return;
    }
    openConfirm({anchor: anchor, heading: "Archive " + fmtN(ids.length) + " done workstreams?",
      lines: ["Each archives on its own after its closing report; ones that cannot stay put and are listed after."],
      list: names, confirmLabel: "Archive done",
      onConfirm: function () {
        setBusy(mgSection, true);
        return apiPost("/api/workstreams/bulk", {action: "archive", ws: ids}).then(function (j) {
          setBusy(mgSection, false);
          bulkDone(j, "Archived");
        }).catch(function (err) {
          setBusy(mgSection, false);
          showToast("Archive done failed: " + ((err && (err.hint || err.error)) || "request failed"));
        });
      }});
  }

  function rowEl(w) {
    var label = displayName(w.ws, w.name);
    var tr = el("tr", {"data-ws": w.ws});
    var sel = el("td", {class: "mg-c-check"});
    var box = el("input", {type: "checkbox", "aria-label": "Select " + label});
    box.checked = !!mg.checked[String(w.ws).toUpperCase()];
    box.addEventListener("change", function () {
      if (box.checked) mg.checked[String(w.ws).toUpperCase()] = 1;
      else delete mg.checked[String(w.ws).toUpperCase()];
      paintBulk();
      paintHeadCheck();
    });
    sel.appendChild(box);
    tr.appendChild(sel);
    var name = el("td", {class: "mg-c-name"});
    var open = el("button", {type: "button", class: "mg-name", text: w.ws});
    open.setAttribute("aria-label", "Open the " + label + " execution tab");
    open.title = w.name || w.ws;
    open.addEventListener("click", function () {
      if (typeof pickWs === "function") pickWs(w.ws, ADT.wsStateOf(w));
      else if (typeof go === "function") go(w.ws);
    });
    name.appendChild(open);
    if (w.name) name.appendChild(el("span", {class: "mg-title", text: w.name}));
    /* Lifetime planning-vs-execution bar under the name; the tooltip
       names every phase with exact values. Absent on rows with no
       split (older payloads, zero spend). */
    if (w.by_phase && typeof phaseBar === "function") {
      try {
        var split = phaseBar(w.by_phase);
        if (split) name.appendChild(split);
      } catch (e) {}
    }
    tr.appendChild(name);
    var st = el("td", {class: "mg-c-state"});
    st.appendChild(statePill(ADT.wsStateOf(w)));
    tr.appendChild(st);
    var pr = el("td", {class: "mg-c-prog"});
    pr.appendChild(progressCell(w));
    tr.appendChild(pr);
    var live = el("td", {class: "mg-c-live"});
    live.appendChild(el("span", {class: w.live_agents ? "ws-live" : "mg-none", text: w.live_agents ? "\u25CF " + w.live_agents + " live" : "\u2014"}));
    tr.appendChild(live);
    var act = el("td", {class: "mg-c-act"});
    act.appendChild(el("span", {text: lastActive(w.last_activity), title: w.last_activity ? "Last activity" : "No recorded activity"}));
    tr.appendChild(act);
    var dep = el("td", {class: "mg-c-dep"});
    dep.appendChild(dependsCell(w));
    tr.appendChild(dep);
    var ctl = el("td", {class: "mg-c-ctl"});
    ctl.appendChild(rowCluster(w));
    tr.appendChild(ctl);
    return tr;
  }

  function paintHeadCheck() {
    if (!mgSection) return;
    var head = mgSection.querySelector("[data-mg-checkall]");
    if (!head) return;
    var rows = tableRows();
    var all = rows.length > 0 && rows.every(function (w) { return mg.checked[String(w.ws).toUpperCase()]; });
    head.checked = all;
  }

  function paintRows() {
    if (!mgSection) return;
    var body = mgSection.querySelector("[data-mg-rows]");
    if (!body) return;
    body.innerHTML = "";
    var rows = tableRows();
    rows.forEach(function (w) { body.appendChild(rowEl(w)); });
    var empty = mgSection.querySelector("[data-mg-empty]");
    if (empty) empty.hidden = rows.length > 0;
    paintHeadCheck();
  }

  /* The Current management table: the first section of Execution on
     Overall. Keyed on data only, never on the filter text or the
     checkbox set, so typing and checking never lose focus. */
  function manageSpec() {
    var rows = tableRowsUnfiltered();
    var sig = rows.map(function (w) {
      return [w.ws, ADT.wsStateOf(w), w.progress && w.progress.pct, w.live_agents, w.last_activity,
        (w.depends_on || []).join("+"), (w.allowed_actions || []).join("+")];
    });
    var key = (typeof keyOf === "function" ? keyOf([sig, manageEnabled()]) : String(sig.length));
    return ["manage", key, function () {
      var wrap = el("div", {class: "card mg-wrap", "data-nav": "Manage", "data-manage": "1"});
      wrap.appendChild(el("h2", {text: "Workstreams"}));
      wrap.appendChild(el("p", {class: "sub", text: "Start, pause, resume, finish, archive — one row at a time or in bulk. Changes apply immediately, with Undo."}));
      var bar = el("div", {class: "mg-toolbar"});
      var chips = el("div", {class: "mg-chips", role: "group", "aria-label": "Filter rows by state"});
      ["all", "active", "paused", "planned", "done"].forEach(function (st) {
        var b = el("button", {type: "button", class: "mg-chip", "data-mg-chip": st});
        b.setAttribute("aria-label", "Show " + (st === "all" ? "every state" : st) + " workstreams");
        b.addEventListener("click", function () { mg.state = st; paintManage(); });
        chips.appendChild(b);
      });
      bar.appendChild(chips);
      var search = el("input", {type: "search", class: "mg-search", placeholder: "Search name or id\u2026",
        "aria-label": "Search workstreams by name or id"});
      search.value = mg.q;
      search.addEventListener("input", function () { mg.q = search.value; paintRows(); });
      bar.appendChild(search);
      var arch = el("button", {type: "button", class: "arch-btn", "data-mg-archdone": "1", text: "Archive done"});
      arch.addEventListener("click", function () { runArchiveDone(arch); });
      bar.appendChild(arch);
      wrap.appendChild(bar);
      var bulk = el("div", {class: "mg-bulk", "data-mg-bulk": "1"});
      bulk.hidden = true;
      wrap.appendChild(bulk);
      var scroll = el("div", {class: "mg-scroll"});
      var table = el("table", {class: "mg-table", "aria-label": "Workstream lifecycle controls"});
      var head = el("thead");
      var hr = el("tr");
      var checkTh = el("th", {class: "mg-c-check"});
      var checkAll = el("input", {type: "checkbox", "data-mg-checkall": "1", "aria-label": "Select every listed workstream"});
      checkAll.addEventListener("change", function () {
        var rows2 = tableRows();
        if (checkAll.checked) rows2.forEach(function (w) { mg.checked[String(w.ws).toUpperCase()] = 1; });
        else rows2.forEach(function (w) { delete mg.checked[String(w.ws).toUpperCase()]; });
        paintRows();
        paintBulk();
      });
      checkTh.appendChild(checkAll);
      hr.appendChild(checkTh);
      ["Workstream", "State", "Progress", "Live", "Last activity", "Waits on", "Actions"].forEach(function (t) {
        hr.appendChild(el("th", {text: t}));
      });
      head.appendChild(hr);
      table.appendChild(head);
      var body = el("tbody", {"data-mg-rows": "1"});
      table.appendChild(body);
      scroll.appendChild(table);
      var empty = el("p", {class: "pj-note", "data-mg-empty": "1", text: "No workstreams in this state."});
      empty.hidden = true;
      scroll.appendChild(empty);
      wrap.appendChild(scroll);
      mgSection = wrap;
      paintToolbarCounts();
      paintRows();
      paintBulk();
      return wrap;
    }, ""];
  }

  /* The workstream tab heading cluster: state pill plus the primary as
     a split button, the remaining allowed actions in its menu. */
  function wsHeaderSpec() {
    if (!state.ws || state.ws === "all") return null;
    var id = String(state.ws).toUpperCase();
    var hit = null;
    ((state.wssAll && state.wssAll.length ? state.wssAll : state.wss) || []).forEach(function (w) {
      if (String(w.ws).toUpperCase() === id) hit = w;
    });
    if (!hit) {
      (state.bundles || []).forEach(function (b) {
        if (String(b.id).toUpperCase() === id) hit = {ws: b.id, name: b.title || "", state: b.state || "active", allowed_actions: []};
      });
    }
    if (!hit) return null;
    var row = {ws: hit.ws, name: hit.name || "", state: ADT.wsStateOf(hit),
      progress: hit.progress || null, allowed_actions: hit.allowed_actions || []};
    var key = (typeof keyOf === "function" ? keyOf([[row.ws, row.state, row.progress && row.progress.pct, row.allowed_actions, manageEnabled()]]) : row.ws);
    return ["wshead", key, function () {
      var wrap = el("div", {class: "mg-head", "data-nav": "Workstream"});
      var title = el("div", {class: "mg-head-id"});
      title.appendChild(el("b", {text: row.ws}));
      if (row.name) title.appendChild(el("span", {text: row.name}));
      wrap.appendChild(title);
      wrap.appendChild(statePill(row.state));
      var label = displayName(row.ws, row.name);
      var enabled = manageEnabled();
      var primary = primaryFor(row);
      var rest = secondaryActions(row);
      /* Without an emphasized primary (a workstream in progress), the
         first allowed action leads the split so the heading always
         carries a real button plus its menu. */
      var lead = primary;
      if (!lead && rest.length) {
        lead = rest[0];
        rest = rest.slice(1);
      }
      if (lead || rest.length) {
        var split = el("span", {class: "mg-split"});
        if (lead) {
          split.appendChild(enabled ? actionButton(lead, row.ws, label, false) : copyButton(lead, row.ws, label));
        }
        if (rest.length) {
          var caret = el("button", {type: "button", class: "mg-caret", text: "\u25BE"});
          caret.setAttribute("aria-label", "More actions for " + label);
          caret.setAttribute("aria-expanded", "false");
          caret.setAttribute("aria-haspopup", "menu");
          var menu = el("div", {class: "mg-menu", role: "menu"});
          menu.hidden = true;
          rest.forEach(function (d) {
            var mi = el("button", {type: "button", role: "menuitem", class: "mg-menuitem", text: d.label});
            mi.setAttribute("aria-label", d.label + " " + label);
            mi.title = d.title;
            mi.addEventListener("click", function (ev) {
              ev.stopPropagation();
              menu.hidden = true;
              caret.setAttribute("aria-expanded", "false");
              runDescriptor(d, row.ws, label, split, function () {});
            });
            menu.appendChild(mi);
          });
          var closeMenu = function () { menu.hidden = true; caret.setAttribute("aria-expanded", "false"); };
          caret.addEventListener("click", function (ev) {
            ev.stopPropagation();
            menu.hidden = !menu.hidden;
            caret.setAttribute("aria-expanded", menu.hidden ? "false" : "true");
            if (!menu.hidden) {
              var first = menu.querySelector("button");
              if (first) first.focus();
            }
          });
          menu.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { ev.stopPropagation(); closeMenu(); caret.focus(); } });
          document.addEventListener("mousedown", function h(ev) {
            if (!menu.hidden && !split.contains(ev.target)) closeMenu();
          }, {once: true});
          split.appendChild(caret);
          split.appendChild(menu);
        }
        wrap.appendChild(split);
        if (!enabled) {
          var ro = el("span", {class: "mg-ro", text: "Read-only: buttons copy commands."});
          wrap.appendChild(ro);
        }
      } else {
        wrap.appendChild(el("span", {class: "mg-none", text: row.state === "dropped" ? "Dropped workstreams take no action." : "No actions for this state."}));
      }
      return wrap;
    }, ""];
  }

  var AdM = (global.AdPage = global.AdPage || {});
  AdM.manage = AdM.manage || {};
  AdM.manage.primaryFor = primaryFor;
  AdM.manage.runDescriptor = runDescriptor;
  AdM.manage.manageSpec = manageSpec;
  AdM.manage.wsHeaderSpec = wsHeaderSpec;
  AdM.manage.keysPop = keysPop;
  AdM.manage.toastUndo = toastUndo;
  AdM.manage.openConfirm = openConfirm;
  AdM.manage.manageEnabled = manageEnabled;
  AdM.manage.reverseOf = reverseOf;
  AdM.manage.secondaryActions = secondaryActions;
  global.AdManage = AdM.manage;
  global.primaryFor = primaryFor;
  global.runDescriptor = runDescriptor;
  global.manageSpec = manageSpec;
  global.wsHeaderSpec = wsHeaderSpec;
  global.keysPop = keysPop;
  global.toastUndo = toastUndo;
  global.openConfirm = openConfirm;
  global.manageEnabled = manageEnabled;
  global.reverseOf = reverseOf;
})(window);
