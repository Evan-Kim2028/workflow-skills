#!/usr/bin/env node
/* Shared table-column check: every table is compact, aligned, and wraps.
 *
 * Boots a TEMP server (port 8917, temp state dir, temp workstreams with
 * synthetic bundles -- never the owner's real bundles or :8765) holding
 * synthetic workstreams with very long titles, a very long section name,
 * long depends-on text, long run labels, and an archived card, then drives
 * the live page in headless Chrome over the DevTools protocol (stdlib
 * only). For every table on Execution (overall plus one workstream tab),
 * Planned, Archive, and Project home, at 1440 and 390 wide, it asserts:
 * the table uses the shared .dt class; no non-label column is wider than
 * 45 percent of its table; no table is wider than its card on desktop
 * (on phones a wider table must sit in a horizontal scroll container and
 * the page itself must not scroll sideways); no cell clips text without
 * wrapping or a tooltip; number cells align right and text cells left.
 * It prints the whitespace ratio (content over column widths) per table.
 *
 * Usage: node scripts/tables_check.mjs [--keep]   (keeps the temp dirs)
 * Exit 0 on PASS, 1 on the first failed assertion.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PORT = 8917;
const URL = `http://127.0.0.1:${PORT}`;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const LONG_WS = "A very long workstream title that keeps going past any reasonable column width so wrapping can be verified in the management table";
const LONG_PLAN = "Another extremely long planned workstream title exercising the readiness table label column wrapping behavior";
const LONG_SEC = "A very long plan section name that must wrap inside the section column";
const LONG_RUN = "tbl synthetic run label that runs on and on past every sensible width so the label column must wrap it instead of stretching the table wider and wider";
const SEC_RUN = "ship WS1.1 right now with some extra words after the task id";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tables-check-"));
const wsDir = path.join(tmp, "ws");
const stateDir = path.join(tmp, "state");
const homeDir = path.join(tmp, "home");
const emptyDir = path.join(tmp, "empty");
for (const d of [wsDir, stateDir, homeDir, emptyDir]) fs.mkdirSync(d, { recursive: true });
const keep = process.argv.includes("--keep");

const env = { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
  AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_DATA_ROOT: path.join(tmp, "root"),
  AGENT_DASH_PORT: String(PORT), HOME: homeDir,
  AGENT_DASH_CLAUDE_PROJECTS: emptyDir, AGENT_DASH_CLAUDE_SESSIONS: emptyDir,
  AGENT_DASH_CURSOR_HOME: emptyDir, AGENT_DASH_GROK_LOG: emptyDir,
  AGENT_DASH_GROK_SESSIONS: emptyDir, AGENT_DASH_MUSE_SESSIONS: emptyDir,
  AGENT_DASH_ISSUES_DIR: emptyDir, AGENT_DASH_PROJECTS: emptyDir };
let failures = 0;
function check(name, cond, detail = "") {
  console.log(`${cond ? "ok" : "FAIL"}  ${name}${detail ? " -- " + detail : ""}`);
  if (!cond) failures++;
}
function cli(...args) {
  const r = spawnSync("python3", ["agent_dash.py", ...args], { cwd: ROOT, env, encoding: "utf8" });
  if (r.status !== 0) { console.error(`cli failed: agent_dash.py ${args.join(" ")}\n${r.stdout}\n${r.stderr}`); process.exit(1); }
  return r.stdout;
}
function seedBundles() {
  cli("ws", "new", "WS1", LONG_WS);
  cli("ws", "new", "WS2", LONG_PLAN, "--state", "planned");
  cli("ws", "new", "WS3", "Short planned one", "--state", "planned");
  const b2 = path.join(wsDir, "WS2", "bundle.json");
  const meta = JSON.parse(fs.readFileSync(b2, "utf8"));
  meta.depends_on = ["WS1", "WS3"];
  fs.writeFileSync(b2, JSON.stringify(meta));
  const p1 = path.join(wsDir, "WS1", "plan.json");
  const plan = JSON.parse(fs.readFileSync(p1, "utf8"));
  plan.dag.groups[0].title = LONG_SEC;
  fs.writeFileSync(p1, JSON.stringify(plan));
  cli("ws", "new", "WS4", "Archive seed with a fairly long title for the key value card");
  const p4 = path.join(wsDir, "WS4", "plan.json");
  const plan4 = JSON.parse(fs.readFileSync(p4, "utf8"));
  for (const g of plan4.dag.groups) for (const t of g.tasks) t.status = "done";
  fs.writeFileSync(p4, JSON.stringify(plan4));
  fs.mkdirSync(path.join(wsDir, "WS4", "docs"), { recursive: true });
  fs.writeFileSync(path.join(wsDir, "WS4", "docs", "closing-report.md"), "# Closing report\n\nShipped.\n");
  cli("ws", "state", "WS4", "done");
  cli("ws", "archive", "WS4");
}
function seedRuns() {
  const now = Math.floor(Date.now() / 1000);
  const rows = [
    ["tbl-run-long-1", "tbl", "muse", "muse-spark-1.3", LONG_RUN, "WS1", "running", null, now - 400, null, 4096, 0],
    ["tbl-run-sec-2", "tbl", "muse", "muse-spark-1.3", SEC_RUN, "WS1", "running", null, now - 900, null, 8192, 0],
    ["tbl-run-done-3", "tbl", "muse", "muse-spark-1.3", "tbl short finished label", "WS1", "done", "PASS", now - 7200, now - 3600, 2048, 0],
    ["tbl-run-done-4", "tbl", "muse", "muse-spark-1.3", LONG_RUN + " second outing", "WS1", "done", "PARTIAL", now - 9000, now - 8000, 1024, 0],
  ];
  const script = "import sqlite3,sys; rows = " + JSON.stringify(rows).replace(/null/g, "None") + "; " +
    "c = sqlite3.connect(sys.argv[1]); " +
    "c.executemany('INSERT OR REPLACE INTO runs (run_id, source, tier, model, label, workstream, status, verdict, started_at, ended_at, out_bytes, err_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows); " +
    "c.commit(); print(c.execute('select count(*) from runs').fetchone()[0])";
  const r = spawnSync("python3", ["-c", script, path.join(stateDir, "dash.sqlite")],
    { encoding: "utf8" });
  if (r.status !== 0) throw new Error("run seeding failed\n" + r.stdout + "\n" + r.stderr);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try { const r = await fetch(url); if (r.ok) return await r.json(); } catch {}
    if (Date.now() > deadline) throw new Error("timeout " + url);
    await new Promise((r) => setTimeout(r, 300));
  }
}
function connectDebugger(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map(); let id = 0; let settled = false;
    ws.addEventListener("open", () => { settled = true; resolve({ send, close }); });
    ws.addEventListener("message", (ev) => {
      let msg; try { msg = JSON.parse(String(ev.data)); } catch { return; }
      if (msg.id != null && pending.has(msg.id)) {
        const e = pending.get(msg.id); pending.delete(msg.id);
        if (msg.error) e.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else e.resolve(msg.result);
      }
    });
    ws.addEventListener("error", () => { if (!settled) reject(new Error("socket error")); });
    function send(method, params, timeoutMs = 30000) {
      const cur = ++id;
      return new Promise((res, rej) => {
        pending.set(cur, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id: cur, method, params: params || {} }));
        setTimeout(() => { if (pending.has(cur)) { pending.delete(cur); rej(new Error("cdp timeout " + method)); } }, timeoutMs);
      });
    }
    function close() { try { ws.close(); } catch {} }
  });
}
async function ev(cdp, expr) {
  const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error("eval failed: " + JSON.stringify(r.exceptionDetails).slice(0, 200));
  return r.result ? r.result.value : undefined;
}
async function waitFor(cdp, expr, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await ev(cdp, expr).catch(() => null);
    if (v) return v;
    if (Date.now() > deadline) return null;
    await new Promise((r) => setTimeout(r, 500));
  }
}

/* Per-table measurements: column widths via header cells, content widths
   via a Range over each cell's contents (accurate for inline content),
   clipped cells (scroll overflow without a 2-line clamp or a tooltip),
   and the whitespace ratio (content over column widths). */
