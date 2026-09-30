#!/usr/bin/env node
/**
 * Records the demo videos listed in demo/USER_STORIES.md against the live stack.
 *
 *   node demo/record_demo.mjs            # all six videos
 *   node demo/record_demo.mjs 3 5        # only videos 3 and 5
 *
 * Needs main.py, the API (:8000) and `npm run dev` in ../charger-fe (:3000) running, the demo
 * seeded, and run_demo_fleet.py NOT running: this starts its own fleet, so it can show the fleet's
 * console, and stops it at the end (DEMO_KEEP_FLEET=1 leaves it running). Video 6 restarts
 * main.py. Every operator step runs the real operate.py command and shows its real output.
 *
 * Output: demo/videos/NN-name.mp4, 1920x1080 H.264. Frames come from Chrome's screencast and are
 * encoded afterwards at high quality: Playwright's own recorder caps the bitrate too low for
 * readable text. Env: DEMO_PACE (pause multiplier, default 1), DEMO_FFMPEG, DEMO_PYTHON.
 */

import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileP = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHARGERS = path.resolve(HERE, "..");
const FE = path.resolve(CHARGERS, "..", "charger-fe");
const { chromium } = createRequire(path.join(FE, "package.json"))("@playwright/test");

const APP_URL = process.env.DEMO_APP_URL ?? "http://localhost:3000/";
const API = process.env.DEMO_API_URL ?? "http://localhost:8000/api/v1";
const PYTHON = process.env.DEMO_PYTHON ?? path.join(CHARGERS, "venv", "bin", "python");
const FFMPEG = process.env.DEMO_FFMPEG ?? "ffmpeg";
const PACE = Number(process.env.DEMO_PACE ?? 1);
const OUT_DIR = path.join(HERE, "videos");
const LOG_DIR = path.join(HERE, "logs");
// Recorded at 1920x1080, one CSS pixel per video pixel: headless Chrome's screencast ignores a
// device scale factor. The page's text is enlarged to 133% instead (the root font size, which
// every Tailwind size is relative to), as browser zoom would when presenting; the map canvas is
// untouched, so pointer positions stay exact.
const VIEWPORT = { width: 1920, height: 1080 };
const VIDEO = { width: 1920, height: 1080, fps: 30 };
// The GPU (through Vulkan) renders the map at ~25 fps; software WebGL manages ~7.
const BROWSER_ARGS = process.env.DEMO_NO_GPU === "1" ? [] : [
  "--use-angle=vulkan", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-features=Vulkan",
];
// Where a flown-to site lands: left of centre, clear of the 512 px station panel on the right.
const FLY_OFFSET = [-270, 25];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const hold = (ms) => sleep(ms * PACE);

function readDotenv() {
  try {
    return Object.fromEntries(
      fs.readFileSync(path.join(CHARGERS, ".env"), "utf8").split("\n")
        .filter((line) => line.includes("=") && !line.trimStart().startsWith("#"))
        .map((line) => [line.slice(0, line.indexOf("=")).trim(), line.slice(line.indexOf("=") + 1).trim()]),
    );
  } catch {
    return {};
  }
}
const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? readDotenv().ADMIN_TOKEN;

async function api(pathname) {
  const response = await fetch(API + pathname);
  if (!response.ok) throw new Error(`GET ${pathname} -> HTTP ${response.status}`);
  return response.json();
}

async function waitFor(predicate, timeoutMs, what, pollMs = 250) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await sleep(pollMs);
  }
  throw new Error(`timed out after ${timeoutMs / 1000}s waiting for ${what}`);
}

// --- the on-screen overlay: captions, cards, terminal, pointer, highlights ----------------------

/** Runs in the page (addInitScript). Everything is pointer-events: none, so the app underneath
 * behaves exactly as it does for a real user. Mounted on <html>, outside React's <body>. */
