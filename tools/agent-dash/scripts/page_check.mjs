#!/usr/bin/env node
/* Browser regression check for token and cost coherence.
 *
 * Drives the live dashboard in headless Chrome over the DevTools protocol
 * (stdlib only: global fetch and WebSocket, child_process, no npm installs).
 * For the overview and every workstream tab at each range it waits for the
 * page to settle, reads the rendered tile and chart legend numbers from the
 * DOM data attributes, and compares them to /api/snapshot and /api/usage
 * for the same scope and range.
 *
 * Exit 0 when everything agrees, 1 with a table on any mismatch, 2 when
 * Chrome is missing (the agent_dash.py wrapper reports a skip then).
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const RANGES = [6, 12, 24, 72];
const SETTLE_POLL_MS = 700;
const COMBO_TIMEOUT_MS = 30000;

function args(argv) {
  const out = { url: "http://127.0.0.1:8765", chrome: null, ws: null, hours: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--ws" && argv[i + 1]) out.ws = argv[++i];
    else if (flag === "--hours" && argv[i + 1]) out.hours = Number(argv[++i]);
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: page_check.mjs [--url URL] [--chrome PATH] [--ws WS] [--hours N]");
      process.exit(0);
    } else {
      console.error(`unknown flag ${flag}`);
      process.exit(1);
    }
  }
  return out;
}

function which(names) {
  const dirs = String(process.env.PATH || "").split(path.delimiter);
  const suffixes = process.platform === "win32" ? [".exe", ".cmd", ""] : [""];
  for (const dir of dirs) {
    for (const name of names) {
      for (const suffix of suffixes) {
        const candidate = path.join(dir, name + suffix);
        try {
          fs.accessSync(candidate, fs.constants.X_OK);
          return candidate;
        } catch {
          /* next */
        }
      }
    }
  }
  return null;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`page_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("page_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
  process.exit(2);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${url}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

function connectDebugger(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let id = 0;
    let settled = false;
    ws.addEventListener("open", () => {
      settled = true;
      resolve({ send, close });
    });
    ws.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (msg.id != null && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
      }
    });
    ws.addEventListener("error", (event) => {
      if (!settled) reject(new Error(`debugger socket error: ${event.message || "unknown"}`));
    });
    function send(method, params, timeoutMs = 20000) {
      const cur = ++id;
      return new Promise((res, rej) => {
        pending.set(cur, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id: cur, method, params: params || {} }));
        setTimeout(() => {
          if (pending.has(cur)) {
            pending.delete(cur);
            rej(new Error(`cdp timeout: ${method}`));
          }
        }, timeoutMs);
      });
    }
    function close() {
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
  });
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
  if (!result || result.error) {
    throw new Error(`cdp evaluate gave no result (page is mid-navigation): ${JSON.stringify(result)}`);
  }
  if (result.exceptionDetails) {
    throw new Error(`page expression failed: ${JSON.stringify(result.exceptionDetails).slice(0, 400)}`);
  }
  return result.result ? result.result.value : undefined;
}

/* The click runs on a fresh document right after navigate, so it must be
   null-safe and retried: a throw here used to fail every first combo. */
async function clickRange(cdp, hours) {
  const expr = `(function(){var b=document.querySelector('#range button[data-h="${hours}"]');if(!b)return null;b.click();return 1;})()`;
  const deadline = Date.now() + COMBO_TIMEOUT_MS;
  for (;;) {
    let clicked = null;
    try {
      clicked = await evaluate(cdp, expr);
    } catch {
      clicked = null;
    }
    if (clicked === 1) return;
    if (Date.now() > deadline) throw new Error("range button never became clickable");
    await new Promise((r) => setTimeout(r, 300));
  }
}

async function waitForLoad(cdp) {
  const deadline = Date.now() + COMBO_TIMEOUT_MS;
  for (;;) {
    let state = null;
    try {
      state = await evaluate(cdp, "document.readyState");
    } catch {
      state = null;
    }
    if (state === "complete" || state === "interactive") return;
    if (Date.now() > deadline) throw new Error("page never finished loading");
    await new Promise((r) => setTimeout(r, 300));
  }
}

function readDomExpression(ws, hours) {
  return `(function(){
    var s = window.__adSettled || "";
    var parts = String(s).split("|");
    if (parts[0] !== ${JSON.stringify(ws)} || parts[1] !== ${JSON.stringify(String(hours))}) return null;
    var tile = document.querySelector('#exec-tiles [data-tile="tokens"]');
    if (!tile) return null;
    if (tile.getAttribute("data-ws") !== ${JSON.stringify(ws)}) return {bleed: tile.getAttribute("data-ws")};
    if (tile.getAttribute("data-hours") !== ${JSON.stringify(String(hours))}) return null;
    var cost = document.querySelector('#exec-tiles [data-tile="cost"]');
    var lg = document.querySelector('#exec-tokens .ltotal');
    var emptyChart = !lg && !!document.querySelector('#exec-tokens .empty');
    if (!lg && !emptyChart) return null;
    var xl = document.querySelector('#exec-tokens .xline');
    var cards = [];
    if (${JSON.stringify(ws)} === "all") {
      cards = Array.prototype.map.call(document.querySelectorAll('[data-wsc]'), function (c) {
        return {id: c.getAttribute("data-wsc"), tokens: c.getAttribute("data-tokens"), cost: c.getAttribute("data-cost-usd"), hours: c.getAttribute("data-hours")};
      });
    }
    return {settled: s,
      tile: {tokens: tile.getAttribute("data-tokens"), allTime: tile.getAttribute("data-tokens-all-time"), text: tile.textContent},
      cost: cost ? {usd: cost.getAttribute("data-cost-usd"), allTime: cost.getAttribute("data-cost-all-time")} : null,
      legend: lg ? {total: lg.getAttribute("data-legend-total"), chartSum: lg.getAttribute("data-chart-sum")} : {total: "0", chartSum: "0", empty: true},
      xline: xl ? xl.textContent : null,
      cards};
  })()`;
}

function billed(tokens) {
  return (tokens.input || 0) + (tokens.output || 0) + (tokens.cache_read || 0) + (tokens.cache_write || 0);
}

async function apiJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} -> ${response.status}`);
  return response.json();
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-page-check-"));
  const child = spawn(
    chrome,
    [
      "--headless",
      "--no-sandbox",
      "--disable-gpu",
      "--no-first-run",
      "--disable-extensions",
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let cdp = null;
  const rows = [];
  let failures = 0;
  const finish = (code) => {
    try {
      if (cdp) cdp.close();
    } catch {
      /* ignore */
    }
    try {
      child.kill();
    } catch {
      /* ignore */
    }
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));
  try {
    await waitForJson(`http://127.0.0.1:${port}/json/version`, 20000);
    const created = await (
      await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })
    ).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    const tabsExpression = `Array.prototype.map.call(document.querySelectorAll('#tabs .tab'), function (e) { return e.getAttribute("data-ws"); })`;
    await cdp.send("Page.navigate", { url: `${opts.url}/?view=exec&ws=all` });
    await waitForLoad(cdp);
    let tabs = [];
    {
      const deadline = Date.now() + COMBO_TIMEOUT_MS;
      for (;;) {
        try {
          tabs = ((await evaluate(cdp, tabsExpression)) || []).filter(Boolean);
        } catch {
          tabs = [];
        }
        if (tabs.length) break;
        if (Date.now() > deadline) {
          throw new Error("tabs never rendered with data-ws (is the page reachable from Chrome, and is the server running the token-coherence page?)");
        }
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    if (opts.ws) tabs = tabs.filter((t) => t === opts.ws);
    if (!tabs.length) throw new Error("no tabs matched");
    const ranges = opts.hours ? [opts.hours] : RANGES;

    for (const ws of tabs) {
      await cdp.send("Page.navigate", { url: `${opts.url}/?view=exec&ws=${encodeURIComponent(ws)}` });
      await waitForLoad(cdp);
      for (const hours of ranges) {
        const row = { ws, hours, tile: null, snap: null, legend: null, chart: null, cost: null, allTime: null, status: "OK", detail: "" };
        rows.push(row);
        const fail = (detail) => {
          row.status = "MISMATCH";
          row.detail = detail;
          failures++;
        };
        try {
          await clickRange(cdp, hours);
          const expr = readDomExpression(ws, hours);
          let seen = null;
          {
            const deadline = Date.now() + COMBO_TIMEOUT_MS;
            let stable = 0;
            let prev = "";
            for (;;) {
              let cur = null;
              try {
                cur = await evaluate(cdp, expr);
              } catch {
                cur = null;
              }
              if (cur && cur.settled) {
                const fingerprint = JSON.stringify([cur.settled, cur.tile, cur.legend]);
                if (fingerprint === prev) {
                  stable++;
                  if (stable >= 1) {
                    seen = cur;
                    break;
                  }
                } else {
                  stable = 0;
                  prev = fingerprint;
                }
              }
              if (Date.now() > deadline) {
                throw new Error(`page never settled for ${ws} ${hours}h`);
              }
              await new Promise((r) => setTimeout(r, SETTLE_POLL_MS));
            }
          }
          if (seen.bleed !== undefined && seen.tile === undefined) {
            fail(`cross-tab bleed: asked ${ws}, tile shows ${seen.bleed}`);
            continue;
          }
          const usageUrl = (sinceValue) =>
            `${opts.url}/api/usage?group=model&bucket=hour&since=${sinceValue}${ws === "all" ? "" : `&ws=${encodeURIComponent(ws)}`}`;
          const snapUrl = (sinceValue) =>
            `${opts.url}/api/snapshot?ws=${encodeURIComponent(ws === "all" ? "all" : ws)}&since=${sinceValue}`;
          const since = Math.floor(Date.now() / 1000 - hours * 3600);
          const [snap, usage] = await Promise.all([apiJson(snapUrl(since)), apiJson(usageUrl(since))]);
          const snapTok = billed(snap.totals.tokens);
          const tileTok = Number(seen.tile.tokens);
          const legendTot = Number(seen.legend.total);
          const chartSum = Number(seen.legend.chartSum);
          const usageSum = usage.buckets.reduce(
            (acc, b) => acc + (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0),
            0,
          );
          row.tile = tileTok;
          row.snap = snapTok;
          row.legend = legendTot;
          row.chart = chartSum;
          row.cost = Number(seen.cost ? seen.cost.usd : NaN);
          row.allTime = Number(seen.tile.allTime);
          if (tileTok !== snapTok) {
            // One retry: the collector may have written between the page fetch and ours.
            await new Promise((r) => setTimeout(r, 2500));
            const again = await evaluate(cdp, expr);
            const retrySince = Math.floor(Date.now() / 1000 - hours * 3600);
            const snap2 = await apiJson(snapUrl(retrySince));
            if (!again || !again.tile || Number(again.tile.tokens) !== billed(snap2.totals.tokens)) {
              fail(`tile ${tileTok} != snapshot ${snapTok} (retry: ${again && again.tile ? again.tile.tokens : "no tile"} vs ${billed(snap2.totals.tokens)})`);
              continue;
            }
            row.tile = Number(again.tile.tokens);
            row.snap = billed(snap2.totals.tokens);
          }
          if (Math.abs(Number(seen.cost ? seen.cost.usd : 0) - (snap.totals.cost_usd || 0)) > 1e-6) {
            fail(`cost tile ${seen.cost.usd} != snapshot ${snap.totals.cost_usd}`);
            continue;
          }
          if (row.allTime !== billed(snap.totals_all_time.tokens)) {
            fail(`tile all-time ${row.allTime} != snapshot ${billed(snap.totals_all_time.tokens)}`);
            continue;
          }
          if (legendTot !== tileTok) {
            fail(`legend ${legendTot} != tile ${tileTok} in the same render`);
            continue;
          }
          if (tileTok === 0) {
            // Zero with context: "0 in 6 h" plus the last run and all-time line.
            const text = (seen.tile.text || "").replace(/\s+/g, " ");
            const allTimeTok = billed(snap.totals_all_time.tokens);
            if (!text.includes(`0 in ${hours} h`)) {
              fail(`zero tile does not say "0 in ${hours} h": ${text.slice(0, 120)}`);
              continue;
            }
            if (allTimeTok > 0 && !/last run .* all time/.test(text)) {
              fail(`zero tile names no last run or all-time total: ${text.slice(0, 160)}`);
              continue;
            }
            if (allTimeTok === 0 && snap.last_activity !== null && snap.last_activity !== undefined) {
              fail(`tile shows no all-time tokens but the scope has activity`);
              continue;
            }
          }
          if (tileTok !== chartSum && !seen.xline) {
            fail(`tile ${tileTok} != chart ${chartSum} with no cross line shown`);
            continue;
          }
          if (tileTok === chartSum && seen.xline) {
            fail(`stale cross line while tile and chart agree at ${tileTok}`);
            continue;
          }
          if (chartSum !== usageSum) {
            // One retry: the usage fetch and the page render raced the collector.
            await new Promise((r) => setTimeout(r, 2500));
            const seenAgain = await evaluate(cdp, expr);
            const usageSince = Math.floor(Date.now() / 1000 - hours * 3600);
            const usage2 = await apiJson(usageUrl(usageSince));
            const chart2 = seenAgain && seenAgain.legend ? Number(seenAgain.legend.chartSum) : NaN;
            const usageSum2 = usage2.buckets.reduce(
              (acc, b) => acc + (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0),
              0,
            );
            if (chart2 !== usageSum2) {
              fail(`rendered chart sum ${chartSum} != usage API sum ${usageSum} (retry: ${chart2} vs ${usageSum2})`);
              continue;
            }
            row.chart = chart2;
          }
          if (ws === "all" && seen.cards && seen.cards.length) {
            const byWs = {};
            for (const r of snap.by_workstream) byWs[r.ws] = r;
            for (const card of seen.cards) {
              if (card.hours !== String(hours)) {
                fail(`overview card ${card.id} shows ${card.hours}h, asked ${hours}h`);
                break;
              }
              const hit = byWs[card.id] || (card.id === "Unassigned" ? byWs.unassigned : undefined);
              if (!hit) continue;
              if (Number(card.tokens) !== hit.tokens) {
                fail(`overview card ${card.id} ${card.tokens} != tab row ${hit.tokens}`);
                break;
              }
            }
            if (row.status !== "OK") continue;
          }
        } catch (error) {
          row.status = "ERROR";
          row.detail = String((error && error.message) || error);
          failures++;
        }
      }
    }
    if (!rows.length) throw new Error("no tab/range combinations ran");
  } catch (error) {
    console.error(`page_check failed: ${(error && error.message) || error}`);
    finish(1);
    return;
  }

  const width = Math.max(10, ...rows.map((r) => String(r.ws).length));
  console.log(`page check ${opts.url} (${rows.length} tab/range combinations)`);
  console.log(
    `${"workstream".padEnd(width)} ${"range".padStart(5)} ${"tile".padStart(12)} ${"snap".padStart(12)} ${"legend".padStart(12)} ${"chart".padStart(12)} ${"cost".padStart(10)} ${"status".padStart(8)}`,
  );
  for (const row of rows) {
    console.log(
      `${String(row.ws).padEnd(width)} ${(`${row.hours}h`).padStart(5)} ${String(row.tile ?? "-").padStart(12)} ${String(row.snap ?? "-").padStart(12)} ${String(row.legend ?? "-").padStart(12)} ${String(row.chart ?? "-").padStart(12)} ${String(row.cost ?? "-").padStart(10)} ${row.status.padStart(8)}`,
    );
    if (row.detail) console.log(`  - ${row.detail}`);
  }
  if (failures) {
    console.log(`${failures} mismatch(es)`);
    finish(1);
    return;
  }
  console.log(`ok: every rendered tile and legend matches the API`);
  finish(0);
}

main();
