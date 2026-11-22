#!/usr/bin/env node
/* Stream gate check: a page nobody looks at closes its live-update stream.
 *
 * Loads static/core/streamgate.js in a bare VM context (no browser, no
 * server) and drives it with a fake clock: hidden closes at once, a visible
 * unfocused page closes after IDLE_CLOSE_MS without input, a focused page
 * stays open, and focus, input or becoming visible reopens it.
 *
 * Usage: node scripts/stream_gate_check.mjs
 * Exit 0 on PASS, 1 on the first failed assertion.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source = fs.readFileSync(path.join(ROOT, "static", "core", "streamgate.js"), "utf8");
const sandbox = {};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const { AdStreamGate } = sandbox;

let failures = 0;
function check(name, cond) {
  if (!cond) { failures += 1; console.log(`FAIL ${name}`); } else { console.log(`ok   ${name}`); }
}

function harness() {
  const clock = { t: 0 };
  const events = [];
  const gate = AdStreamGate.create({
    now: () => clock.t,
    open: () => events.push("open"),
    close: () => events.push("close"),
  });
  return { clock, events, gate };
}

const IDLE = AdStreamGate.IDLE_CLOSE_MS;
check("idle window is ten minutes", IDLE === 600000);

{
  const { events, gate } = harness();
  gate.start(false, true);
  check("a visible focused page opens the stream", events.join() === "open");
  gate.visibility(true);
  check("hiding the page closes it at once", events.join() === "open,close");
  gate.visibility(false);
  check("showing it again reopens it", events.join() === "open,close,open");
}

{
  const { clock, events, gate } = harness();
  gate.start(false, true);
  clock.t = IDLE * 5;
  gate.check();
  check("a focused page stays open however long it sits", gate.isOpen() && events.join() === "open");
}

{
  const { clock, events, gate } = harness();
  gate.start(false, false);
  clock.t = IDLE - 1;
  gate.check();
  check("an unfocused page stays open inside the idle window", gate.isOpen());
  clock.t = IDLE;
  gate.check();
  check("an unfocused page with no input closes at the idle window", !gate.isOpen() && events.join() === "open,close");
  clock.t = IDLE + 5;
  gate.input();
  check("input reopens it", gate.isOpen() && events.join() === "open,close,open");
  clock.t = IDLE * 3;
  gate.check();
  check("it closes again after another idle window", !gate.isOpen());
  gate.focus(true);
  check("focus reopens it", gate.isOpen());
}

{
  const { events, gate } = harness();
  gate.start(true, true);
  check("a page that starts hidden never opens", events.length === 0 && !gate.isOpen());
  gate.input();
  check("input on a hidden page does not open it", !gate.isOpen());
}

if (failures) { console.log(`FAIL ${failures} assertion(s)`); process.exit(1); }
console.log("PASS stream gate");