function installOverlay() {
  if (window.__demo) return;
  // Sized for a 1920x1080 page. html's font size enlarges the app itself (see VIEWPORT).
  const CSS = `
    html { font-size: 133.333% !important; }
    nextjs-portal { display: none !important; }
    #demo-root { position: fixed; inset: 0; pointer-events: none; z-index: 2147483000;
      font-family: var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif; }
    #demo-root * { box-sizing: border-box; }
    #demo-header { position: absolute; top: 18px; left: 50%; transform: translateX(-50%);
      background: rgba(17,24,39,.88); color: #fff; font-size: 19px; font-weight: 600;
      padding: 9px 22px; border-radius: 999px; opacity: 0; transition: opacity .4s; white-space: nowrap; }
    #demo-counter { position: absolute; top: 74px; left: 50%; transform: translateX(-50%);
      background: rgba(255,255,255,.96); color: #111827; font-size: 22px; font-weight: 600;
      padding: 12px 24px; border-radius: 16px; box-shadow: 0 8px 26px rgba(0,0,0,.25);
      display: none; gap: 24px; white-space: nowrap; }
    #demo-counter.on { display: flex; }
    #demo-counter span { display: inline-flex; align-items: center; gap: 9px; }
    #demo-counter i, #demo-legend i { display: inline-block; width: 18px; height: 18px; border-radius: 50%;
      border: 3px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,.25); }
    #demo-caption { position: absolute; left: 32px; bottom: 32px; max-width: 1320px; display: flex;
      gap: 18px; align-items: flex-start; background: rgba(17,24,39,.93); color: #fff;
      padding: 21px 29px; border-radius: 18px; box-shadow: 0 12px 36px rgba(0,0,0,.35);
      opacity: 0; transform: translateY(12px); transition: opacity .35s, transform .35s; }
    #demo-caption.on { opacity: 1; transform: none; }
    #demo-root.right #demo-caption, #demo-root.right #demo-term { left: auto; right: 32px; }
    #demo-step { flex: none; background: #22c55e; color: #052e16; font-weight: 800; font-size: 20px;
      border-radius: 10px; padding: 5px 13px; margin-top: 4px; }
    #demo-text { font-size: 30px; line-height: 1.35; font-weight: 650; }
    #demo-detail { display: block; font-size: 23px; font-weight: 400; color: #d1d5db; margin-top: 8px; }
    #demo-term { position: absolute; left: 32px; bottom: 205px; width: 900px; background: #0b1020;
      color: #e5e7eb; border-radius: 16px; box-shadow: 0 12px 36px rgba(0,0,0,.45); overflow: hidden;
      opacity: 0; transition: opacity .35s; }
    #demo-term.on { opacity: 1; }
    #demo-term .bar { background: #1f2937; color: #9ca3af; font-size: 16px; padding: 8px 16px;
      font-family: var(--font-geist-sans), sans-serif; }
    #demo-term-body { margin: 0; padding: 16px 19px; font-size: 19.5px; line-height: 1.5; min-height: 94px;
      font-family: var(--font-geist-mono), ui-monospace, Menlo, Consolas, monospace;
      white-space: pre-wrap; word-break: break-word; }
    #demo-term-body .cmd { color: #86efac; }
    #demo-term-body .cmd::before { content: "$ "; color: #9ca3af; }
    #demo-term-body .dim { color: #9ca3af; }
    #demo-term-body .out { color: #f3f4f6; }
    #demo-term-body .ok { color: #fde68a; }
    #demo-legend { position: absolute; left: 32px; top: 100px; background: rgba(255,255,255,.96);
      color: #111827; border-radius: 16px; padding: 16px 22px; font-size: 20px; line-height: 1.9;
      box-shadow: 0 8px 26px rgba(0,0,0,.25); opacity: 0; transition: opacity .4s; }
    #demo-legend.on { opacity: 1; }
    #demo-legend .t { font-weight: 700; font-size: 17px; text-transform: uppercase; color: #6b7280;
      letter-spacing: .05em; }
    #demo-legend div { display: flex; align-items: center; gap: 12px; }
    #demo-card { position: absolute; inset: 0; background: rgba(15,23,42,.96); color: #fff;
      display: flex; flex-direction: column; justify-content: center; padding: 0 200px;
      opacity: 0; transition: opacity .6s; }
    #demo-card.on { opacity: 1; }
    #demo-card .kicker { color: #86efac; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
      font-size: 22px; }
    #demo-card h1 { font-size: 62px; line-height: 1.15; margin: 18px 0 26px; font-weight: 800; max-width: 1450px; }
    #demo-card .story { font-size: 31px; color: #d1d5db; max-width: 1350px; line-height: 1.5; font-style: italic; }
    #demo-card ol { margin: 36px 0 0 0; padding-left: 38px; font-size: 28px; color: #e5e7eb; line-height: 1.75; max-width: 1400px; }
    #demo-card .foot { margin-top: 44px; font-size: 20px; color: #9ca3af; }
    #demo-cursor { position: absolute; left: 0; top: 0; width: 40px; height: 40px; margin: -4px 0 0 -5px;
      transform: translate(-200px, -200px); filter: drop-shadow(0 3px 4px rgba(0,0,0,.45)); }
    #demo-card.on ~ #demo-cursor { opacity: 0; }
    .demo-ripple { position: absolute; width: 62px; height: 62px; margin: -31px 0 0 -31px; border-radius: 50%;
      border: 5px solid #f59e0b; animation: demo-ripple .8s ease-out forwards; }
    @keyframes demo-ripple { from { transform: scale(.3); opacity: 1; } to { transform: scale(1.7); opacity: 0; } }
    .demo-hl { position: absolute; border: 5px solid #f59e0b; border-radius: 12px;
      box-shadow: 0 0 0 4px rgba(255,255,255,.85), 0 0 28px rgba(245,158,11,.75); animation: demo-pop .45s ease-out; }
    .demo-hl.ring { border-radius: 50%; }
    @keyframes demo-pop { from { transform: scale(1.25); opacity: 0; } to { transform: none; opacity: 1; } }
  `;
  const html = `
    <div id="demo-header"></div>
    <div id="demo-counter"></div>
    <div id="demo-legend"><div class="t">Live status</div>
      <div><i style="background:#22c55e"></i>A connector is free</div>
      <div><i style="background:#f59e0b"></i>Every connector is busy</div>
      <div><i style="background:#8b5cf6"></i>Reserved</div>
      <div><i style="background:#ef4444"></i>Broken</div>
      <div><i style="background:#6b7280"></i>Switched off</div></div>
    <div id="demo-term"><div class="bar" id="demo-term-title">terminal</div><div id="demo-term-body"></div></div>
    <div id="demo-caption"><span id="demo-step"></span><span><span id="demo-text"></span><span id="demo-detail"></span></span></div>
    <div id="demo-card"></div>
    <svg id="demo-cursor" viewBox="0 0 24 24"><path d="M3 2 L3 19 L7.6 14.8 L10.6 21.4 L13.7 20.1 L10.7 13.6 L16.9 13.6 Z"
      fill="#111827" stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  const $ = (id) => document.getElementById(id);
  function mount() {
    if ($("demo-root")) return;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.documentElement.appendChild(style);
    const root = document.createElement("div");
    root.id = "demo-root";
    root.innerHTML = html;
    document.documentElement.appendChild(root);
    window.addEventListener("mousemove", (e) => {
      $("demo-cursor").style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
    }, true);
    window.addEventListener("mousedown", (e) => {
      const ripple = document.createElement("div");
      ripple.className = "demo-ripple";
      ripple.style.left = `${e.clientX}px`;
      ripple.style.top = `${e.clientY}px`;
      root.appendChild(ripple);
      setTimeout(() => ripple.remove(), 900);
    }, true);
    window.__demo.ready = true;
  }
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  window.__demo = {
    ready: false,
    header(text) {
      $("demo-header").textContent = text || "";
      $("demo-header").style.opacity = text ? 1 : 0;
    },
    caption(step, text, detail) {
      $("demo-step").textContent = step ? `Step ${step}` : "";
      $("demo-step").style.display = step ? "" : "none";
      $("demo-text").textContent = text;
      $("demo-detail").textContent = detail || "";
      $("demo-detail").style.display = detail ? "" : "none";
      $("demo-caption").classList.add("on");
    },
    hideCaption() { $("demo-caption").classList.remove("on"); },
    layout(side) { $("demo-root").classList.toggle("right", side === "right"); },
    card({ kicker, title, story, bullets, foot }) {
      const card = $("demo-card");
      card.replaceChildren(el("div", "kicker", kicker), el("h1", null, title));
      if (story) card.appendChild(el("div", "story", story));
      if (bullets?.length) {
        const list = el("ol");
        for (const bullet of bullets) list.appendChild(el("li", null, bullet));
        card.appendChild(list);
      }
      if (foot) card.appendChild(el("div", "foot", foot));
      card.classList.add("on");
    },
    hideCard() { $("demo-card").classList.remove("on"); },
    legend(on) { $("demo-legend").classList.toggle("on", on); },
    counter(items) {
      const box = $("demo-counter");
      if (!items) { box.classList.remove("on"); return; }
      box.replaceChildren(...items.map(({ color, label }) => {
        const span = el("span");
        if (color) { const dot = el("i"); dot.style.background = color; span.appendChild(dot); }
        span.appendChild(document.createTextNode(label));
        return span;
      }));
      box.classList.add("on");
    },
    termShow(title) { $("demo-term-title").textContent = title; $("demo-term").classList.add("on"); },
    termHide() { $("demo-term").classList.remove("on"); },
    termClear() { $("demo-term-body").replaceChildren(); },
    termLine(cls, text = "") {
      const body = $("demo-term-body");
      body.appendChild(el("div", cls, text));
      while (body.childElementCount > 9) body.firstElementChild.remove();
    },
    termAppend(text) { $("demo-term-body").lastElementChild.textContent += text; },
    termSetLast(text) { $("demo-term-body").lastElementChild.textContent = text; },
    highlight(rect, ring) {
      const box = el("div", ring ? "demo-hl ring" : "demo-hl");
      Object.assign(box.style, {
        left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`,
      });
      document.getElementById("demo-root").appendChild(box);
    },
    clearHighlights() { document.querySelectorAll(".demo-hl").forEach((node) => node.remove()); },
  };
  if (document.readyState === "complete") mount();
  else window.addEventListener("load", mount);
}

// --- screen recording ---------------------------------------------------------------------------

/** Chrome's screencast sends a frame only when the screen changes, with its timestamp; each frame
 * is kept on screen until the next one, so the video runs in real time. */