const MEASURE = `(() => {
  function contentW(td) {
    try {
      const range = document.createRange();
      range.selectNodeContents(td);
      return range.getBoundingClientRect().width;
    } catch (e) { return td.scrollWidth; }
  }
  const out = [];
  Array.from(document.querySelectorAll("table")).forEach((t, ti) => {
    const tr = t.getBoundingClientRect();
    const card = t.closest(".card") || t.parentElement;
    const cr = card ? card.getBoundingClientRect() : { width: 0 };
    const cs = card ? getComputedStyle(card) : null;
    const cardInner = card ? cr.width - (cs ? parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) : 0) : 0;
    const scrollBox = t.closest(".twrap,.mg-scroll,.tbl,.wsp-tbl,.pj-slot");
    const headCells = Array.from(t.querySelectorAll("thead th"));
    const cols = headCells.map((th) => {
      const idx = th.cellIndex;
      const rows = Array.from(t.querySelectorAll("tbody tr"));
      let content = 0;
      rows.forEach((row) => {
        const td = row.cells[idx];
        if (!td || (td.colSpan && td.colSpan > 1)) return;
        const w = contentW(td);
        if (w > content) content = w;
      });
      const colW = th.getBoundingClientRect().width;
      return { header: (th.textContent || "").trim().slice(0, 14), cls: th.className || "",
        colW: Math.round(colW), pct: tr.width ? +(100 * colW / tr.width).toFixed(1) : 0,
        contentW: Math.round(content) };
    });
    let clipped = 0, clippedNoTip = 0, sumContent = 0, sumCol = 0;
    cols.forEach((c) => { sumContent += c.contentW; sumCol += c.colW; });
    Array.from(t.querySelectorAll("tbody td")).forEach((td) => {
      if (td.colSpan && td.colSpan > 1) return;
      const target = td.firstElementChild || td;
      if (target.scrollWidth - target.clientWidth > 2) {
        clipped++;
        const st = getComputedStyle(target);
        const clamped = st.webkitLineClamp !== "none" && st.webkitLineClamp !== "";
        if (!clamped && !(td.title || target.title)) clippedNoTip++;
      }
    });
    const aligns = [];
    const isKv = (t.className || "").split(/\\s+/).indexOf("dt-kv") >= 0;
    Array.from(t.querySelectorAll("tbody td")).forEach((td) => {
      if (isKv) return;
      if (td.colSpan && td.colSpan > 1) return;
      const cls = td.className || "";
      const ta = getComputedStyle(td).textAlign;
      if (/\\bc-num\\b/.test(cls) || /\\bc-time\\b/.test(cls) || /\\bc-actions\\b/.test(cls)) aligns.push(ta === "right" ? 1 : 0);
      else if (/\\bc-text\\b/.test(cls) || /\\bc-label\\b/.test(cls) || /\\bc-status\\b/.test(cls)) aligns.push(ta === "left" ? 1 : 0);
    });
    out.push({
      label: t.getAttribute("aria-label") || t.className || ("table#" + ti),
      cls: t.className || "",
      hasDt: (t.className || "").split(/\\s+/).indexOf("dt") >= 0,
      tableW: Math.round(tr.width), cardInnerW: Math.round(cardInner),
      inScroll: !!scrollBox,
      cols, clippedCells: clipped, clippedNoTip,
      alignOk: aligns.length ? aligns.every(Boolean) : true,
      whitespaceRatio: sumCol ? +(sumContent / sumCol).toFixed(3) : 0
    });
  });
  return { tables: out,
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth };
})()`;

