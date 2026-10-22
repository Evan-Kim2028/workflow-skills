/* Page core: the one write action (archive/restore from the page).
   Every other page control copies a CLI command; these buttons POST to
   /api/workstreams/archive and /api/workstreams/restore through the
   shared apiPost layer (which sends X-AgentDash-Action: 1). The buttons
   appear only when the server advertises {"actions": {"archive": true}}
   on /api/project (read into state.actionsArchive by loadNav); in
   read-only mode or the fixture preview the controls fall back to the
   copy-command button, so the CLI path stays visible.
   Archive opens an inline confirm popover (never window.confirm):
   "Archive <name>? It moves to the Archive view; you can restore it."
   with Archive and Cancel, Escape cancels, focus moves into the popover
   and back to the trigger on close. Success shows a toast
   "Archived <name>" with an Undo button (10 s) that POSTs restore, then
   refetches state and bundles and redraws without a reload. Restore is
   confirm-free with a "Restored <name>" toast. Failures (409/403/
   network) toast the server hint, never silent. Buttons are real
   <button>s with aria-labels naming the workstream, disabled in flight.
   Exports (on window.AdPage.actions, also kept as plain globals):
   actionsEnabled, archiveControl, restoreControl.
   Needs: el, apiPost, state, FIX, showToast, copyCmd, copyBtn, loadNav,
   refreshProject, refreshAll, isProjectView (plain globals at call time). */