class Recording {
  static async start(page) {
    const recording = new Recording();
    recording.dir = fs.mkdtempSync(path.join(os.tmpdir(), "demo-frames-"));
    recording.frames = [];
    recording.client = await page.context().newCDPSession(page);
    recording.client.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
      const file = path.join(recording.dir, `f${String(recording.frames.length).padStart(6, "0")}.jpg`);
      fs.writeFileSync(file, Buffer.from(data, "base64"));
      recording.frames.push({ file, t: metadata.timestamp });
      recording.client.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
    });
    await recording.client.send("Page.startScreencast", {
      format: "jpeg", quality: 90, maxWidth: VIDEO.width, maxHeight: VIDEO.height,
    });
    return recording;
  }

  async stop(outFile) {
    const end = Date.now() / 1000;
    await this.client.send("Page.stopScreencast");
    await sleep(300);
    if (this.frames.length === 0) throw new Error("no frames were captured");
    const lines = [];
    this.frames.forEach((frame, index) => {
      const next = this.frames[index + 1]?.t ?? end;
      lines.push(`file '${frame.file}'`, `duration ${Math.max(0.001, next - frame.t).toFixed(4)}`);
    });
    lines.push(`file '${this.frames.at(-1).file}'`); // the concat demuxer ignores the last duration otherwise
    const list = path.join(this.dir, "frames.txt");
    fs.writeFileSync(list, lines.join("\n") + "\n");
    await execFileP(FFMPEG, [
      "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list,
      "-vf", `fps=${VIDEO.fps},scale=${VIDEO.width}:${VIDEO.height}:flags=lanczos,format=yuv420p`,
      "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", outFile,
    ], { maxBuffer: 1 << 26 });
    const seconds = end - this.frames[0].t;
    fs.rmSync(this.dir, { recursive: true, force: true });
    return { frames: this.frames.length, seconds };
  }
}

// --- processes: the fleet and the Central System ------------------------------------------------

function processes() {
  return execFileSync("ps", ["-eo", "pid=,args="], { encoding: "utf8" }).split("\n")
    .map((line) => line.trim()).filter(Boolean)
    .map((line) => ({ pid: Number(line.split(/\s+/, 1)[0]), args: line.slice(line.indexOf(" ") + 1) }));
}

const isAlive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

/** Start a Python script detached, logging to `log`. SIGINT is reset to its default first: a
 * process started in the background inherits it ignored, and Ctrl+C is how both scripts stop. */
function spawnPython(script, log) {
  const fd = fs.openSync(log, "a");
  const child = spawn(PYTHON, [
    "-c",
    "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); "
      + `os.execv(sys.executable, [sys.executable, '-u', '${script}'])`,
  ], { cwd: CHARGERS, detached: true, stdio: ["ignore", fd, fd] });
  child.unref();
  fs.closeSync(fd);
  return child.pid;
}

async function interrupt(pid, what, timeoutMs = 30000) {
  process.kill(pid, "SIGINT");
  await waitFor(() => !isAlive(pid), timeoutMs, `${what} to exit`, 100);
}

function readLines(file, fromByte = 0) {
  if (!fs.existsSync(file)) return [];
  // Slice the bytes, then decode: marks are byte offsets, and the logs hold multi-byte "·".
  return fs.readFileSync(file).subarray(fromByte).toString("utf8").split("\n").filter(Boolean);
}

class Fleet {
  constructor() {
    this.log = path.join(LOG_DIR, "fleet.log");
    this.pid = null;
  }

  static runningElsewhere() {
    return processes().some((p) => p.args.includes("run_demo_fleet.py"));
  }

  /** Byte offset into the log: lines written after this are this run's. */
  mark() { return fs.existsSync(this.log) ? fs.statSync(this.log).size : 0; }

  start() {
    this.startMark = this.mark();
    this.pid = spawnPython("run_demo_fleet.py", this.log);
    return this.startMark;
  }

  async stop() {
    if (this.pid && isAlive(this.pid)) await interrupt(this.pid, "the fleet");
    this.pid = null;
  }

  lines(fromByte) { return readLines(this.log, fromByte); }

  async waitForLine(pattern, fromByte, timeoutMs) {
    return waitFor(() => this.lines(fromByte).find((line) => pattern.test(line)), timeoutMs,
      `fleet output ${pattern}`, 500);
  }
}

function centralSystemPid() {
  const found = processes().find((p) => /python\S*\s+(-u\s+)?main\.py$/.test(p.args));
  return found?.pid ?? null;
}

async function centralSystemUp() {
  return new Promise((resolve) => {
    const socket = net.connect(9000, "127.0.0.1");
    socket.once("connect", () => { socket.destroy(); resolve(true); });
    socket.once("error", () => resolve(false));
  });
}

// --- data lookups -------------------------------------------------------------------------------

async function siteNamed(name) {
  const site = (await api("/sites")).find((s) => s.name === name);
  if (!site) throw new Error(`no site named ${name}`);
  return site;
}

/** A simulated site with exactly one charger and one connector, Available right now. */
async function freeSingleConnectorSite(preferred) {
  const sites = await api("/sites");
  const candidates = [
    ...preferred.map((name) => sites.find((s) => s.name === name)).filter(Boolean),
    ...sites.filter((s) => s.source === "simulated" && s.charge_point_count === 1),
  ];
  for (const site of candidates) {
    const detail = await api(`/sites/${site.id}`);
    const [charger] = detail.charge_points;
    if (detail.charge_points.length === 1 && charger.connectors.length === 1
        && charger.connectors[0].status === "Available") {
      return { site, identity: charger.identity };
    }
  }
  throw new Error("no free single-connector site");
}

const COLOURS = {
  available: "#22c55e", occupied: "#f59e0b", reserved: "#8b5cf6", faulted: "#ef4444",
  unavailable: "#6b7280",
};

async function simulatedSiteCounts() {
  const counts = { available: 0, occupied: 0, reserved: 0, faulted: 0, unavailable: 0 };
  for (const site of await api("/sites")) {
    if (site.source === "simulated") counts[site.aggregate_status] += 1;
  }
  return counts;
}

// --- the director: everything a video script does -----------------------------------------------

class Director {
  constructor(page) {
    this.page = page;
    this.stepNo = 0;
    this.mouse = { x: VIEWPORT.width * 0.62, y: VIEWPORT.height * 0.45 };
    this.remoteStarts = new Set(); // identities a session was started on, for cleanup
  }

  demo(method, ...args) {
    return this.page.evaluate(([m, a]) => window.__demo[m](...a), [method, args]);
  }

  async card(content, ms = 9000) {
    await this.demo("card", content);
    await hold(ms);
    await this.demo("hideCard");
    await sleep(800);
  }

  header(text) { return this.demo("header", text); }

  async step(text, detail = "", ms = 5000) {
    this.stepNo += 1;
    await this.demo("caption", this.stepNo, text, detail);
    await hold(ms);
  }

  /** Same caption box, no new step number: a follow-up sentence to the current step. */
  async say(text, detail = "", ms = 4500) {
    await this.demo("caption", this.stepNo, text, detail);
    await hold(ms);
  }

  hideCaption() { return this.demo("hideCaption"); }
  legend(on) { return this.demo("legend", on); }
  counter(items) { return this.demo("counter", items); }

  // pointer ---------------------------------------------------------------------------------------

  async glide(x, y, ms = 1300) {
    const from = this.mouse;
    const steps = Math.max(12, Math.round(ms / 20));
    for (let i = 1; i <= steps; i += 1) {
      const t = i / steps;
      const ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      await this.page.mouse.move(from.x + (x - from.x) * ease, from.y + (y - from.y) * ease);
      await sleep(ms / steps);
    }
    this.mouse = { x, y };
  }