function isLabelCls(cls) {
  return /\bc-label\b/.test(cls || "") || /\bc-key\b/.test(cls || "") || (cls || "") === "";
}
async function settle(cdp) {
  const deadline = Date.now() + 30000;
  for (;;) {
    const s = await ev(cdp, "window.__adSettled || ''").catch(() => null);
    if (s && String(s).length > 0) break;
    if (Date.now() > deadline) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  await new Promise((r) => setTimeout(r, 1200));
}
async function main() {
  seedBundles();
  try {
    await fetch(URL + "/api/health");
    console.error("port 8917 is already in use; stop the other temp server first");
    process.exit(1);
  } catch {}
  const server = spawn("python3", ["agent_dash.py", "run"], { cwd: ROOT, env, stdio: "ignore" });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let pid = null;
  try {
    for (let i = 0; i < 100; i++) {
      await sleep(300);
      try {
        const h = await (await fetch(`${URL}/api/health`)).json();
        if (h.ok !== undefined) break;
      } catch {}
    }
    try { pid = Number(fs.readFileSync(path.join(stateDir, "agent-dash.pid"), "utf8").trim().split(/\s+/)[0]); } catch {}
    check("temp server answers on 8917", pid !== null, `pid ${pid}`);
    seedRuns();
    let seeded = false;
    for (let i = 0; i < 40; i++) {
      await sleep(500);
      try {
        const j = await (await fetch(`${URL}/api/runs?ws=all`)).json();
        if ((j.runs || []).some((r) => r.run_id === "tbl-run-long-1")) { seeded = true; break; }
      } catch {}
    }
    check("synthetic runs surface through /api/runs", seeded);

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tables-chrome-"));
    const ch = spawn(CHROME, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
      "--disable-extensions", `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`,
      "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
    const finishChrome = () => { try { ch.kill(); } catch {} try { fs.rmSync(profile, { recursive: true, force: true }); } catch {} };
    try {
      await waitForJson(`http://127.0.0.1:${dbgPort}/json/version`, 20000);
      const created = await (await fetch(`http://127.0.0.1:${dbgPort}/json/new?about:blank`, { method: "PUT" })).json();
      const cdp = await connectDebugger(created.webSocketDebuggerUrl);
      await cdp.send("Page.enable"); await cdp.send("Runtime.enable");
      const problems = [];
      await cdp.send("Runtime.evaluate", { expression: "1" }).catch((e) => problems.push(String(e)));
      for (const width of [1440, 390]) {
        const desktop = width === 1440;
        await cdp.send("Emulation.setDeviceMetricsOverride",
          desktop ? { width, height: 1000, deviceScaleFactor: 1, mobile: false }
                  : { width, height: 844, deviceScaleFactor: 1, mobile: true });
        for (const route of ["exec/all", "exec/WS1", "planned", "archive", "project"]) {
          const [view, ws] = route.split("/");
          await cdp.send("Page.navigate", { url: `${URL}/?view=${view}&ws=${ws || "all"}&theme=dark` });
          await settle(cdp);
          if (view === "exec") {
            /* Cards land as their fetches resolve: wait for the management
               and running tables (always seeded) before measuring, and
               require the finished table too (two seeded done rows). */
            await waitFor(cdp, "!!document.querySelector('.mg-disclose > .disclose-btn')", 20000);
            await ev(cdp, "(function(){var b=document.querySelector('.mg-disclose > .disclose-btn');if(b&&b.getAttribute('aria-expanded')==='false')b.click();})()");
            await waitFor(cdp, "document.querySelector('[data-manage] table')", 20000);
            await waitFor(cdp, "document.querySelector('#exec-running table')", 20000);
            check(`@${width} ${route} renders the finished table`,
              await waitFor(cdp, "document.querySelector('#exec-finished table')", 20000));
          }
          const m = await ev(cdp, MEASURE);
          console.log(`-- ${route} @${width}: ${m.tables.length} tables, sideways ${m.sideways}px`);
          if (route === "exec/all") {
            /* Live worker rows share the temp server, so the collapsed
               top-5 may hide the seeded rows: expand Running now first. */
            await ev(cdp, `(() => { const b = document.querySelector("#exec-running .runmore"); if (b && b.getAttribute("aria-expanded") === "false") b.click(); return true; })()`);
            await waitFor(cdp, `document.body.textContent.indexOf(${JSON.stringify(LONG_RUN.slice(0, 40))}) >= 0`, 15000);
            check(`@${width} exec overall holds the seeded long-label run`,
              await ev(cdp, `document.body.textContent.indexOf(${JSON.stringify(LONG_RUN.slice(0, 40))}) >= 0`));
            check(`@${width} exec overall holds the seeded long workstream title`,
              await ev(cdp, `document.body.textContent.indexOf(${JSON.stringify(LONG_WS.slice(0, 40))}) >= 0`));
          }
          if (route === "planned") {
            check(`@${width} readiness table lists both planned lanes`,
              await ev(cdp, "Array.from(document.querySelectorAll('table[aria-label=\"Start readiness\"] tbody tr')).length === 2"));
          }
          if (route === "archive") {
            check(`@${width} archive renders both key/value tables`,
              await ev(cdp, "document.querySelectorAll('table.pj-kv').length >= 2"));
          }
          check(`@${width} ${route}: page never scrolls sideways`, m.sideways <= 1, `overflow ${m.sideways}px`);
          for (const t of m.tables) {
            const tag = `@${width} ${route} [${t.label}]`;
            check(`${tag} uses the shared .dt class`, t.hasDt, t.cls);
            for (const c of t.cols) {
              if (isLabelCls(c.cls)) continue;
              check(`${tag} column "${c.header}" stays under 45%`, c.pct <= 45, `${c.pct}%`);
            }
            if (desktop) check(`${tag} fits its card`, t.tableW <= t.cardInnerW + 2, `${t.tableW} vs ${t.cardInnerW}`);
            else if (t.tableW > t.cardInnerW + 2) {
              check(`${tag} overflows only inside a scroll container`, t.inScroll, `${t.tableW} vs ${t.cardInnerW}`);
            }
            check(`${tag} aligns headers with cells`, t.alignOk);
            check(`${tag} never clips text without wrap or tooltip`, t.clippedNoTip === 0, `${t.clippedNoTip} cells`);
            if (t.cols.length) check(`${tag} keeps a sane whitespace ratio`, t.whitespaceRatio >= 0.15, String(t.whitespaceRatio));
            console.log(`   ratio ${t.whitespaceRatio} clipped ${t.clippedCells}/${t.clippedNoTip} widths ${t.tableW}/${t.cardInnerW}`);
          }
        }
      }
      check("zero CDP errors", problems.length === 0, problems.slice(0, 2).join(",") || "clean");
      cdp.close();
    } finally {
      finishChrome();
    }
  } finally {
    try { server.kill(); } catch {}
    try { if (pid) process.kill(pid); } catch {}
    await sleep(1500);
    try { if (pid) process.kill(pid, 9); } catch {}
    if (!keep) fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(failures === 0 ? "tables_check: PASS" : `tables_check: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => { console.error("tables_check errored:", err); process.exit(1); });