(function (global) {
  "use strict";
  /* Real buttons only on the live page when the server allows writes.
     Fixture mode never writes even if a fixture advertised the flag. */
  function actionsEnabled() {
    if (typeof FIX !== "undefined" && FIX) return false;
    return !!(typeof state !== "undefined" && state && state.actionsArchive);
  }
  function displayName(id, name) {
    return (name ? name + " (" + id + ")" : id);
  }
  function failToast(verb, err, id) {
    var msg = (err && (err.hint || err.error)) || "request failed";
    showToast(verb + " " + id + " failed: " + msg);
  }
  /* Refetch state and bundles, then redraw in place (no reload), so the
     next poll already shows the new state. */
  function refreshAfterAction() {
    var nav = (typeof loadNav === "function") ? loadNav(true) : Promise.resolve();
    return nav.then(function () {
      if (typeof isProjectView === "function" && isProjectView()) {
        if (typeof refreshProject === "function") return refreshProject();
      } else if (typeof refreshAll === "function") {
        return refreshAll();
      }
    }).catch(function () {});
  }
  /* Toast with an Undo button on the shared #toast node, kept 10 s.
     Undo POSTs restore for the same workstream. Expiry detaches the
     button with its handler: an opacity-hidden Undo must never stay
     keyboard-reachable or re-fire its action after the window closes. */
  function actionToast(msg, onUndo) {
    var t = document.getElementById("toast");
    if (!t) {
      t = el("div", {id: "toast", role: "status"});
      document.body.appendChild(t);
    }
    if (typeof onUndo !== "function") { showToast(msg); return; }
    if (typeof showToast === "function" && showToast._h) clearTimeout(showToast._h);
    clearTimeout(actionToast._h);
    t.textContent = "";
    t.appendChild(document.createTextNode(msg + " "));
    var u = el("button", {type: "button", class: "toast-undo", text: "Undo"});
    u.setAttribute("aria-label", msg + " — undo");
    u.addEventListener("click", function () {
      clearTimeout(actionToast._h);
      t.classList.remove("show");
      t.classList.remove("has-action");
      onUndo();
    });
    t.appendChild(u);
    t.classList.add("show");
    t.classList.add("has-action");
    actionToast._h = setTimeout(function () {
      t.classList.remove("show"); t.classList.remove("has-action");
      if (u.parentNode === t) t.removeChild(u);
    }, 10000);
  }
  function doArchive(id, name, scope) {
    setBusy(scope, true);
    return apiPost("/api/workstreams/archive", {ws: id}).then(function () {
      actionToast("Archived " + (name || id), function () { doRestore(id, name, null); });
      refreshAfterAction();
    }).catch(function (err) {
      failToast("Archive", err, id);
    }).then(function () {
      setBusy(scope, false);
    });
  }
  /* Restore is confirm-free: one click POSTs, the toast names the workstream. */
  function doRestore(id, name, scope) {
    setBusy(scope, true);
    return apiPost("/api/workstreams/restore", {ws: id}).then(function () {
      showToast("Restored " + (name || id));
      refreshAfterAction();
    }).catch(function (err) {
      failToast("Restore", err, id);
    }).then(function () {
      setBusy(scope, false);
    });
  }
  /* Disable the buttons under one control while its request is in flight:
     no double submit. */
  function setBusy(scope, busy) {
    if (!scope) return;
    var btns = scope.querySelectorAll ? scope.querySelectorAll("button") : [];
    for (var i = 0; i < btns.length; i++) btns[i].disabled = !!busy;
  }
  /* Small secondary copy affordance beside every real button, so the CLI
     path stays visible next to the click-to-run one. */
  function copyAffordance(cmd, id) {
    var c = el("button", {type: "button", class: "arch-copy", text: "Copy command"});
    c.setAttribute("aria-label", "Copy the archive command for " + id);
    c.title = "Copy: " + cmd;
    c.addEventListener("click", function (ev) { ev.stopPropagation(); copyCmd(cmd); });
    return c;
  }
  function archiveControl(id, name) {
    var wrap = el("span", {class: "arch-ctl"});
    if (!actionsEnabled()) {
      wrap.appendChild(copyBtn("python3 agent_dash.py ws archive " + id, "Archive"));
      return wrap;
    }
    var b = el("button", {type: "button", class: "arch-btn", text: "Archive"});
    b.setAttribute("aria-label", "Archive " + displayName(id, name));
    b.title = "Archive after the closing report — moves to the Archive view, restorable";
    b.addEventListener("click", function (ev) { ev.stopPropagation(); openConfirm(b, id, name); });
    wrap.appendChild(b);
    wrap.appendChild(copyAffordance("python3 agent_dash.py ws archive " + id, id));
    return wrap;
  }
  function restoreControl(id, name) {
    var wrap = el("span", {class: "arch-ctl"});
    if (!actionsEnabled()) {
      wrap.appendChild(copyBtn("python3 agent_dash.py ws restore " + id, "Restore"));
      return wrap;
    }
    var b = el("button", {type: "button", class: "arch-btn", text: "Restore"});
    b.setAttribute("aria-label", "Restore " + displayName(id, name));
    b.title = "Restore to done — the frozen card stays as previously archived";
    b.addEventListener("click", function (ev) { ev.stopPropagation(); doRestore(id, name, wrap); });
    wrap.appendChild(b);
    var c = copyAffordance("python3 agent_dash.py ws restore " + id, id);
    c.textContent = "Copy command";
    c.setAttribute("aria-label", "Copy the restore command for " + id);
    wrap.appendChild(c);
    return wrap;
  }
  /* Inline confirm popover (not window.confirm). Focus moves to the
     Archive button on open and back to the trigger on close; Escape or
     Cancel or an outside click closes it without acting. */
  var current = null;
  function closeConfirm() {
    if (!current) return;
    var anchor = current.anchor;
    if (current.pop && current.pop.parentNode) current.pop.parentNode.removeChild(current.pop);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("mousedown", onOutside, true);
    current = null;
    if (anchor && anchor.focus && document.body.contains(anchor)) anchor.focus();
  }
  function onKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); closeConfirm(); }
  }
  function onOutside(ev) {
    if (current && current.pop && !current.pop.contains(ev.target)) closeConfirm();
  }
  function openConfirm(anchor, id, name) {
    closeConfirm();
    var label = displayName(id, name);
    var pop = el("div", {class: "arch-confirm", role: "dialog"});
    pop.setAttribute("aria-label", "Confirm archiving " + label);
    pop.appendChild(el("p", {text: "Archive " + label + "? It moves to the Archive view; you can restore it."}));
    var row = el("div", {class: "arch-confirm-btns"});
    var yes = el("button", {type: "button", class: "arch-btn", text: "Archive"});
    yes.setAttribute("aria-label", "Confirm archiving " + label);
    var no = el("button", {type: "button", text: "Cancel"});
    no.setAttribute("aria-label", "Cancel archiving " + label);
    row.appendChild(yes);
    row.appendChild(no);
    pop.appendChild(row);
    document.body.appendChild(pop);
    try {
      var r = anchor.getBoundingClientRect();
      pop.style.left = Math.max(8, Math.min(window.innerWidth - pop.offsetWidth - 8, r.left)) + "px";
      pop.style.top = (r.bottom + 6) + "px";
      if (pop.getBoundingClientRect().bottom > window.innerHeight - 8) {
        pop.style.top = Math.max(8, r.top - pop.offsetHeight - 6) + "px";
      }
    } catch (e) { pop.style.left = "8px"; pop.style.top = "8px"; }
    current = {pop: pop, anchor: anchor};
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("mousedown", onOutside, true);
    yes.addEventListener("click", function () {
      yes.disabled = true;
      no.disabled = true;
      doArchive(id, name, pop).then(function () { closeConfirm(); });
    });
    no.addEventListener("click", function () { closeConfirm(); });
    yes.focus();
  }
  var AdActions = (global.AdPage = global.AdPage || {});
  AdActions.actions = AdActions.actions || {};
  AdActions.actions.actionsEnabled = actionsEnabled;
  AdActions.actions.archiveControl = archiveControl;
  AdActions.actions.restoreControl = restoreControl;
  global.AdArchive = {
    actionsEnabled: actionsEnabled,
    archiveControl: archiveControl,
    restoreControl: restoreControl
  };
  global.actionsEnabled = actionsEnabled;
  global.archiveControl = archiveControl;
  global.restoreControl = restoreControl;
})(window);