  async click(x, y) {
    await this.glide(x, y);
    await hold(500);
    await this.page.mouse.click(x, y);
    await sleep(400);
  }

  /** Click a map pin, then move the pointer off it so the pin's colour stays visible. */
  async clickPin(point) {
    await this.click(point.x, point.y);
    await this.glide(point.x + 150, point.y + 130, 900);
  }

  async clickLocator(locator) {
    const box = await locator.boundingBox();
    await this.click(box.x + box.width / 2, box.y + box.height / 2);
  }

  async typeInto(locator, text) {
    await this.clickLocator(locator);
    await locator.fill("");
    for (const char of text) {
      await this.page.keyboard.type(char);
      await hold(150);
    }
  }

  // highlights ------------------------------------------------------------------------------------

  async highlight(target, { pad = 6, ring = false } = {}) {
    // One box around everything given: rectangles as they are, and every element a locator matches.
    const rects = [];
    for (const item of Array.isArray(target) ? target : [target]) {
      if ("x" in item) {
        rects.push(item);
        continue;
      }
      const count = await item.count();
      for (let i = 0; i < count; i += 1) rects.push(await item.nth(i).boundingBox());
    }
    if (rects.length === 0) throw new Error("nothing to highlight");
    const x = Math.min(...rects.map((r) => r.x)) - pad;
    const y = Math.min(...rects.map((r) => r.y)) - pad;
    const right = Math.max(...rects.map((r) => r.x + r.width)) + pad;
    const bottom = Math.max(...rects.map((r) => r.y + r.height)) + pad;
    await this.demo("highlight", { x, y, width: right - x, height: bottom - y }, ring);
  }

  clearHighlights() { return this.demo("clearHighlights"); }

  async ringPin(siteId) {
    const point = await this.pinPoint(siteId);
    await this.highlight({ x: point.x - 22, y: point.y - 22, width: 44, height: 44 }, { pad: 0, ring: true });
  }

  // map -------------------------------------------------------------------------------------------

  async mapReady() {
    await this.page.waitForFunction(
      () => window.__chargerMap?.getSource("sites") && window.__chargerMap.loaded() && window.__demo?.ready,
      null, { timeout: 45000 },
    );
  }

  async mapIdle(timeoutMs = 10000) {
    await this.page.evaluate((timeout) => new Promise((resolve) => {
      const map = window.__chargerMap;
      const done = () => resolve();
      setTimeout(done, timeout);
      if (!map.isMoving() && map.areTilesLoaded()) return done();
      map.once("idle", done);
    }), timeoutMs);
  }

  /** Fly the camera to a point, leaving it left of centre so the panel does not cover it. */
  async fly(lng, lat, zoom, ms = 4500, offset = FLY_OFFSET) {
    await this.page.evaluate(([center, z, duration, offset]) => new Promise((resolve) => {
      const map = window.__chargerMap;
      map.once("moveend", resolve);
      map.flyTo({ center, zoom: z, duration, offset, essential: true });
    }), [[lng, lat], zoom, ms * PACE, offset]);
    await this.mapIdle();
    await sleep(600);
  }

  async pinPoint(siteId) {
    const handle = await this.page.waitForFunction((id) => {
      const map = window.__chargerMap;
      const feature = map.queryRenderedFeatures({ layers: ["sites-unclustered"] })
        .find((f) => f.properties.id === id);
      if (!feature) return null;
      const point = map.project(feature.geometry.coordinates);
      return { x: point.x, y: point.y };
    }, siteId, { timeout: 15000, polling: 200 });
    return handle.jsonValue();
  }

  async biggestCluster() {
    return this.page.evaluate(() => {
      const map = window.__chargerMap;
      const clusters = map.queryRenderedFeatures({ layers: ["sites-clusters"] })
        .sort((a, b) => b.properties.point_count - a.properties.point_count);
      if (clusters.length === 0) return null;
      const point = map.project(clusters[0].geometry.coordinates);
      return { x: point.x, y: point.y, count: clusters[0].properties.point_count };
    });
  }

  /** The pin's colour as the map currently has it (its own copy of aggregate_status). */
  mapStatus(siteId) {
    return this.page.evaluate((id) => {
      const data = window.__chargerMap.getSource("sites").serialize().data;
      return data.features.find((f) => f.properties.id === id)?.properties.aggregate_status;
    }, siteId);
  }

  waitMapStatus(siteId, status, timeoutMs = 16000) {
    return waitFor(async () => (await this.mapStatus(siteId)) === status, timeoutMs,
      `the pin to show ${status}`, 300);
  }

  // station panel ---------------------------------------------------------------------------------

  get panel() { return this.page.getByRole("button", { name: "Close" }).locator(".."); }
  charger(identity) { return this.panel.locator("section", { hasText: identity }); }
  connectorRow(identity, index = 0) { return this.charger(identity).locator("li").nth(index); }
  statusOf(identity, index = 0) {
    return this.connectorRow(identity, index).locator(":scope > div > span").nth(1);
  }

  async openSite(site, zoom = 15.5) {
    await this.fly(site.longitude, site.latitude, zoom);
    const point = await this.pinPoint(site.id);
    await this.clickPin(point);
    await this.page.getByRole("heading", { name: site.name }).waitFor({ timeout: 10000 });
    await this.page.getByText("Live", { exact: true }).waitFor({ timeout: 15000 });
    await sleep(500);
  }

  async closePanel() {
    await this.clearHighlights();
    await this.clickLocator(this.page.getByRole("button", { name: "Close" }));
    await sleep(600);
  }

  waitPanelStatus(identity, status, timeoutMs = 20000, index = 0) {
    return waitFor(async () => (await this.statusOf(identity, index).textContent()) === status,
      timeoutMs, `${identity} to show ${status}`, 200);
  }

  // terminal --------------------------------------------------------------------------------------

  async terminal(title = "operator terminal") {
    await this.demo("termShow", title);
    await sleep(400);
  }

  async typeLine(cls, text, perChar = 38) {
    await this.demo("termLine", cls, "");
    for (const char of text) {
      await this.demo("termAppend", char);
      await sleep(perChar * PACE);
    }
  }

  async echo(text, cls = "out") { await this.demo("termLine", cls, text); }

  async comment(text) {
    await this.typeLine("dim", `# ${text}`, 26);
    await hold(600);
  }

  /** Type `display` into the on-screen terminal, run the real command, print its real output. */
  async run(display, argv, { expect } = {}) {
    await this.typeLine("cmd", display);
    await hold(700);
    const { stdout, stderr } = await execFileP(argv[0], argv.slice(1), {
      cwd: CHARGERS, env: { ...process.env, ADMIN_TOKEN },
    }).catch((error) => error);
    const output = `${stdout ?? ""}${stderr ?? ""}`.trim();
    for (const line of output.split("\n")) await this.echo(line, "ok");
    if (expect && !expect.test(output)) throw new Error(`${display}: unexpected output ${output}`);
    await hold(1200);
    return output;
  }

  operate(...args) {
    const display = `python operate.py ${args.join(" ")}`;
    return (options) => this.run(display, [PYTHON, "operate.py", ...args], options);
  }

  async remoteStart(identity, expect = /Accepted/) {
    this.remoteStarts.add(identity);
    return this.operate("remote-start", identity, "DEMO-REMOTE")({ expect });
  }

  async currentTransaction(identity) {
    return (await api(`/charge-points/${encodeURIComponent(identity)}`)).current_transaction;
  }
}

