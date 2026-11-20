// Fixture tests for the Verifikator page checker: real Chromium against a localhost server, no network.
//
// Run:  PLAYWRIGHT_MODULE_DIR=<dir with node_modules/playwright> [PLAYWRIGHT_CHANNEL=chrome] \
//         node --test scripts/verifikator/run.test.mjs
// The browser suite is skipped (not failed) when Playwright cannot launch; the baseline unit tests always run.

import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const OUT = fs.mkdtempSync(path.join(os.tmpdir(), "verifikator-test-"));
process.env.VERIFIKATOR_OUT = OUT;
const { checkPage, judgeViews, loadPlaywright, nextBaseline, readBaseline } = await import("./run.mjs");

const CTA = `<a href="https://wa.me/620000000">Chat on WhatsApp</a>`;
const page = ({ header = "<header>Bali Zero</header>", main = `<main><h1>T</h1>${CTA}</main>`, footer = "<footer>©</footer>", script = "" } = {}) =>
  `<!doctype html><html><head><title>t</title><link rel="icon" href="data:,"></head><body>${header}${main}${footer}${script}</body></html>`;

const state = { toggleFooter: true };
const ROUTES = {
  "/ok": () => page(),
  "/nav-only": () => page({ header: `<nav><a href="/">Home</a></nav>` }),
  "/nav-inside-main": () => page({ header: "", main: `<main><nav><a href="/">Home</a></nav>${CTA}</main>` }),
  "/no-footer": () => page({ footer: "" }),
  "/no-cta": () => page({ main: "<main><h1>T</h1><p>no call to action</p></main>" }),
  "/console-error": () => page({ script: `<script>console.error("boom")</script>` }),
  "/toggle": () => page({ footer: state.toggleFooter ? "<footer>©</footer>" : "" }),
};

let server;
let origin;
let browser = null;
let browserError = null;

