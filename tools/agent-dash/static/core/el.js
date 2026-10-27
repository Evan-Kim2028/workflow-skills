/* Page core: DOM and text glue.
   Exports (on window.AdPage, also kept as plain globals so the staying page
   script and the card files keep calling every name unchanged): Q and FIX
   (the page query flags); modelName, fullName, tierKey, modelKey, modelLegend,
   colorOf, WSCOL, wsColor, cssv (model/workstream text and color glue);
   fmtDate, fmtN, fmtUsd, fmtDur, fmtT, axes, timeTicks, warpTime, warpPos
   (the format.js aliases both views share); el, s, NS (element builders);
   tip, showTip, hideTip (hover tooltip); copyBtn (also window.DashCopyBtn,
   used by the plan view); countUp (tile percent animation).
   Needs: window.ADM (static/models.js), window.AD (static/format.js), and
   the #tip node from the markup, which sits above the script tags. */
(function (global) {
  "use strict";
  var Q = new URLSearchParams(location.search);
  var FIX = Q.has("fixtures");
  /* Models: family = hue, model = shade of it (static/models.js). Keys look like grok-4.7, composer-2.5, glm-5.3-flash, haiku. */
  function modelName(k) { return ADM.nameOf(k); }
  /* Family-aware display lives in static/models.js (ADM.fullName) next to
     nameOf so both stay in one place; run rows pass their raw model id
     so the recorded version shows (Claude Haiku 5.5). */
  function fullName(x) { return ADM.fullName(x); }
  var WSCOL = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7"];
  var wsColor = {};
  function cssv(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function tierKey(r) { return ADM.info(r).key; }
  function modelKey(m) { return ADM.keyOf(m)[1]; }
  /* Legend grouped by family: "Cursor ■ Grok 4.7 ■ Composer 2.5".
     Runs without a recorded model stay out by default (lists simply
     omit them); pass the page scope to scope the shared toggle, or
     nothing to list every key. */
  function modelLegend(keys, counts, ws) {
    var showU = ws == null || typeof AdUnatt === "undefined" || AdUnatt.shown(ws);
    var lg = el("div", {class: "legend mlegend"}), byF = {};
    ADM.sortKeys(keys).filter(function (k) { return showU || !AdUnatt.isUnattributed(k); }).forEach(function (k) { var f = ADM.famOf(k); (byF[f] = byF[f] || []).push(k); });
    ADM.FAMS.forEach(function (f) {
      if (!byF[f]) return;
      var grp = el("span", {class: "mfam"}, [el("b", {text: ADM.FAM[f].name})]);
      byF[f].forEach(function (k) { var sp = el("span"); var sw = el("i", {class: "sw"}); sw.style.background = ADM.colorOf(k); sp.appendChild(sw); sp.appendChild(document.createTextNode(ADM.nameOf(k) + (counts && counts[k] != null ? " " + counts[k] : ""))); grp.appendChild(sp); });
      lg.appendChild(grp);
    });
    return lg;
  }
  function colorOf(key, kind) {
    /* The ignored-activity bucket is muted like unassigned, never an
       entity colour: it is a sum of hidden lanes, not a workstream. */
    if (kind === "workstream") { if (!key || key === "null" || key === "unassigned" || key === "Other") return "var(--other)"; if (!wsColor[key]) wsColor[key] = WSCOL[Object.keys(wsColor).length % WSCOL.length]; return "var(" + wsColor[key] + ")"; }
    return ADM.colorOf(key);
  }
  /* axes() and the compact formatters live in format.js and are shared with the plan view. */
  var fmtDate = AD.fmtDate, fmtN = AD.fmtN, fmtUsd = AD.fmtUsd, fmtDur = AD.fmtDur, fmtT = AD.fmtT, fmtPct = AD.fmtPct, fmtBytes = AD.fmtBytes, axes = AD.axes, timeTicks = AD.timeTicks, warpTime = AD.warpTime, warpPos = AD.warpPos;
  function el(tag, attrs, kids) { var e = document.createElement(tag); if (attrs) for (var k in attrs) { if (k === "text") e.textContent = attrs[k]; else if (k === "html") e.innerHTML = attrs[k]; else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]); else e.setAttribute(k, attrs[k]); } (kids || []).forEach(function (c) { if (c) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); }); return e; }
  var NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }
  /* tooltip */
  var tip = document.getElementById("tip");
  function showTip(ev, title, rows) {
    tip.innerHTML = ""; tip.appendChild(el("b", {text: title}));
    rows.forEach(function (r) { var d = el("div", {class: "r"}); var a = el("span"); if (r.c) { var i = el("i"); i.style.background = r.c; a.appendChild(i); } a.appendChild(document.createTextNode(r.k)); d.appendChild(a); d.appendChild(el("span", {text: r.v})); tip.appendChild(d); });
    tip.style.display = "block";
    var x = ev.clientX + 14, y = ev.clientY + 14, w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14; if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hideTip() { tip.style.display = "none"; }
  function copyBtn(text) {
    var b = el("button", {class: "copybtn", title: "Copy the paste-ready prompt that starts this workstream's lead in a fresh Claude Code session", text: "Copy start prompt"});
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var done = function () { b.textContent = "Copied \u2713"; b.classList.add("ok"); setTimeout(function () { b.textContent = "Copy start prompt"; b.classList.remove("ok"); }, 2000); };
      var fallback = function () { var d = el("div", {class: "copy-modal"}), ta = el("textarea", {readonly: "readonly"}); ta.value = text; var close = el("button", {text: "Close"}); close.addEventListener("click", function () { d.remove(); }); d.appendChild(el("p", {text: "Copy this prompt (Cmd+C):"})); d.appendChild(ta); d.appendChild(close); document.body.appendChild(d); ta.focus(); ta.select(); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
    });
    return b;
  }
  window.DashCopyBtn = copyBtn;
  function countUp(node, from, to) {
    if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches || from === to) { node.textContent = to + "%"; return; }
    var i = 0, n = 28, h = setInterval(function () { i++; var k = i / n, e = 1 - Math.pow(1 - k, 3); node.textContent = Math.round(from + (to - from) * e) + "%"; if (i >= n) clearInterval(h); }, 25);
    (window.__adCountUp = window.__adCountUp || []).push(h);
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.Q = Q;
  AdPage.FIX = FIX;
  AdPage.modelName = modelName;
  AdPage.fullName = fullName;
  AdPage.WSCOL = WSCOL;
  AdPage.wsColor = wsColor;
  AdPage.cssv = cssv;
  AdPage.tierKey = tierKey;
  AdPage.modelKey = modelKey;
  AdPage.modelLegend = modelLegend;
  AdPage.colorOf = colorOf;
  AdPage.fmtDate = fmtDate;
  AdPage.fmtN = fmtN;
  AdPage.fmtUsd = fmtUsd;
  AdPage.fmtDur = fmtDur;
  AdPage.fmtT = fmtT;
  AdPage.fmtPct = fmtPct;
  AdPage.fmtBytes = fmtBytes;
  AdPage.axes = axes;
  AdPage.timeTicks = timeTicks;
  AdPage.warpTime = warpTime;
  AdPage.warpPos = warpPos;
  AdPage.el = el;
  AdPage.NS = NS;
  AdPage.s = s;
  AdPage.tip = tip;
  AdPage.showTip = showTip;
  AdPage.hideTip = hideTip;
  AdPage.copyBtn = copyBtn;
  AdPage.countUp = countUp;
  global.Q = Q;
  global.FIX = FIX;
  global.modelName = modelName;
  global.fullName = fullName;
  global.WSCOL = WSCOL;
  global.wsColor = wsColor;
  global.cssv = cssv;
  global.tierKey = tierKey;
  global.modelKey = modelKey;
  global.modelLegend = modelLegend;
  global.colorOf = colorOf;
  global.fmtDate = fmtDate;
  global.fmtN = fmtN;
  global.fmtUsd = fmtUsd;
  global.fmtDur = fmtDur;
  global.fmtT = fmtT;
  global.fmtPct = fmtPct;
  global.fmtBytes = fmtBytes;
  global.axes = axes;
  global.timeTicks = timeTicks;
  global.warpTime = warpTime;
  global.warpPos = warpPos;
  global.el = el;
  global.NS = NS;
  global.s = s;
  global.tip = tip;
  global.showTip = showTip;
  global.hideTip = hideTip;
  global.copyBtn = copyBtn;
  global.countUp = countUp;
})(window);