/** After a video (or a failed take): stop any session DEMO-REMOTE started and switch anything
 * this script switched off back on, so the next take starts clean. Silent: not on camera. */
async function cleanup(director, extra = []) {
  for (const identity of [...director.remoteStarts, ...extra]) {
    const run = (...args) => execFileP(PYTHON, ["operate.py", ...args], {
      cwd: CHARGERS, env: { ...process.env, ADMIN_TOKEN },
    }).catch(() => {});
    const tx = await director.currentTransaction(identity).catch(() => null);
    if (tx) await run("remote-stop", identity, String(tx.transaction_id));
    await run("change-availability", identity, "1", "Operative");
  }
}

// --- the videos ---------------------------------------------------------------------------------

async function video1(d) {
  const page = d.page;
  await d.card({
    kicker: "Story 1 · Driver",
    title: "Where can I charge right now?",
    story: "As a driver, I want to see every charging site in Montenegro with its live status, "
      + "so that I know where I can plug in before I set off.",
    bullets: [
      "Open the map of the whole country",
      "Read the colours: free, busy, reserved, broken, switched off",
      "Zoom in by clicking a cluster",
      "Search for a town — and for one that doesn't exist",
    ],
    foot: "Demo data: the sites are real places; their statuses are simulated.",
  });
  await d.header("Story 1 · Where can I charge right now?");
  await d.step("This is the public map: every charging site in Montenegro.",
    "136 sites. A circle with a number is a cluster of sites close together.", 6500);
  await d.legend(true);
  await d.step("Each colour is a live status.",
    "Green: a connector is free. Amber: every connector is busy. Violet: reserved. "
      + "Red: broken. Grey: switched off.", 8500);
  await d.say("A cluster shows the most useful colour inside it.",
    "Red if any site in it is broken, otherwise green if any has a free connector.", 6500);

  const first = await d.biggestCluster();
  await d.step("Click a cluster to zoom in on it.", `This one groups ${first.count} sites.`, 3000);
  await d.click(first.x, first.y);
  await d.mapIdle();
  await hold(2500);
  const second = await d.biggestCluster();
  if (second) {
    await d.say("Clusters split up as you zoom. Click one again.", "", 2500);
    await d.click(second.x, second.y);
    await d.mapIdle();
    await hold(2000);
  }
  await d.step("Now most circles are single sites, each coloured by its own live status.", "", 6000);

  const search = page.getByPlaceholder("Search a place in Montenegro…");
  const go = page.getByRole("button", { name: "Go" });
  await d.step("Or search for a place. First, one that doesn't exist…", "", 3000);
  await d.typeInto(search, "Narnia");
  await hold(600);
  await d.clickLocator(go);
  const nothing = page.getByText("Nothing found.");
  await nothing.waitFor({ timeout: 15000 });
  await d.highlight(nothing);
  await d.say("…the map says “Nothing found.” and stays where it is.", "", 4500);
  await d.clearHighlights();

  await d.step("Now a real town: Budva, on the coast.", "", 2500);
  await d.typeInto(search, "Budva");
  await hold(600);
  await d.clickLocator(go);
  await sleep(1500);
  await d.mapIdle();
  await d.step("The map moves to Budva. Every pin here is a charging site.",
    "Hotels, car parks and fuel stations — green ones have a free connector right now.", 7000);
  await d.step("The colours keep themselves up to date.",
    "The map re-reads every site's status every 10 seconds. No reload needed.", 11000);
  await d.hideCaption();
  await d.legend(false);
  await d.header("");
  await d.card({
    kicker: "Story 1 · What we saw",
    title: "The map answers “where can I charge?” at a glance",
    bullets: [
      "Every site in the country, coloured by live status",
      "Clusters zoom in when clicked, and take the most useful colour inside them",
      "Search moves the map to a town, and says so when a place isn't found",
      "Colours refresh every 10 seconds without reloading the page",
    ],
  }, 8000);
}

async function video2(d) {
  await d.card({
    kicker: "Story 2 · Driver",
    title: "Will my car fit, and how fast will it charge?",
    story: "As a driver, I want to see each station's plugs and power before I drive there, "
      + "so that I don't arrive at a plug my car can't use.",
    bullets: [
      "A roadside rest stop with four fast chargers",
      "One charger with three different plugs, each rated",
      "A rating that isn't a round number",
      "A station whose power nobody published",
    ],
  });
  await d.header("Story 2 · Will my car fit, and how fast will it charge?");
  const page = d.page;

  const pelev = await siteNamed("Rest stop Pelev Brijeg");
  await d.step("Fly to Rest stop Pelev Brijeg, a roadside rest stop.", "", 2500);
  await d.fly(pelev.longitude, pelev.latitude, 15.5);
  await d.say("Click its pin to open the station panel.", "", 1500);
  const pin = await d.pinPoint(pelev.id);
  await d.clickPin(pin);
  await page.getByRole("heading", { name: pelev.name }).waitFor();
  await page.getByText("Live", { exact: true }).waitFor({ timeout: 15000 });
  await hold(1500);
  await d.highlight(page.getByText("Up to 200 kW"));
  await d.step("“Up to 200 kW”: the fastest connector at this site.", "", 5000);
  await d.clearHighlights();
  await d.highlight([d.panel.locator("section").first(), d.panel.locator("section").last()]);
  await d.step("Four chargers. Each has one CCS2 plug, rated 200 kW DC.",
    "CCS2 is the fast DC plug most European cars use. DC skips the car's own charger, "
      + "which is why it is so fast.", 8000);
  await d.clearHighlights();
  const firstCharger = d.panel.locator("section").first();
  await d.highlight(firstCharger.locator("li > div > span").nth(1));
  await d.step("Next to each plug: its live status.",
    "Available means nothing is plugged in; Charging means a car is drawing power.", 6000);
  await d.clearHighlights();
  const note = page.getByText("Demo data — simulated chargers.");
  await d.highlight(note);
  await d.step("Every simulated site says so, at the bottom.",
    "These are real places, but in this demo their statuses are simulated.", 5500);
  await d.closePanel();

  const tivat = await siteNamed("EKO Tivat");
  await d.step("EKO Tivat: one charger, three different plugs.", "", 2500);
  await d.openSite(tivat);
  await d.highlight(d.panel.locator("section").first().locator("li"));
  await d.say("Each plug has its own rating and its own status.",
    "CHAdeMO 50 kW DC (older Japanese cars), CCS2 50 kW DC, and Type 2 22 kW AC — "
      + "the everyday AC plug in Europe.", 9000);
  await d.closePanel();

  const merit = await siteNamed("Merit Starlit Hotel & Residence");
  await d.step("Merit Starlit Hotel in Budva: ratings are shown as published.", "", 2500);
  await d.openSite(merit);
  await d.highlight(d.panel.locator("section").first().locator("li").first());
  await d.say("PlugShare lists 15.36 kW; the panel shows 15.4 kW.",
    "Whole numbers without decimals (22 kW), anything else to one decimal.", 7000);
  await d.closePanel();

  const kolasin = await siteNamed("kolasin 1600");
  await d.step("kolasin 1600, in the mountains: nobody published its power.", "", 2500);
  await d.openSite(kolasin);
  await d.highlight(d.panel.locator("section").first().locator("li").first());
  await d.say("So the panel says only “Type 2 · AC”, with no “Up to” line.",
    "The power is never guessed from the plug type.", 7000);
  await d.clearHighlights();
  await d.hideCaption();
  await d.header("");
  await d.card({
    kicker: "Story 2 · What we saw",
    title: "Plugs and power, before you drive there",
    bullets: [
      "Every charger at a site is listed, with each of its plugs",
      "Plug type, AC or DC, and the rated power when it is known",
      "“Up to N kW” tells you the fastest connector at the site",
      "Unknown power is left out, never guessed",
    ],
  }, 8000);
}

