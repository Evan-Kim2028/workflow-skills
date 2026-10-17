#!/usr/bin/env node
/* Render a workstream replay to MP4 and GIF.
 *
 * Drives the dashboard in headless Chrome over the DevTools protocol
 * (stdlib only: global fetch and WebSocket, child_process, no npm
 * installs). Opens the execution tab with the replay running
 * (&replay=1&autoplay=1), restarts it under Page.startScreencast, saves
 * the frames to a temp dir, then ffmpeg makes an H.264 MP4 (yuv420p) and
 * a palette GIF about 1000 px wide. Light theme by default, dark with
 * --dark. Also saves three still PNGs (start, middle, end).
 *
 * With --clip it records the promo clip composition instead (&clip=1 on
 * the page): title card, caption strip, counters, closing card, and the
 * map plus run timeline playing from one playhead. Frames are stepped
 * deterministically through window.__adReplay.seek at --fps (default 30),
 * so the capture is jitter-free. Viewport is 1920x1080, or 1080x1350
 * with --ar 4x5. Writes an H.264 MP4 plus (16x9 light only) a 1280 px
 * palette GIF, and stills at 0, 1.5, 5 and 10 s plus the last frame.
 *
 * usage: node scripts/render_replay.mjs --url http://127.0.0.1:8765 \
 *   --ws WS1 --out <dir> [--dark] [--seconds 15|30] [--fps 15] \
 *   [--width 1000] [--chrome PATH] [--ffmpeg PATH]
 * usage (clip): node scripts/render_replay.mjs --url URL --ws WS1 \
 *   --out <dir> --clip [--ar 16x9|4x5] [--dark] [--seconds 15|30] \
 *   [--fps 30] [--chrome PATH] [--ffmpeg PATH]
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const READY_TIMEOUT_MS = 45000;

function args(argv) {
  const out = {
    url: "http://127.0.0.1:8765", ws: "WS1", out: null, dark: false,
    seconds: 15, fps: null, width: 1000, chrome: null,
    ffmpeg: "/opt/homebrew/bin/ffmpeg", clip: false, ar: "16x9",
  };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--ws" && argv[i + 1]) out.ws = argv[++i];
    else if (flag === "--out" && argv[i + 1]) out.out = argv[++i];
    else if (flag === "--dark") out.dark = true;
    else if (flag === "--clip") out.clip = true;
    else if (flag === "--ar" && argv[i + 1]) out.ar = argv[++i];
    else if (flag === "--seconds" && argv[i + 1]) out.seconds = Number(argv[++i]);
    else if (flag === "--fps" && argv[i + 1]) out.fps = Number(argv[++i]);
    else if (flag === "--width" && argv[i + 1]) out.width = Number(argv[++i]);
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--ffmpeg" && argv[i + 1]) out.ffmpeg = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: render_replay.mjs --url URL --ws WS --out DIR [--dark] [--seconds 15|30] [--fps N] [--width PX] [--chrome PATH] [--ffmpeg PATH]");
      console.log("       render_replay.mjs --url URL --ws WS --out DIR --clip [--ar 16x9|4x5] [--dark] [--seconds 15|30] [--fps N] [--chrome PATH] [--ffmpeg PATH]");
      process.exit(0);
    } else {
      console.error(`unknown flag ${flag}`);
      process.exit(1);
    }
  }
  if (!out.out) {
    console.error("render_replay: --out DIR is required");
    process.exit(1);
  }
  if (out.seconds !== 15 && out.seconds !== 30) {
    console.error("render_replay: --seconds must be 15 or 30");
    process.exit(1);
  }
  if (out.clip && out.ar !== "16x9" && out.ar !== "4x5") {
    console.error("render_replay: --ar must be 16x9 or 4x5");
    process.exit(1);
  }
  if (out.fps == null) out.fps = out.clip ? 30 : 15;
  return out;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`render_replay: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  for (const dir of String(process.env.PATH || "").split(path.delimiter)) {
    for (const name of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
      const candidate = path.join(dir, name);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      } catch { /* next */ }
    }
  }
  console.error("render_replay: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return (async function poll() {
    for (;;) {
      try {
        const response = await fetch(url);
        if (response.ok) return await response.json();
      } catch { /* not up yet */ }
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${url}`);
      await new Promise((r) => setTimeout(r, 200));
    }
  })();
}

/* Minimal CDP client with unsolicited-event listeners: on("Page.screencastFrame", fn). */
function connectDebugger(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    const handlers = new Map();
    let id = 0;
    let settled = false;
    ws.addEventListener("open", () => {
      settled = true;
      resolve({ send, close, on });
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
        return;
      }
      if (msg.method && handlers.has(msg.method)) {
        for (const fn of handlers.get(msg.method)) {
          try {
            fn(msg.params || {});
          } catch { /* listener must not break the socket */ }
        }
      }
    });
    ws.addEventListener("error", (event) => {
      if (!settled) reject(new Error(`debugger socket error: ${event.message || "unknown"}`));
    });
    function on(method, fn) {
      if (!handlers.has(method)) handlers.set(method, []);
      handlers.get(method).push(fn);
    }
    function send(method, params, timeoutMs = 30000) {
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
      } catch { /* already closed */ }
    }
  });
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
  if (!result || !result.result) throw new Error(`cdp evaluate gave no result: ${JSON.stringify(result).slice(0, 200)}`);
  if (result.exceptionDetails) throw new Error(`page expression failed: ${JSON.stringify(result.exceptionDetails).slice(0, 400)}`);
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    let value = null;
    try {
      value = await evaluate(cdp, expression);
    } catch { /* page mid-navigation */ }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((r) => setTimeout(r, 400));
  }
}

function ffmpegRun(ffmpeg, argv) {
  const proc = spawnSync(ffmpeg, argv, { encoding: "utf8", timeout: 300000 });
  if (proc.error) throw proc.error;
  if (proc.status !== 0) throw new Error(`ffmpeg failed: ${(proc.stderr || "").slice(-2000)}`);
}

/* Clip render: deterministic stepping through the promo composition.
 *
 * Opens view=exec with replay+autoplay+clip, sets the playback length,
 * then seeks frame by frame (firing unblock highlights like a live
 * playback) and screenshots each step at exactly --fps. Encodes an H.264
 * MP4 (crf 18, yuv420p) and, for the 16x9 light clip, a 1280 px palette
 * GIF at a lower frame rate. Copies stills at 0, 1.5, 5 and 10 s plus
 * the last frame.
 */
async function renderClip(opts, chrome) {
  const vertical = opts.ar === "4x5";
  const vw = vertical ? 1080 : 1920, vh = vertical ? 1350 : 1080;
  const framesDir = fs.mkdtempSync(path.join(os.tmpdir(), "dash-clip-frames-"));
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-clip-profile-"));
  const base = opts.ws.toLowerCase().replace(/[^a-z0-9]+/g, "-")
    + "-clip-" + (vertical ? "4x5" : "16x9") + (opts.dark ? "-dark" : "");
  const mp4Path = path.join(opts.out, `${base}.mp4`);
  const gifPath = path.join(opts.out, `${base}.gif`);
  const wantGif = !vertical && !opts.dark;

  const child = spawn(chrome, [
    "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
    "--disable-extensions", "--hide-scrollbars",
    `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  let cdp = null;
  const finish = (code) => {
    try { if (cdp) cdp.close(); } catch { /* ignore */ }
    try { child.kill(); } catch { /* ignore */ }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));

  try {
    await waitForJson(`http://127.0.0.1:${port}/json/version`, 20000);
    const created = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: vw, height: vh, deviceScaleFactor: 1, mobile: false,
    });

    const clipFlag = vertical ? "4x5" : "1";
    const pageUrl = `${opts.url}/?view=exec&ws=${encodeURIComponent(opts.ws)}&replay=1&autoplay=1&clip=${clipFlag}${opts.dark ? "&theme=dark" : "&theme=light"}`;
    await cdp.send("Page.navigate", { url: pageUrl });
    await waitFor(cdp, "!!(window.__adReplay && window.__adReplay.ready && window.__adReplay.clipReady)", READY_TIMEOUT_MS, "clip replay data");
    await evaluate(cdp, `window.__adReplay.setDuration(${opts.seconds})`);

    const total = opts.fps * opts.seconds;
    for (let i = 0; i < total; i++) {
      const frac = total === 1 ? 1 : i / (total - 1);
      await evaluate(cdp, `window.__adReplay.seekFire(${frac})`);
      await new Promise((r) => setTimeout(r, 60));
      const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
      fs.writeFileSync(path.join(framesDir, `frame-${String(i).padStart(4, "0")}.png`), Buffer.from(shot.data, "base64"));
      if ((i + 1) % 90 === 0) console.log(`render_replay: clip frame ${i + 1}/${total}`);
    }

    for (const [at, name] of [[0, "t0"], [1.5, "t1-5"], [5, "t5"], [10, "t10"], [opts.seconds - 1 / opts.fps, "tend"]]) {
      const idx = Math.min(total - 1, Math.max(0, Math.round(at * opts.fps)));
      fs.copyFileSync(
        path.join(framesDir, `frame-${String(idx).padStart(4, "0")}.png`),
        path.join(opts.out, `${base}-still-${name}.png`),
      );
    }

    ffmpegRun(opts.ffmpeg, [
      "-y", "-framerate", String(opts.fps), "-i", path.join(framesDir, "frame-%04d.png"),
      "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
      "-movflags", "+faststart", mp4Path,
    ]);

    let gifBytes = 0;
    if (wantGif) {
      const gifFps = opts.fps > 15 ? 10 : opts.fps;
      const step = Math.max(1, Math.round(opts.fps / gifFps));
      const gifDir = fs.mkdtempSync(path.join(os.tmpdir(), "dash-clip-gif-"));
      try {
        let g = 0;
        for (let i = 0; i < total; i += step) {
          fs.copyFileSync(
            path.join(framesDir, `frame-${String(i).padStart(4, "0")}.png`),
            path.join(gifDir, `g-${String(g++).padStart(4, "0")}.png`),
          );
        }
        const gifScale = `fps=${gifFps},scale=1280:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse`;
        ffmpegRun(opts.ffmpeg, [
          "-y", "-framerate", String(gifFps), "-i", path.join(gifDir, "g-%04d.png"),
          "-vf", gifScale, gifPath,
        ]);
        gifBytes = fs.statSync(gifPath).size;
        if (gifBytes > 20 * 1024 * 1024) {
          ffmpegRun(opts.ffmpeg, [
            "-y", "-framerate", String(gifFps), "-i", path.join(gifDir, "g-%04d.png"),
            "-vf", `fps=8,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse`,
            gifPath,
          ]);
          gifBytes = fs.statSync(gifPath).size;
        }
      } finally {
        try { fs.rmSync(gifDir, { recursive: true, force: true }); } catch { /* keep the out dir */ }
      }
    }

    const mp4Bytes = fs.statSync(mp4Path).size;
    console.log(JSON.stringify({
      ws: opts.ws, clip: true, ar: opts.ar, dark: opts.dark, frames: total, fps: opts.fps,
      seconds: opts.seconds, viewport: `${vw}x${vh}`,
      mp4: mp4Path, mp4_bytes: mp4Bytes,
      gif: wantGif ? gifPath : null, gif_bytes: gifBytes,
      stills: [0, 1.5, 5, 10, "end"].map((_, i) => path.join(opts.out, `${base}-still-${["t0", "t1-5", "t5", "t10", "tend"][i]}.png`)),
    }, null, 2));
    if (mp4Bytes > 20 * 1024 * 1024) {
      console.error(`render_replay: warning: mp4 is ${(mp4Bytes / 1048576).toFixed(1)} MB, over the ~20 MB target`);
    }
    if (wantGif && gifBytes > 20 * 1024 * 1024) {
      console.error(`render_replay: warning: gif is ${(gifBytes / 1048576).toFixed(1)} MB, over the ~20 MB target`);
    }
    finish(0);
  } catch (err) {
    console.error(`render_replay: ${err && err.message ? err.message : err}`);
    finish(1);
  } finally {
    try { fs.rmSync(framesDir, { recursive: true, force: true }); } catch { /* keep the out dir */ }
  }
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  if (opts.clip) {
    if (!fs.existsSync(opts.ffmpeg)) {
      console.error(`render_replay: ffmpeg not found at ${opts.ffmpeg} (pass --ffmpeg PATH)`);
      process.exit(1);
    }
    fs.mkdirSync(opts.out, { recursive: true });
    await renderClip(opts, chrome);
    return;
  }
  if (!fs.existsSync(opts.ffmpeg)) {
    console.error(`render_replay: ffmpeg not found at ${opts.ffmpeg} (pass --ffmpeg PATH)`);
    process.exit(1);
  }
  fs.mkdirSync(opts.out, { recursive: true });
  const framesDir = fs.mkdtempSync(path.join(os.tmpdir(), "dash-replay-frames-"));
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-replay-profile-"));
  const base = opts.ws.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const mp4Path = path.join(opts.out, `${base}-replay.mp4`);
  const gifPath = path.join(opts.out, `${base}-replay.gif`);
  const stillPaths = {
    start: path.join(opts.out, `${base}-still-start.png`),
    middle: path.join(opts.out, `${base}-still-middle.png`),
    end: path.join(opts.out, `${base}-still-end.png`),
  };

  const child = spawn(chrome, [
    "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
    "--disable-extensions", "--hide-scrollbars",
    `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  let cdp = null;
  const finish = (code) => {
    try {
      if (cdp) cdp.close();
    } catch { /* ignore */ }
    try {
      child.kill();
    } catch { /* ignore */ }
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch { /* ignore */ }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));

  try {
    await waitForJson(`http://127.0.0.1:${port}/json/version`, 20000);
    const created = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1280, height: 860, deviceScaleFactor: 1, mobile: false,
    });

    const pageUrl = `${opts.url}/?view=exec&ws=${encodeURIComponent(opts.ws)}&replay=1&autoplay=1${opts.dark ? "&theme=dark" : "&theme=light"}`;
    await cdp.send("Page.navigate", { url: pageUrl });
    await waitFor(cdp, "!!(window.__adReplay && window.__adReplay.ready)", READY_TIMEOUT_MS, "replay data");
    if (opts.seconds === 30) {
      await evaluate(cdp, `(function(){var b=document.querySelectorAll(".rp-bar .seg-ctl button")[1];if(b)b.click();return 1;})()`);
    }

    const frames = [];
    let acking = Promise.resolve();
    cdp.on("Page.screencastFrame", (params) => {
      const stamp = Date.now();
      const data = Buffer.from(params.data, "base64");
      const sessionId = params.sessionId;
      acking = acking.then(() => cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {}));
      const index = frames.length;
      frames.push({ index, stamp, data });
    });
    await cdp.send("Page.startScreencast", { format: "jpeg", quality: 80, maxWidth: 1280, maxHeight: 860 });

    // Restart under the screencast so frame zero is the gray map.
    const t0 = Date.now();
    await evaluate(cdp, `(function(){var bs=document.querySelectorAll(".rp-bar > button");for(var i=0;i<bs.length;i++){if(bs[i].textContent==="Restart"){bs[i].click();return 1;}}return 0;})()`);

    // Stills: start, middle, end of the playback.
    await new Promise((r) => setTimeout(r, 700));
    const startShot = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(stillPaths.start, Buffer.from(startShot.data, "base64"));
    await new Promise((r) => setTimeout(r, Math.max(500, (opts.seconds * 1000) / 2 - 700)));
    const midShot = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(stillPaths.middle, Buffer.from(midShot.data, "base64"));

    await waitFor(cdp, "!!(window.__adReplay && window.__adReplay.done)", (opts.seconds + 30) * 1000, "replay finish");
    const elapsed = (Date.now() - t0) / 1000;
    await new Promise((r) => setTimeout(r, 600));
    const endShot = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(stillPaths.end, Buffer.from(endShot.data, "base64"));
    await cdp.send("Page.stopScreencast").catch(() => {});
    await acking;
    await new Promise((r) => setTimeout(r, 500));

    if (frames.length < 10) throw new Error(`only ${frames.length} screencast frames arrived; the animation may not have played`);
    frames.sort((a, b) => a.index - b.index);
    // Thin to about opts.fps so the video lasts as long as the playback did.
    const keepEvery = Math.max(1, Math.round(frames.length / Math.max(1, elapsed) / opts.fps));
    const kept = frames.filter((_, i) => i % keepEvery === 0);
    kept.forEach((frame, i) => {
      fs.writeFileSync(path.join(framesDir, `frame-${String(i).padStart(4, "0")}.jpg`), frame.data);
    });
    const fps = opts.fps;

    ffmpegRun(opts.ffmpeg, [
      "-y", "-framerate", String(fps), "-i", path.join(framesDir, "frame-%04d.jpg"),
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", `scale=${opts.width}:-2`,
      "-movflags", "+faststart", mp4Path,
    ]);

    const gifScale = `fps=${opts.fps},scale=${opts.width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse`;
    ffmpegRun(opts.ffmpeg, [
      "-y", "-framerate", String(fps), "-i", path.join(framesDir, "frame-%04d.jpg"),
      "-vf", gifScale, gifPath,
    ]);
    let gifBytes = fs.statSync(gifPath).size;
    if (gifBytes > 15 * 1024 * 1024) {
      ffmpegRun(opts.ffmpeg, [
        "-y", "-framerate", String(fps), "-i", path.join(framesDir, "frame-%04d.jpg"),
        "-vf", `fps=10,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse`,
        gifPath,
      ]);
      gifBytes = fs.statSync(gifPath).size;
    }

    const mp4Bytes = fs.statSync(mp4Path).size;
    console.log(JSON.stringify({
      ws: opts.ws, frames: kept.length, raw_frames: frames.length, wall_s: Math.round(elapsed * 10) / 10, fps,
      mp4: mp4Path, mp4_bytes: mp4Bytes,
      gif: gifPath, gif_bytes: gifBytes,
      stills: stillPaths,
    }, null, 2));
    if (gifBytes > 15 * 1024 * 1024) {
      console.error(`render_replay: warning: gif is ${(gifBytes / 1048576).toFixed(1)} MB, over the ~15 MB target`);
    }
    finish(0);
  } catch (err) {
    console.error(`render_replay: ${err && err.message ? err.message : err}`);
    finish(1);
  } finally {
    try {
      fs.rmSync(framesDir, { recursive: true, force: true });
    } catch { /* keep the out dir, drop scratch frames */ }
  }
}

main();
