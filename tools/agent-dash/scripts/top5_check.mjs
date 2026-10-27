#!/usr/bin/env node
/* Top-5 + unmapped-strip + outcome-colour check (stdlib only).
 *
 * Drives a TEMP dashboard (default http://127.0.0.1:8920 -- never the
 * live :8765) in headless Chrome over the DevTools protocol:
 *  - every capped list shows exactly 5 rows by default with a Show-all
 *    button, expands and collapses through it;
 *  - an expanded flag stored under the pre-top-5 key does not apply;
 *  - no "Running, not mapped" text anywhere;
 *  - blocked and died swatches differ (computed colours).
 *
 * Usage: node scripts/top5_check.mjs [base]   (keeps nothing running)
 * Exit 0 on PASS, 1 on the first failed assertion batch.
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:8920";
const CHROME = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "top5-check-"));
const prof = path.join(tmp, "prof");
const dbgPort = 9466;
let failures = 0;
function check(name, cond, detail = "") {
  console.log(`${cond ? "ok" : "FAIL"}  ${name}${detail ? " -- " + detail : ""}`);
  if (!cond) failures++;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ch = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${dbgPort}`,
  `--user-data-dir=${prof}`, "--window-size=1440,1600", "about:blank"], { stdio: "ignore" });
let tgt = null;
for (let i = 0; i < 60; i++) {
  try {
    const ts = await (await fetch(`http://127.0.0.1:${dbgPort}/json`)).json();
    tgt = ts.find((t) => t.type === "page");
    if (tgt) break;
  } catch {}
  await sleep(200);
}
if (!tgt) { console.error("SKIP: no Chrome target"); ch.kill(); process.exit(2); }
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pend = {};
ws.onmessage = (e) => {
  const m = JSON.parse(typeof e.data === "string" ? e.data : e.data.toString());
  if (m.id && pend[m.id]) pend[m.id](m.result);
};
const send = (method, params = {}) => new Promise((r) => { pend[++id] = r; ws.send(JSON.stringify({ id, method, params })); });
const ev = (expression, awaitPromise = false) => send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
const text = async (expr) => (await ev(expr)).result.value;
async function waitFor(expr, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    try { if (await text(expr)) return true; } catch {}
    if (Date.now() > end) return false;
    await sleep(500);
  }
}
await send("Page.enable");
await send("Runtime.enable");

async function cappedList(name, rowExpr, btnSel) {
  const q = `document.querySelector('${btnSel}')`;
  const btnExpr = `(function(){var b=${q};return b?b.textContent:null;})()`;
  const rows = await text(rowExpr);
  const btn = await text(btnExpr);
  let total = rows;
  if (btn) { const m = /Show all ([0-9]+)/.exec(btn); if (m) total = Number(m[1]); }
  console.log(`info  ${name}: total=${total} rows=${rows} btn=${btn}`);
  if (!(total > 5)) { check(`${name} short list has no button`, btn === null || btn === ""); return; }
  check(`${name} shows exactly 5 by default`, rows === 5, `rows=${rows} total=${total}`);
  check(`${name} button names Show all N`, typeof btn === "string" && btn.indexOf(`Show all ${total}`) === 0, btn);
  const exp0 = await text(`(function(){var b=${q};return b&&b.getAttribute('aria-expanded');})()`);
  check(`${name} button aria-expanded=false`, exp0 === "false", String(exp0));
  const ctl = await text(`(function(){var b=${q};return b&&b.getAttribute('aria-controls');})()`);
  check(`${name} button has aria-controls`, !!ctl, String(ctl));
  await ev(`(function(){var b=${q};if(b)b.click();})()`);
  await sleep(1500);
  const rowsOpen = await text(rowExpr);
  check(`${name} expands past 5`, rowsOpen > 5, `rows=${rowsOpen}`);
  const btnOpen = await text(btnExpr);
  check(`${name} expanded button says Show top 5`, typeof btnOpen === "string" && btnOpen.indexOf("Show top 5") === 0, btnOpen);
  await ev(`(function(){var b=${q};if(b)b.click();})()`);
  await sleep(1500);
  const rowsShut = await text(rowExpr);
  check(`${name} collapses back to 5`, rowsShut === 5, `rows=${rowsShut}`);
}

// ---- Overall Execution view ----
await send("Page.navigate", { url: `${BASE}/?view=exec&ws=all` });
check("exec view loads running rows", await waitFor(`document.querySelectorAll('#exec-running-rows tr').length>0`));
check("no 'not mapped to a task' on exec", await text(`document.body.textContent.indexOf('not mapped to a task')<0`));
await ev(`try{sessionStorage.setItem('agent-dash-running:all','all');}catch(e){}`);
await send("Page.reload", {});
check("exec reloads with old key set", await waitFor(`document.querySelectorAll('#exec-running-rows tr').length>0`));
await cappedList("running-now",
  `document.querySelectorAll('#exec-running-rows tr.clk').length`,
  `#exec-running .runmore`);
const runBtnCtl = await text(`(function(){var b=document.querySelector('#exec-running .runmore');return b&&b.getAttribute('aria-controls');})()`);
check("running button controls rows", runBtnCtl === "exec-running-rows", String(runBtnCtl));
await waitFor(`!!document.querySelector('.mg-disclose > .disclose-btn')`);
await ev(`(function(){var b=document.querySelector('.mg-disclose > .disclose-btn');if(b&&b.getAttribute('aria-expanded')==='false')b.click();})()`);
await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length > 0`);
await cappedList("manage-current",
  `document.querySelectorAll('[data-mg-rows] tr').length`,
  `[data-mg-more] .disclose-btn`);
const selAll = await text(`(function(){var c=document.querySelector('[data-mg-checkall]');return c&&c.getAttribute('aria-label');})()`);
check("select-all names the filter count", /Select all [0-9]+ workstreams in this filter/.test(selAll || ""), selAll);
await cappedList("where-we-are",
  `document.querySelectorAll('#prog-rows .prog-row').length`,
  `#prog-rows .disclose-btn`);
await cappedList("phase-usage",
  `document.querySelectorAll('#phase-usage-list .ph-row').length`,
  `.phase-usage .disclose-btn`);
/* The overview's pulse KPI strip owns the numbers now (the tiles stay on
   workstream tabs): the Finished KPI always states its 24 h window, so
   the check asserts on that cell rather than skipping when it is absent. */