async function video3(d) {
  const target = await freeSingleConnectorSite(["kolasin 1600", "Hotel Eleven", "Cetinje Municipality"]);
  const { site, identity } = target;
  await d.card({
    kicker: "Story 3 · Operator and driver",
    title: "Start a charge remotely — and watch it happen live",
    story: "As an operator, I want to start and stop a session for a driver whose card or app "
      + "doesn't work. As a driver, I want to watch the station change without reloading.",
    bullets: [
      "Open a station with a single connector",
      "The operator starts a session from the terminal",
      "Preparing → Charging, with the energy counting up",
      "The pin turns amber; a second start is refused",
      "The operator stops it: Finishing → Available",
    ],
  });
  await d.header("Story 3 · Start a charge remotely, and watch it live");
  const page = d.page;

  await d.step(`Open ${site.name}: one charger with a single connector.`, "", 2500);
  await d.openSite(site);
  await d.highlight(d.statusOf(identity));
  await d.say("It is Available: nothing is plugged in.", "", 4500);
  await d.clearHighlights();
  await d.highlight(page.getByText("Live", { exact: true }));
  await d.say("“Live” means the panel is connected to the station and updates itself.", "", 5000);
  await d.clearHighlights();

  await d.terminal();
  await d.step("An operator starts a session for the demo driver, tag DEMO-REMOTE.",
    "operate.py asks the Central System, which sends the charger a RemoteStartTransaction.", 3000);
  await d.remoteStart(identity);
  await d.say("The charger checks the driver's tag, reports Preparing, then Charging.", "", 1500);
  await d.waitPanelStatus(identity, "Charging", 15000);
  await d.highlight(d.connectorRow(identity));
  await hold(4000);
  const session = d.connectorRow(identity).locator("p", { hasText: "Session #" });
  await session.waitFor({ timeout: 10000 });
  await d.step("A session number appears.",
    "The charger sends a meter reading every 15 seconds: the energy delivered so far.", 3000);
  const reading = async () => {
    const match = (await session.textContent()).match(/([\d.]+) Wh/);
    return match ? Number(match[1]) : null;
  };
  const first = await waitFor(reading, 25000, "a meter reading");
  await d.say(`First reading: ${first} Wh.`, "Watch it count up…", 1500);
  const second = await waitFor(async () => {
    const value = await reading();
    return value !== null && value > first ? value : null;
  }, 25000, "a higher meter reading");
  await d.say(`Energy keeps counting: ${first} Wh → ${second} Wh.`, "", 5000);
  await d.clearHighlights();

  await d.step("On the map, the pin turns amber: every connector here is busy.",
    "The map re-reads statuses every 10 seconds.", 1000);
  await d.waitMapStatus(site.id, "occupied");
  await d.ringPin(site.id);
  await hold(5000);
  await d.clearHighlights();

  await d.step("What if someone tries to start a second session here?", "", 2500);
  await d.remoteStart(identity, /Rejected/);
  await d.say("Refused: this connector already has a session.", "", 5000);

  const tx = await d.currentTransaction(identity);
  await d.step("Now the operator stops the session.", "", 2500);
  await d.operate("remote-stop", identity, String(tx.transaction_id))({ expect: /Accepted/ });
  await d.waitPanelStatus(identity, "Finishing", 15000);
  await d.highlight(d.connectorRow(identity));
  await d.say("Finishing: charging is over, but the cable is still plugged in.", "", 3000);
  await d.waitPanelStatus(identity, "Available", 20000);
  await d.say("The driver unplugs: Available again.", "", 4000);
  await d.clearHighlights();
  await d.step("And within 10 seconds the pin is green again.", "", 1000);
  await d.waitMapStatus(site.id, "available");
  await d.ringPin(site.id);
  await hold(5000);
  await d.clearHighlights();
  await d.hideCaption();
  await d.demo("termHide");
  await d.header("");
  await d.card({
    kicker: "Story 3 · What we saw",
    title: "One command starts a charge; everyone sees it live",
    bullets: [
      "remote-start: the charger checks the driver, then Preparing → Charging",
      "The panel showed the session number and the energy counting up",
      "The pin turned amber without a reload, and back to green after",
      "A second start on a busy connector was refused",
      "remote-stop: Finishing, then Available once the cable is out",
    ],
  }, 8000);
}

async function video4(d) {
  await d.card({
    kicker: "Story 4 · Driver",
    title: "Which charger is free — and which is broken?",
    story: "As a driver arriving at a big site, I want to see every charger's own status. "
      + "Before I drive anywhere, I want to know if a station is broken.",
    bullets: [
      "A site with five chargers, each with its own status",
      "One of them changes; the others don't follow",
      "A red pin: a station reported out of order",
    ],
  });
  await d.header("Story 4 · Which charger is free, and which is broken?");
  const page = d.page;

  const greencar = await siteNamed("GreenCar.me");
  await d.step("GreenCar.me, on the coast road: five chargers at one site.", "", 2500);
  await d.openSite(greencar);
  await d.highlight([d.panel.locator("section").first(), d.panel.locator("section").last()]);
  await d.say("Charger 1 to Charger 5, each with its own live status.",
    "The site stays green while any connector is free; it turns amber only when all are busy.", 8000);
  await d.clearHighlights();

  const detail = await api(`/sites/${greencar.id}`);
  const index = detail.charge_points.findIndex((cp) => cp.connectors[0].status === "Available");
  if (index < 0) throw new Error("no Available charger at GreenCar.me right now");
  const identity = detail.charge_points[index].identity;
  await d.terminal();
  await d.step(`Change just one of them: Charger ${index + 1}.`, "", 2500);
  await d.remoteStart(identity);
  await d.waitPanelStatus(identity, "Charging", 15000);
  await d.highlight(d.charger(identity));
  await d.say(`Only Charger ${index + 1} changed to Charging.`,
    "Each row follows its own charger — the others don't copy it.", 7000);
  await d.clearHighlights();
  const tx = await d.currentTransaction(identity);
  await d.say("Stop it again.", "", 1500);
  await d.operate("remote-stop", identity, String(tx.transaction_id))({ expect: /Accepted/ });
  await d.waitPanelStatus(identity, "Finishing", 15000);
  await hold(2500);
  await d.demo("termHide");
  await d.closePanel();

  const vranjina = await siteNamed("Vranjina");
  await d.step("Now a red pin: Vranjina, by Lake Skadar.", "", 2500);
  await d.fly(vranjina.longitude, vranjina.latitude, 15.5);
  await d.ringPin(vranjina.id);
  await hold(3500);
  await d.clearHighlights();
  const pin = await d.pinPoint(vranjina.id);
  await d.clickPin(pin);
  await page.getByRole("heading", { name: vranjina.name }).waitFor();
  await hold(1500);
  await d.highlight(d.panel.locator("section").first().locator("li"));
  await d.say("Both connectors are Faulted, with error code OtherError.",
    "PlugShare users reported this station out of order, so its demo charger reports a fault "
      + "for the whole demo.", 9000);
  await d.clearHighlights();
  await d.step("A driver sees this before driving there, not after.", "", 5000);
  await d.hideCaption();
  await d.header("");
  await d.card({
    kicker: "Story 4 · What we saw",
    title: "Every charger speaks for itself",
    bullets: [
      "A five-charger site lists each charger and its own live status",
      "Starting one changed only that one's row",
      "A broken station is red on the map and Faulted in the panel",
    ],
  }, 8000);
}

