/* Doctor health: header pill, Health card, workstream problem badges.
   All DOM reads go through document at call time; the pure helpers
   (counts, pillFor, pillTitle, taskProblemMap, wsLabel, relAgo) run in
   node for tests. Numbers go through AD.fmtN; status is always a colour
   plus a symbol plus a word, never colour alone. */
(function (global) {
  "use strict";

  var SEVS = ["critical", "warn", "info"];
  var SYM = {critical: "\u2715", warn: "\u26A0", info: "\u2139"};
  var WORD = {critical: "critical", warn: "warn", info: "info"};

  function N(n) {
    try {
      if (global.AD && global.AD.fmtN) return global.AD.fmtN(n);
    } catch (e) {}
    return String(n == null ? 0 : n);
  }

  /* Relative age in plain words, e.g. "just now", "2 min ago", "3 h ago". */
  function relAgo(nowS, t) {
    if (t == null || !isFinite(+t)) return "unknown";
    var d = Math.max(0, (+nowS || 0) - (+t || 0));
    if (d < 90) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 48 * 3600) return Math.floor(d / 3600) + " h ago";
    return Math.floor(d / 86400) + " d ago";
  }

  function counts(doc) {
    var out = {critical: 0, warn: 0, info: 0, regressed: 0, total: 0};
    if (!doc || !doc.problems) return out;
    doc.problems.forEach(function (p) {
      if (out[p.severity] != null) out[p.severity]++;
      out.total++;
      if (p.regressed) out.regressed++;
    });
    return out;
  }

  /* Pill state: never "healthy" when the fetch failed or nothing arrived yet. */
  function pillFor(doc, failed) {
    if (failed) return {text: "\u26A0 doctor unavailable", tone: "warn"};
    if (!doc) return {text: "\u25CB checking health", tone: "muted"};
    var c = counts(doc);
    if (c.critical > 0) return {text: SYM.critical + " " + N(c.critical) + " critical", tone: "crit"};
    if (c.total > 0) return {text: SYM.warn + " " + N(c.total) + " problems", tone: "warn"};
    return {text: "\u2713 healthy", tone: "good"};
  }

  function pillTitle(doc, failed, nowS) {
    if (failed) return "Doctor check failed \u00B7 showing nothing new";
    if (!doc) return "Checking health\u2026";
    var c = counts(doc);
    var sum = c.total
      ? "Critical " + N(c.critical) + " \u00B7 Warn " + N(c.warn) + " \u00B7 Info " + N(c.info)
      : "No problems";
    return sum + " \u00B7 checked " + relAgo(nowS == null ? Date.now() / 1000 : nowS, doc.generated_at);
  }

  /* The card shows while loading, when the fetch failed, when problems
     exist, or when something resolved recently. A clean bill hides it. */
  function showCard(doc, failed) {
    if (failed || !doc) return true;
    return (doc.problems || []).length > 0 || (doc.resolved_24h || []).length > 0;
  }

  function wsLabel(ws, names) {
    if (!ws || ws === "global") return "global";
    var hit = null;
    try { hit = names && names[ws]; } catch (e) {}
    return hit ? ws + " " + hit : ws;
  }

  /* Problems that name a plan task: the contract id is
     "<code>:ws:<ID>:<task>" (scope "ws:<ID>"). */
  function taskProblemMap(problems, ws) {
    var out = {};
    (problems || []).forEach(function (p) {
      if (!p || !p.id) return;
      var parts = String(p.id).split(":");
      if (parts.length >= 4 && parts[1] === "ws" && parts[2] === ws && parts[3]) {
        if (!out[parts[3]]) out[parts[3]] = p;
      }
    });
    return out;
  }

  /* Clipboard with a textarea fallback, shared by every copy button here. */
  function copyText(text, done, fallback) {
    var nav = null;
    try { nav = global.navigator; } catch (e) {}
    function fb() {
      if (fallback) { fallback(); return; }
      fallbackModal(text);
    }
    try {
      if (nav && nav.clipboard && nav.clipboard.writeText) {
        nav.clipboard.writeText(text).then(function () { if (done) done(); }, fb);
        return;
      }
    } catch (e) {}
    fb();
  }

  function doc() {
    return global.document;
  }

  function fallbackModal(text) {
    var d = doc();
    if (!d || !d.createElement) return;
    var box = d.createElement("div"), ta = d.createElement("textarea"), close = d.createElement("button");
    box.setAttribute("class", "copy-modal");
    ta.setAttribute("readonly", "readonly");
    ta.value = text;
    close.textContent = "Close";
    close.addEventListener("click", function () { box.remove(); });
    var hint = d.createElement("p");
    hint.textContent = "Copy this command:";
    box.appendChild(hint);
    box.appendChild(ta);
    box.appendChild(close);
    d.body.appendChild(box);
    ta.focus();
    ta.select();
  }

  function h(tag, attrs, kids) {
    var d = doc(), e = d.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") e.textContent = attrs[k];
      else if (k.slice(0, 2) === "on" && typeof attrs[k] === "function") e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) {
      if (c == null) return;
      e.appendChild(typeof c === "string" ? d.createTextNode(c) : c);
    });
    return e;
  }

  function updatePill(node, docData, failed, nowS) {
    if (!node) return;
    var st = pillFor(docData, failed);
    node.textContent = st.text;
    node.setAttribute("data-tone", st.tone);
    node.setAttribute("title", pillTitle(docData, failed, nowS));
    if (node.hidden) node.hidden = false;
  }

  function scopeChip(p, wsNames) {
    var label = p.scope === "global" ? "global" : wsLabel(p.ws || (p.scope || "").replace(/^ws:/, ""), wsNames);
    return h("span", {class: "doc-scope", title: "Scope: " + (p.scope || "global")}, [label]);
  }

  function fixRow(p) {
    var cmd = p.fix && p.fix.command;
    var row = h("div", {class: "doc-fix"});
    if (cmd) {
      row.appendChild(h("code", {text: cmd}));
      var btn = h("button", {type: "button", class: "doc-copy", text: "Copy", title: "Copy the fix command"});
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        copyText(cmd, function () {
          btn.textContent = "Copied \u2713";
          setTimeout(function () { btn.textContent = "Copy"; }, 2000);
        });
      });
      row.appendChild(btn);
    }
    if (p.fix && p.fix.doc) {
      row.appendChild(h("a", {href: "/" + p.fix.doc, text: "problem doc", title: p.fix.doc}));
    }
    return row;
  }

  function problemRow(p, wsNames, nowS) {
    var sev = SEVS.indexOf(p.severity) >= 0 ? p.severity : "info";
    var row = h("div", {class: "doc-row sev-" + sev});
    var top = h("div", {class: "doc-top"}, [
      h("span", {class: "doc-sev", text: SYM[sev] + " " + WORD[sev]}),
      scopeChip(p, wsNames),
      h("span", {class: "doc-since", title: "First seen", text: "since " + relAgo(nowS, p.first_seen)}),
    ]);
    if (p.regressed) top.appendChild(h("span", {class: "doc-reg", title: "This problem came back after clearing", text: "regressed"}));
    if (p.count != null && p.count > 1) top.appendChild(h("span", {class: "doc-count", text: "\u00D7" + N(p.count)}));
    row.appendChild(top);
    row.appendChild(h("div", {class: "doc-title", text: p.title || p.id}));
    if (p.detail) row.appendChild(h("div", {class: "doc-detail", text: p.detail}));
    if (p.fix && (p.fix.command || p.fix.doc)) row.appendChild(fixRow(p));
    return row;
  }

  /* The Health card. opts: {wsNames, failed, nowS}. Loading, failed,
     problems and recent resolutions each render; a clean bill hides. */
  function healthCard(docData, opts) {
    opts = opts || {};
    var nowS = opts.nowS == null ? Date.now() / 1000 : opts.nowS;
    var card = h("div", {class: "card doc-card", id: "exec-health", "data-nav": "Health"});
    card.appendChild(h("h2", {text: "Health"}));
    if (opts.failed || !docData) {
      card.appendChild(h("p", {class: "sub", text: opts.failed ? "doctor unavailable" : "Checking health\u2026"}));
      return card;
    }
    var list = docData.problems || [];
    card.appendChild(h("p", {
      class: "sub",
      text: list.length
        ? "What the doctor found \u00B7 checked " + relAgo(nowS, docData.generated_at)
        : "No problems \u00B7 checked " + relAgo(nowS, docData.generated_at),
    }));
    SEVS.forEach(function (sev) {
      var rows = list.filter(function (p) { return p.severity === sev; });
      if (!rows.length) return;
      card.appendChild(h("div", {class: "doc-sevhead sev-" + sev, text: SYM[sev] + " " + WORD[sev] + " (" + N(rows.length) + ")"}));
      rows.forEach(function (p) { card.appendChild(problemRow(p, opts.wsNames, nowS)); });
    });
    var res = docData.resolved_24h || [];
    if (res.length) {
      var det = h("details", {class: "doc-resolved"});
      det.appendChild(h("summary", {text: "Resolved in the last 24 h (" + N(res.length) + ")"}));
      res.forEach(function (r) {
        det.appendChild(h("div", {class: "doc-res", text: "\u2713 " + (r.title || r.id || r.code)}));
      });
      card.appendChild(det);
    }
    return card;
  }

  function fixtureMode() {
    try { return new URLSearchParams(global.location.search).has("fixtures"); } catch (e) { return false; }
  }

  /* Fetch the doctor payload, fixture-aware. In fixture mode a missing
     per-workstream file falls back to the global one, filtered here. */
  function fetchDoctor(ws, severity) {
    var params = {};
    if (ws && ws !== "all") params.ws = ws;
    if (severity) params.severity = severity;
    function filterWs(j) {
      if (!j || !params.ws) return j;
      var keep = (j.problems || []).filter(function (p) {
        return p.scope === "global" || p.ws === params.ws || p.scope === "ws:" + params.ws;
      });
      var c = {critical: 0, warn: 0, info: 0, regressed: 0};
      keep.forEach(function (p) {
        if (c[p.severity] != null) c[p.severity]++;
        if (p.regressed) c.regressed++;
      });
      j.problems = keep;
      j.summary = c;
      return j;
    }
    function get(url) {
      return global.fetch(url).then(function (r) {
        if (!r.ok) throw new Error("doctor " + r.status);
        return r.json();
      });
    }
    if (fixtureMode()) {
      var name = "../fixtures/doctor" + (params.ws ? "-" + params.ws : "") + ".json";
      return get(name).catch(function () {
        if (!params.ws) throw new Error("doctor fixtures missing");
        return get("../fixtures/doctor.json").then(filterWs);
      });
    }
    if (global.DashApi) {
      return global.DashApi("/api/doctor", params).catch(function () {
        if (!params.ws) throw new Error("doctor unavailable");
        return global.DashApi("/api/doctor", {}).then(filterWs);
      });
    }
    var qs = Object.keys(params).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
    return get("/api/doctor" + (qs ? "?" + qs : ""));
  }

  var CSS = [
    ".doc-pill{font:700 12px var(--mono);border:1px solid var(--ring);border-radius:999px;padding:4px 11px;background:var(--surface);cursor:pointer;white-space:nowrap}",
    ".doc-pill[data-tone=good]{color:var(--good-ink,var(--good));border-color:var(--good)}",
    ".doc-pill[data-tone=warn]{color:var(--ink);border-color:var(--warning);background:color-mix(in srgb,var(--warning) 18%,var(--surface))}",
    ".doc-pill[data-tone=crit]{color:var(--critical);border-color:var(--critical);background:color-mix(in srgb,var(--critical) 12%,var(--surface))}",
    ".doc-pill[data-tone=muted]{color:var(--muted)}",
    ".doc-card{border-color:color-mix(in srgb,var(--s1) 35%,var(--ring))}",
    ".doc-sevhead{font:700 12px var(--mono);margin:10px 0 6px}",
    ".doc-sevhead.sev-critical{color:var(--critical)}",
    ".doc-sevhead.sev-warn{color:var(--ink)}",
    ".doc-sevhead.sev-info{color:var(--muted)}",
    ".doc-row{border:1px solid var(--ring);border-left-width:4px;border-radius:8px;padding:8px 10px;margin:6px 0;background:var(--surface-2)}",
    ".doc-row.sev-critical{border-left-color:var(--critical)}",
    ".doc-row.sev-warn{border-left-color:var(--warning)}",
    ".doc-row.sev-info{border-left-color:var(--muted)}",
    ".doc-top{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;font:600 11.5px var(--mono);color:var(--ink-2)}",
    ".doc-sev{font-weight:700}",
    ".doc-row.sev-critical .doc-sev{color:var(--critical)}",
    ".doc-row.sev-warn .doc-sev{color:var(--ink)}",
    ".doc-row.sev-info .doc-sev{color:var(--muted)}",
    ".doc-scope{border:1px solid var(--grid);border-radius:999px;padding:1px 8px}",
    ".doc-since{color:var(--muted)}",
    ".doc-reg{color:var(--critical);border:1px solid var(--critical);border-radius:4px;padding:0 5px}",
    ".doc-count{color:var(--muted)}",
    ".doc-title{font-weight:650;margin-top:4px}",
    ".doc-detail{font-size:13px;color:var(--ink-2);margin-top:2px}",
    ".doc-fix{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}",
    ".doc-fix code{font:12px var(--mono);background:var(--surface);border:1px solid var(--ring);border-radius:6px;padding:4px 8px;overflow-wrap:anywhere}",
    ".doc-fix .doc-copy{font-size:12px;padding:3px 10px;border-radius:999px}",
    ".doc-fix a{font-size:12.5px;color:var(--s1);text-decoration:none}",
    ".doc-resolved{margin-top:8px;font-size:13px;color:var(--ink-2)}",
    ".doc-resolved summary{cursor:pointer}",
    ".doc-res{margin:4px 0 4px 16px;color:var(--good-ink,var(--good))}",
    ".chip .docwarn{font-style:normal;color:var(--warning);margin-left:3px}",
  ].join("\n");

  function injectCss() {
    var d = doc();
    if (!d || !d.createElement || d.getElementById("doc-css")) return;
    var st = d.createElement("style");
    st.id = "doc-css";
    st.textContent = CSS;
    (d.head || d.body).appendChild(st);
  }

  try { injectCss(); } catch (e) {}

  global.DashDoctor = {
    counts: counts,
    pillFor: pillFor,
    pillTitle: pillTitle,
    showCard: showCard,
    wsLabel: wsLabel,
    taskProblemMap: taskProblemMap,
    relAgo: relAgo,
    copyText: copyText,
    updatePill: updatePill,
    healthCard: healthCard,
    fetchDoctor: fetchDoctor,
    injectCss: injectCss,
  };
})(typeof window !== "undefined" ? window : globalThis);
