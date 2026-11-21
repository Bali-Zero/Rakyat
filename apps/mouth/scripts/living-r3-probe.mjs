#!/usr/bin/env node
/**
 * Living Web (Linea 7) — R3 probe: "one peak per viewport".
 *
 * Scrolls each page in half-viewport steps and, at every step, counts the
 * <video> elements that are actually PLAYING inside the viewport
 * (!paused, !ended, readyState >= 2, visible box intersecting the viewport).
 * A page passes when that count never exceeds --max (default 1).
 *
 * The probe proves nothing unless its own controls behave, so it runs them
 * FIRST on every invocation and prints their verdict before any page:
 *   - innocence: two autoplaying loops a full screen apart -> must PASS
 *   - guilt:     two autoplaying loops side by side         -> must FAIL
 * Innocence noisy or guilt silent => "probe NOT trusted", exit 2.
 *
 * Usage:
 *   node scripts/living-r3-probe.mjs <url> [<url> ...] [--max 1] [--reduced-motion] [--mobile]
 *   node scripts/living-r3-probe.mjs --controls-only
 * With --reduced-motion the expected maximum becomes 0 (no video may play).
 *
 * Needs ffmpeg (to build the control clip) and Playwright Chromium.
 * Exit: 0 all pages pass · 1 a page violates R3 · 2 probe not trusted.
 */
import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const option = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const urls = args.filter((a, i) => !a.startsWith("--") && args[i - 1] !== "--max");
const reducedMotion = flag("--reduced-motion");
const maxAllowed = reducedMotion ? 0 : Number(option("--max", "1"));
const mobile = flag("--mobile");

// ---------------------------------------------------------------- controls
function buildControls() {
  const dir = mkdtempSync(path.join(tmpdir(), "living-r3-"));
  execFileSync(
    "ffmpeg",
    ["-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=2",
     "-c:v", "libvpx-vp9", "-b:v", "200k", "-an", path.join(dir, "clip.webm")],
  );
  const loop = (style) =>
    `<video src="/clip.webm" autoplay muted loop playsinline style="width:320px;height:180px;${style}"></video>`;
  const page = (body) =>
    `<!doctype html><meta name="viewport" content="width=device-width"><body style="margin:0">${body}</body>`;
  writeFileSync(path.join(dir, "innocence.html"),
    page(`${loop("display:block")}<div style="height:250vh"></div>${loop("display:block")}<div style="height:100vh"></div>`));
  writeFileSync(path.join(dir, "guilt.html"),
    page(`${loop("display:inline-block")}${loop("display:inline-block")}<div style="height:150vh"></div>`));
  const types = { ".html": "text/html", ".webm": "video/webm" };
  const server = createServer((req, res) => {
    try {
      const file = path.join(dir, path.basename(req.url.split("?")[0]));
      res.writeHead(200, { "content-type": types[path.extname(file)] ?? "application/octet-stream" });
      res.end(readFileSync(file));
    } catch {
      res.writeHead(404).end();
    }
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, base: `http://127.0.0.1:${server.address().port}` })),
  );
}

// ---------------------------------------------------------------- measure
async function measure(context, url) {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "load", timeout: 60_000 });
  await page.waitForTimeout(1200);
  const steps = [];
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  const viewport = page.viewportSize().height;
  for (let y = 0; y <= Math.max(0, height - viewport) + 1; y += Math.max(1, Math.floor(viewport / 2))) {
    await page.evaluate((top) => window.scrollTo(0, top), y);
    await page.waitForTimeout(900); // IntersectionObserver + play() settle
    const playing = await page.evaluate(() =>
      [...document.querySelectorAll("video")].filter((v) => {
        const r = v.getBoundingClientRect();
        const visible = r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
          r.top < window.innerHeight && r.left < window.innerWidth;
        return visible && !v.paused && !v.ended && v.readyState >= 2;
      }).map((v) => v.currentSrc || v.getAttribute("src") || "(no src)"),
    );
    steps.push({ scrollY: y, playing: playing.length, sources: playing });
  }
  await page.close();
  const worst = Math.max(0, ...steps.map((s) => s.playing));
  return { url, max_playing_in_viewport: worst, pass: worst <= maxAllowed, steps };
}

// ---------------------------------------------------------------- main
// CHROME_HEADLESS pins the binary per machine (same convention as the /design probes):
// Playwright's expected build and the one installed on a node can differ.
const browser = await chromium.launch({
  executablePath: process.env.CHROME_HEADLESS || undefined,
  args: ["--autoplay-policy=no-user-gesture-required"],
});
const context = await browser.newContext({
  viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 800 },
  isMobile: mobile,
  reducedMotion: reducedMotion ? "reduce" : "no-preference",
});

const { server, base } = await buildControls();
const savedMax = maxAllowed;
// Controls always run at max 1 with motion allowed, whatever the page options are.
const controlContext = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: "no-preference" });
const cmax = 1;
const judge = (r) => ({ ...r, pass: r.max_playing_in_viewport <= cmax });
const innocence = judge(await measure(controlContext, `${base}/innocence.html`));
const guilt = judge(await measure(controlContext, `${base}/guilt.html`));
await controlContext.close();
server.close();

const trusted = innocence.pass === true && guilt.pass === false;
console.log(JSON.stringify({
  controls: {
    innocence: { expected: "PASS", got: innocence.pass ? "PASS" : "FAIL", max: innocence.max_playing_in_viewport },
    guilt: { expected: "FAIL", got: guilt.pass ? "PASS" : "FAIL", max: guilt.max_playing_in_viewport },
    verdict: trusted ? "probe trusted" : "probe NOT trusted",
  },
}, null, 2));
if (!trusted) {
  await browser.close();
  process.exit(2);
}
if (flag("--controls-only") || urls.length === 0) {
  await browser.close();
  process.exit(0);
}

const results = [];
for (const url of urls) results.push(await measure(context, url));
await browser.close();
console.log(JSON.stringify({ max_allowed: savedMax, reduced_motion: reducedMotion, mobile, results }, null, 2));
process.exit(results.every((r) => r.pass) ? 0 : 1);