async function video5(d) {
  const { site, identity } = await freeSingleConnectorSite(
    ["Hotel Eleven", "Cetinje Municipality", "17 Vuka Karadžića", "kolasin 1600"],
  );
  d.cleanupExtra = [identity];
  await d.card({
    kicker: "Story 5 · Operator",
    title: "Maintenance, reservations and reboots",
    story: "As an operator, I want to switch a connector off for maintenance, hold one for a "
      + "driver, and reboot a misbehaving charger — and have the public map show each of them.",
    bullets: [
      "Switch the connector off, then on again",
      "Reserve it for a driver, then cancel",
      "Reboot the charger in the middle of a session",
    ],
  });
  await d.header("Story 5 · Maintenance, reservations and reboots");

  await d.step(`Open ${site.name}: one connector, Available.`, "", 2500);
  await d.openSite(site);
  await d.terminal();

  await d.step("Maintenance: switch the connector off.",
    "ChangeAvailability tells the charger to become Inoperative.", 2500);
  await d.operate("change-availability", identity, "1", "Inoperative")({ expect: /Accepted/ });
  await d.waitPanelStatus(identity, "Unavailable", 15000);
  await d.highlight(d.connectorRow(identity));
  await d.say("Unavailable: nobody can start a session here now.", "", 4500);
  await d.clearHighlights();
  await d.say("The pin turns grey within 10 seconds: switched off.", "", 500);
  await d.waitMapStatus(site.id, "unavailable");
  await d.ringPin(site.id);
  await hold(4500);
  await d.clearHighlights();
  await d.say("Maintenance done: switch it back on.", "", 2000);
  await d.operate("change-availability", identity, "1", "Operative")({ expect: /Accepted/ });
  await d.waitPanelStatus(identity, "Available", 15000);
  await d.highlight(d.connectorRow(identity));
  await d.say("Available again.", "", 3500);
  await d.clearHighlights();

  const expiry = new Date(Date.now() + 15 * 60 * 1000).toISOString().replace(/\.\d+Z$/, "+00:00");
  await d.demo("termClear");
  await d.step("Reserve the connector for one driver, for 15 minutes.", "", 2500);
  const reserved = await d.operate("reserve-now", identity, "1", "DEMO-REMOTE", expiry)({ expect: /Accepted/ });
  const reservationId = reserved.match(/'reservation_id': (\d+)/)?.[1];
  d.cleanupReservation = { identity, reservationId };
  await d.waitPanelStatus(identity, "Reserved", 15000);
  await d.highlight(d.connectorRow(identity));
  await d.say("Reserved: held for that driver.", "", 4500);
  await d.clearHighlights();
  await d.say("The pin turns violet.", "", 500);
  await d.waitMapStatus(site.id, "reserved");
  await d.ringPin(site.id);
  await hold(4500);
  await d.clearHighlights();
  await d.say("The driver changed plans: cancel the reservation.", "", 2000);
  await d.operate("cancel-reservation", identity, reservationId)({ expect: /Accepted/ });
  d.cleanupReservation = null;
  await d.waitPanelStatus(identity, "Available", 15000);
  await d.say("Available again.", "", 3500);

  await d.demo("termClear");
  await d.step("Reboot a charger in the middle of a session.", "First, a session:", 2000);
  await d.remoteStart(identity);
  await d.waitPanelStatus(identity, "Charging", 15000);
  await d.highlight(d.connectorRow(identity));
  await d.say("It is charging.", "", 3000);
  await d.clearHighlights();
  const mark = d.fleet.mark();
  await d.say("Now a soft reset: restart the charger's software.", "", 2000);
  await d.operate("reset", identity, "Soft")({ expect: /Accepted/ });
  // Finishing can be brief: the charger reboots straight after it.
  await waitFor(async () => (await d.statusOf(identity).textContent()) !== "Charging", 15000,
    "the session to end", 100);
  await d.say("The charger ends the session first…", "", 1000);
  await d.waitPanelStatus(identity, "Available", 30000);
  await d.highlight(d.connectorRow(identity));
  await d.say("…restarts, reconnects on its own, and reports Available.", "", 3000);
  const lines = await waitFor(() => {
    const own = d.fleet.lines(mark).filter((line) => line.startsWith(`${identity}:`));
    return own.some((line) => line.includes("reconnected")) ? own : null;
  }, 15000, "the fleet to log the reconnect");
  await d.comment("the charger's own console:");
  for (const line of lines) await d.echo(line, "out");
  await hold(6000);
  await d.clearHighlights();
  await d.hideCaption();
  await d.demo("termHide");
  await d.header("");
  await d.card({
    kicker: "Story 5 · What we saw",
    title: "Operator actions show up on the public map",
    bullets: [
      "Switched off: Unavailable in the panel, a grey pin on the map",
      "Reserved: violet, until the reservation was cancelled",
      "A soft reset ended the session, rebooted the charger, and it came back by itself",
    ],
  }, 8000);
}