const finCap = await text(`(function(){var x=document.querySelector('#x-pulse [data-kpi="finished"]');return x?x.textContent:'';})()`);
check("pulse finished KPI names its 24 h verdict window", /\(24 h\)|none in 24 h/.test(finCap || ""), String(finCap).slice(0, 160));
/* The run timeline legend is gone from the overview (the activity stack
   replaced it); the verdict colours now live on the pulse Finished bar,
   one span per verdict titled with its label. */
const swatches = await text(`(function(){var out={};document.querySelectorAll('#x-pulse [data-kpi="finished"] .pulse-stack span').forEach(function(s){var t=s.getAttribute('title')||'';var m=/blocked|died/.exec(t);if(m)out[m[0]]=getComputedStyle(s).backgroundColor;});return JSON.stringify(out);})()`);
console.log(`info  verdict swatches: ${swatches}`);
try {
  const sw = JSON.parse(swatches);
  check("blocked and died swatches differ", !!sw.blocked && !!sw.died && sw.blocked !== sw.died, `${sw.blocked} vs ${sw.died}`);
} catch { check("blocked and died swatches differ", false, swatches); }

// ---- WS9 tab: plan progress without the strip ----
await send("Page.navigate", { url: `${BASE}/?view=exec&ws=WS9` });
check("ws9 plan card loads", await waitFor(`!!document.querySelector('#exec-progress')`));
check("no 'not mapped to a task' on ws9", await text(`document.body.textContent.indexOf('not mapped to a task')<0`));
const headline = await text(`(function(){var h=document.querySelector('#exec-progress h2');return h?h.textContent:'';})()`);
const workingChips = await text(`document.querySelectorAll('#exec-progress .chip.live').length`);
if (workingChips > 0) check("ws9 headline names agents working", /agent/.test(headline), `${headline} live chips=${workingChips}`);
else console.log(`info  ws9 has no mapped live agents (headline: ${headline})`);
check("ws9 map legend works", await text(`!!document.querySelector('#exec-progress .map-legend')`));

// ---- Planned view ----
await send("Page.navigate", { url: `${BASE}/?view=planned` });
check("planned loads", await waitFor(`!!document.querySelector('.pj-ready, .pj-gated, .pj-empty-gated')`));
await cappedList("planned-gated",
  `document.querySelectorAll('#planned-gated-list .prog-row').length`,
  `.pj-gated .disclose-btn`);
await cappedList("planned-readiness",
  `document.querySelectorAll('#planned-ready-rows tr').length`,
  `.pj-ready .disclose-btn`);

// ---- Archive view ----
await send("Page.navigate", { url: `${BASE}/?view=archive` });
check("archive loads", await waitFor(`!!document.querySelector('.pj-arch-wrap')`));
const archCards = await text(`document.querySelectorAll('#archive-shelf-list .pj-arch').length`);
const archBtn = await text(`(function(){var b=document.querySelector('.pj-arch-wrap .disclose-btn');return b?b.textContent:null;})()`);
console.log(`info  archive: cards=${archCards} btn=${archBtn}`);
if (archCards > 5 || archBtn) {
  check("archive shows exactly 5 by default", archCards === 5, `cards=${archCards}`);
  check("archive button names Show all N", /Show all [0-9]+/.test(archBtn || ""), archBtn);
}

// ---- Project home ----
await send("Page.navigate", { url: `${BASE}/?view=project` });
check("project home loads", await waitFor(`!!document.querySelector('#home-planned-list, .pj-planned-wrap')`));
const planCards = await text(`document.querySelectorAll('#home-planned-list .pj-planned').length`);
const planBtn = await text(`(function(){var b=document.querySelector('.pj-planned-wrap .disclose-btn');return b?b.textContent:null;})()`);
console.log(`info  home-planned: cards=${planCards} btn=${planBtn}`);
if (planCards > 5 || planBtn) {
  check("home planned shelf shows exactly 5 by default", planCards === 5, `cards=${planCards}`);
  check("home planned button names Show all N", /Show all [0-9]+/.test(planBtn || ""), planBtn);
}
check("recently done keeps its pager",
  await text(`(function(){var f=document.querySelector('[data-pg-footer="recently-done"]');var rows=document.querySelectorAll('.pj-done .prog-row').length;return !f||rows<=10;})()`));
await cappedList("decisions",
  `document.querySelectorAll('#dec-list .dc-row').length`,
  `#exec-decisions .disclose-btn`);
check("no 'not mapped to a task' on project", await text(`document.body.textContent.indexOf('not mapped to a task')<0`));

try { ws.close(); } catch {}
ch.kill();
console.log(failures ? `RESULT FAIL (${failures})` : "RESULT PASS");
process.exit(failures ? 1 : 0);