before(async () => {
  server = http.createServer((req, res) => {
    const make = ROUTES[new URL(req.url, "http://x").pathname];
    res.writeHead(make ? 200 : 404, { "content-type": "text/html" });
    res.end(make ? make() : "<!doctype html><title>404</title><h1>Not found</h1>");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const opts = process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {};
    browser = await loadPlaywright().chromium.launch(opts);
  } catch (e) {
    browserError = String(e.message).split("\n")[0];
  }
});

after(async () => {
  if (browser) await browser.close();
  server.close();
  fs.rmSync(OUT, { recursive: true, force: true });
});

async function verdictFor(route, baseline = null) {
  const views = await checkPage(browser, route, 1, 0, origin);
  return { views, ...judgeViews(views, baseline) };
}

describe("page checker against fixtures", () => {
  const it = (name, fn) => test(name, async (t) => {
    if (!browser) return t.skip(`Playwright tidak jalan: ${browserError}`);
    await fn();
  });

  it("header + footer + CTA, no errors → HALAMAN OK", async () => {
    const r = await verdictFor("/ok");
    assert.equal(r.verdict, "HALAMAN OK", r.reason);
    assert.ok(r.views.every((v) => v.headerVia === "header"));
  });

  it("only a <nav> before <main> counts as the header → HALAMAN OK", async () => {
    const r = await verdictFor("/nav-only");
    assert.equal(r.verdict, "HALAMAN OK", `${r.reason} ${r.notes.join("; ")}`);
    assert.ok(r.views.every((v) => v.header && v.headerVia === "nav"));
  });

  it("a <nav> inside <main> is not a header (no baseline) → CATATAN", async () => {
    const r = await verdictFor("/nav-inside-main");
    assert.equal(r.verdict, "CATATAN");
    assert.equal(r.notes.length, 2); // desktop + mobile
    assert.match(r.notes[0], /header .* hilang — belum ada baseline/);
  });

  it("no footer and no baseline → CATATAN, not REGRESI", async () => {
    const r = await verdictFor("/no-footer");
    assert.equal(r.verdict, "CATATAN", r.reason);
    assert.deepEqual(r.notes.map((n) => n.split(":")[0]), ["desktop", "mobile"]);
    assert.ok(r.notes.every((n) => /footer .* hilang — belum ada baseline/.test(n)));
  });

  it("no footer, and already missing in the baseline → still CATATAN", async () => {
    const first = await verdictFor("/no-footer");
    const baseline = nextBaseline(null, first.views, "2026-11-13");
    const r = await verdictFor("/no-footer", baseline);
    assert.equal(r.verdict, "CATATAN", r.reason);
    assert.ok(r.notes.every((n) => /sudah hilang di baseline 2026-11-13/.test(n)));
  });

  it("footer present in the baseline and now gone → REGRESI (baseline round-trips through the file)", async () => {
    state.toggleFooter = true;
    const week1 = await verdictFor("/toggle");
    assert.equal(week1.verdict, "HALAMAN OK", week1.reason);
    const file = path.join(OUT, "baseline.json");
    fs.writeFileSync(file, JSON.stringify(nextBaseline(null, week1.views, "2026-11-13")));
    const { baseline } = readBaseline(file);

    state.toggleFooter = false;
    const week2 = await verdictFor("/toggle", baseline);
    assert.equal(week2.verdict, "REGRESI");
    assert.match(week2.reason, /desktop: footer .* hilang — ada di baseline 2026-11-13/);
    assert.match(week2.reason, /mobile: footer .* hilang — ada di baseline 2026-11-13/);
  });

  it("main CTA missing → REGRESI", async () => {
    const r = await verdictFor("/no-cta");
    assert.equal(r.verdict, "REGRESI");
    assert.match(r.reason, /CTA utama hilang/);
  });

  it("console error → REGRESI", async () => {
    const r = await verdictFor("/console-error");
    assert.equal(r.verdict, "REGRESI");
    assert.match(r.reason, /1 console error \("boom"\)/);
  });

  it("HTTP 404 → REGRESI, and the error page is not written into the baseline", async () => {
    const r = await verdictFor("/missing");
    assert.equal(r.verdict, "REGRESI");
    assert.match(r.reason, /desktop: HTTP 404; mobile: HTTP 404/);
    assert.deepEqual(nextBaseline(null, r.views, "2026-11-20").routes, {});
  });
});

describe("baseline (no browser)", () => {
  const view = (o) => ({ viewport: "desktop", route: "/a", status: 200, header: true, footer: true, cta: "Chat", consoleErrors: [], navError: null, network: false, ...o });

  test("a missing or unreadable baseline file is treated as a first run", () => {
    assert.equal(readBaseline(path.join(OUT, "nope.json")).baseline, null);
    const bad = path.join(OUT, "bad.json");
    fs.writeFileSync(bad, "{not json");
    const r = readBaseline(bad);
    assert.equal(r.baseline, null);
    assert.match(r.note, /tidak terbaca/);
  });

  test("nextBaseline keeps unmeasured routes and viewports, overwrites measured ones", () => {
    const prev = { routes: { "/a": { desktop: { header: true, footer: true, checkedAt: "old" }, mobile: { header: true, footer: false, checkedAt: "old" } }, "/b": { desktop: { header: false, footer: true, checkedAt: "old" } } } };
    const next = nextBaseline(prev, [view({ footer: false }), view({ route: "/c", navError: "timeout" })], "new");
    assert.deepEqual(next.routes["/a"].desktop, { header: true, footer: false, checkedAt: "new" });
    assert.equal(next.routes["/a"].mobile.checkedAt, "old");
    assert.ok(next.routes["/b"]);
    assert.equal(next.routes["/c"], undefined);
    assert.equal(prev.routes["/a"].desktop.footer, true, "previous baseline is not mutated");
  });

  test("REGRESI outranks CATATAN; the notes are still reported", () => {
    const baseline = { routes: { "/a": { desktop: { header: false, footer: true, checkedAt: "2026-11-13" } } } };
    const r = judgeViews([view({ header: false, footer: false })], baseline);
    assert.equal(r.verdict, "REGRESI");
    assert.match(r.reason, /footer .* ada di baseline/);
    assert.equal(r.notes.length, 1);
    assert.match(r.notes[0], /header .* sudah hilang di baseline/);
  });

  test("a network failure stays BELUM DIUKUR whatever the baseline says", () => {
    assert.equal(judgeViews([view({ network: true, navError: "net::ERR_PROXY" })], null).verdict, "BELUM DIUKUR");
  });
});