async function video6(d) {
  await d.card({
    kicker: "Story 6 · Operator and presenter",
    title: "Restarts, and ending the demo honestly",
    story: "As an operator, I want chargers to reconnect by themselves when the Central System "
      + "restarts. When the demo ends, the map must stop claiming live knowledge it no longer has.",
    bullets: [
      "Restart the Central System while 195 chargers are connected",
      "Stop the demo: watch the map turn grey (switched off) and red (broken)",
      "Start it again: the colours come back",
    ],
  });
  await d.header("Story 6 · Restarts, and ending the demo honestly");
  // No station panel in this video: keep the country in view and put the captions and the
  // terminal on the right, over Albania, where there are no sites to hide.
  await d.demo("layout", "right");
  await d.fly(19.1, 42.55, 9.2, 3500, [0, 0]);

  let counting = true;
  const updateCounter = async () => {
    while (counting) {
      const c = await simulatedSiteCounts().catch(() => null);
      if (c) {
        await d.counter([
          { label: "Demo sites now:" },
          { color: COLOURS.available, label: `${c.available} free` },
          { color: COLOURS.occupied, label: `${c.occupied} busy` },
          { color: COLOURS.reserved, label: `${c.reserved} reserved` },
          { color: COLOURS.faulted, label: `${c.faulted} broken` },
          { color: COLOURS.unavailable, label: `${c.unavailable} switched off` },
        ]).catch(() => {});
      }
      await sleep(2000);
    }
  };
  const counterLoop = updateCounter();
  try {
    await d.legend(true);
    await d.step("The demo is running. The box at the top counts the 134 demo sites by colour.",
      "It reads the same API the map uses.", 6000);
    await d.terminal("terminals: Central System and fleet");
    await d.comment("the fleet's console, every 30 seconds:");
    const summary = d.fleet.lines(d.fleet.startMark).filter((l) => l.startsWith("connected ")).at(-1);
    await d.echo(summary ?? "connected 195/195", "out");
    await d.say("195 simulated chargers are connected to the Central System.", "", 5000);

    await d.step("Restart the Central System while everything runs.",
      "main.py is the server every charger is connected to.", 3000);
    await d.comment("Ctrl+C in the Central System's terminal, then:");
    const mark = d.fleet.mark();
    const mainLog = path.join(LOG_DIR, "main.log");
    const mainMark = fs.existsSync(mainLog) ? fs.statSync(mainLog).size : 0;
    await d.typeLine("cmd", "python main.py");
    const oldPid = centralSystemPid();
    if (oldPid) await interrupt(oldPid, "main.py");
    const restartedAt = Date.now();
    await sleep(1500);
    spawnPython("main.py", mainLog);
    const listening = await waitFor(
      () => readLines(mainLog, mainMark).find((line) => line.startsWith("Listening on")), 20000,
      "main.py to listen",
    );
    await d.echo(listening, "ok");
    await d.say("Every charger notices the drop, waits a moment, and reconnects on its own.",
      "Nobody touches them.", 1000);
    await d.echo("", "out");
    let reconnected = 0;
    while (reconnected < 195) {
      reconnected = d.fleet.lines(mark).filter((line) => line.endsWith(": reconnected")).length;
      const lost = d.fleet.lines(mark).filter((line) => line.includes(": connection lost")).length;
      await d.demo("termSetLast", `fleet: ${lost} lost the connection · ${reconnected}/195 reconnected`);
      if (Date.now() - restartedAt > 90000) throw new Error("the fleet did not reconnect in 90 s");
      await sleep(500);
    }
    const seconds = Math.round((Date.now() - restartedAt) / 1000);
    await d.say(`All 195 are back, ${seconds} seconds after the restart.`,
      "Sessions the restart cut off are closed as PowerLoss, so none is left hanging open.", 7000);

    await d.step("Now end the demo: Ctrl+C in the fleet's terminal.", "", 2500);
    const stopMark = d.fleet.mark();
    await d.comment("Ctrl+C in the fleet's terminal");
    await d.fleet.stop();
    for (const line of d.fleet.lines(stopMark).slice(-2)) await d.echo(line, "out");
    await d.say("Every session ends, and every working charger reports Unavailable.", "", 5000);
    await d.say("Within 10 seconds the map tells the truth.",
      "Grey: switched off. The 10 broken sites stay red — they are still broken.", 1000);
    await waitFor(async () => (await simulatedSiteCounts()).unavailable === 124, 25000, "124 grey sites");
    await hold(9000);

    await d.step("Start the demo again.", "", 2000);
    await d.typeLine("cmd", "python run_demo_fleet.py");
    const startMark = d.fleet.start();
    const started = await d.fleet.waitForLine(/^Starting /, startMark, 15000);
    await d.echo(started, "out");
    await d.say("The chargers connect ten per second, then report their connectors.", "", 1000);
    await waitFor(async () => (await simulatedSiteCounts()).unavailable === 0, 60000, "all sites live");
    const back = await d.fleet.waitForLine(/^connected /, startMark, 40000);
    await d.echo(back, "out");
    await d.say("The colours are back: the demo is live again.", "", 8000);
  } finally {
    counting = false;
    await counterLoop;
  }
  await d.counter(null);
  await d.legend(false);
  await d.hideCaption();
  await d.demo("termHide");
  await d.header("");
  await d.card({
    kicker: "Story 6 · What we saw",
    title: "It recovers by itself, and it never pretends",
    bullets: [
      "After a Central System restart, all 195 chargers reconnected with no one intervening",
      "Stopping the demo switched every working charger off: grey, with the broken ones still red",
      "Starting it again brought the live colours back",
    ],
  }, 8000);
}

const VIDEOS = [
  { n: 1, file: "01-map-at-a-glance.mp4", run: video1 },
  { n: 2, file: "02-plugs-and-power.mp4", run: video2 },
  { n: 3, file: "03-remote-session-live.mp4", run: video3 },
  { n: 4, file: "04-busy-and-broken-sites.mp4", run: video4 },
  { n: 5, file: "05-operator-controls.mp4", run: video5 },
  { n: 6, file: "06-restarts-and-honest-stop.mp4", run: video6 },
];

// --- main ---------------------------------------------------------------------------------------

async function preflight() {
  const problems = [];
  await fetch(APP_URL).catch(() => problems.push(`the frontend is not up at ${APP_URL}`));
  await api("/sites").catch(() => problems.push(`the API is not up at ${API}`));
  if (!(await centralSystemUp())) problems.push("main.py is not listening on :9000");
  if (Fleet.runningElsewhere()) problems.push("run_demo_fleet.py is running: stop it (Ctrl+C) first");
  if (!ADMIN_TOKEN) problems.push("ADMIN_TOKEN is not set (and not in .env)");
  try { execFileSync(FFMPEG, ["-version"], { stdio: "ignore" }); } catch { problems.push(`no ffmpeg at ${FFMPEG}`); }
  if (problems.length) {
    console.error(`Cannot record:\n  - ${problems.join("\n  - ")}`);
    process.exit(1);
  }
}

async function recordOne(browser, fleet, video) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript(installOverlay);
  const page = await context.newPage();
  const director = new Director(page);
  director.fleet = fleet;
  try {
    await page.goto(APP_URL);
    await director.mapReady();
    await page.mouse.move(director.mouse.x, director.mouse.y);
    await sleep(1500);
    const recording = await Recording.start(page);
    await sleep(300);
    await video.run(director);
    const out = path.join(OUT_DIR, video.file);
    const { frames, seconds } = await recording.stop(out);
    console.log(`  saved ${out} (${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}, ${frames} frames)`);
  } finally {
    if (director.cleanupReservation?.reservationId) {
      const { identity, reservationId } = director.cleanupReservation;
      await execFileP(PYTHON, ["operate.py", "cancel-reservation", identity, reservationId], {
        cwd: CHARGERS, env: { ...process.env, ADMIN_TOKEN },
      }).catch(() => {});
    }
    await cleanup(director, director.cleanupExtra ?? []);
    await context.close();
  }
}

async function main() {
  const wanted = process.argv.slice(2).map(Number).filter(Boolean);
  const videos = wanted.length ? VIDEOS.filter((v) => wanted.includes(v.n)) : VIDEOS;
  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.mkdirSync(LOG_DIR, { recursive: true });
  await preflight();

  const fleet = new Fleet();
  console.log("Starting the demo fleet and waiting for all chargers to connect…");
  const mark = fleet.start();
  await fleet.waitForLine(/^connected \d+\/\d+/, mark, 90000);
  await sleep(20000); // let the first sessions start, so the map is not all green
  const browser = await chromium.launch({ args: BROWSER_ARGS });
  try {
    for (const video of videos) {
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        console.log(`Recording video ${video.n} (${video.file}), take ${attempt}…`);
        try {
          await recordOne(browser, fleet, video);
          break;
        } catch (error) {
          console.error(`  take ${attempt} failed: ${error.message}`);
          if (!fleet.pid || !isAlive(fleet.pid)) {
            const again = fleet.start();
            await fleet.waitForLine(/^connected \d+\/\d+/, again, 90000);
          }
          if (attempt === 3) throw error;
          await sleep(5000);
        }
      }
    }
  } finally {
    await browser.close();
    if (process.env.DEMO_KEEP_FLEET !== "1") {
      console.log("Stopping the demo fleet…");
      await fleet.stop();
    }
  }
  console.log(`Done. Videos in ${OUT_DIR}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
