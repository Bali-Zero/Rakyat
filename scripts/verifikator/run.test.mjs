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
const { checkPage, forgetBaseline, judgeViews, loadConfig, loadPlaywright, nextBaseline, readBaseline } = await import("./run.mjs");

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
  "/exempt-no-cta": () => page({ main: "<main><h1>Hasil</h1></main>", footer: "" }),
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

async function verdictFor(route, baseline = null, config = null) {
  const views = await checkPage(browser, route, 1, 0, origin);
  return { views, ...judgeViews(views, baseline, config) };
}

// Each run's baseline goes through the file, as it does between CI runs via the cache.
function roundTrip(b) {
  const file = path.join(OUT, "baseline.json");
  fs.writeFileSync(file, JSON.stringify(b));
  return readBaseline(file).baseline;
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

  it("no footer, and never seen in the baseline → still CATATAN", async () => {
    const first = await verdictFor("/no-footer");
    const baseline = roundTrip(nextBaseline(null, first.views, "2026-11-13"));
    const r = await verdictFor("/no-footer", baseline);
    assert.equal(r.verdict, "CATATAN", r.reason);
    assert.ok(r.notes.every((n) => /belum pernah terlihat \(dicek terakhir 2026-11-13\)/.test(n)));
  });

  it("footer seen, then gone two runs in a row → REGRESI both times; back → OK", async () => {
    state.toggleFooter = true;
    const week1 = await verdictFor("/toggle");
    assert.equal(week1.verdict, "HALAMAN OK", week1.reason);
    const base1 = roundTrip(nextBaseline(null, week1.views, "2026-11-13"));

    state.toggleFooter = false;
    const week2 = await verdictFor("/toggle", base1);
    assert.equal(week2.verdict, "REGRESI");
    assert.match(week2.reason, /desktop: footer .* hilang — terakhir terlihat 2026-11-13 \(baseline lengket\)/);
    assert.match(week2.reason, /mobile: footer .* hilang — terakhir terlihat 2026-11-13/);
    const base2 = roundTrip(nextBaseline(base1, week2.views, "2026-11-20"));
    assert.equal(base2.routes["/toggle"].desktop.footer, true, "a missing run does not clear the expectation");

    const week3 = await verdictFor("/toggle", base2);
    assert.equal(week3.verdict, "REGRESI", "still REGRESI on the second consecutive run");
    assert.match(week3.reason, /footer .* hilang — terakhir terlihat 2026-11-13 \(baseline lengket\)/);
    const base3 = roundTrip(nextBaseline(base2, week3.views, "2026-11-27"));

    state.toggleFooter = true;
    const week4 = await verdictFor("/toggle", base3);
    assert.equal(week4.verdict, "HALAMAN OK", week4.reason);
  });

  it("footer gone and its entry removed by hand → CATATAN", async () => {
    state.toggleFooter = true;
    const base1 = roundTrip(nextBaseline(null, (await verdictFor("/toggle")).views, "2026-11-13"));
    const { baseline, removed } = forgetBaseline(base1, "/toggle#footer");
    assert.deepEqual(removed, ["/toggle#footer"]);
    state.toggleFooter = false;
    const r = await verdictFor("/toggle", roundTrip(baseline));
    assert.equal(r.verdict, "CATATAN", r.reason);
    state.toggleFooter = true;
  });

  it("CTA exemption: no CTA is not REGRESI, but the missing footer is still reported", async () => {
    const config = { ctaExempt: [{ route: "/exempt-no-cta", reason: "halaman hasil" }] };
    const without = await verdictFor("/exempt-no-cta");
    assert.equal(without.verdict, "REGRESI");
    assert.match(without.reason, /CTA utama hilang/);
    const r = await verdictFor("/exempt-no-cta", null, config);
    assert.equal(r.verdict, "CATATAN", r.reason);
    assert.deepEqual(r.exempt, ["/exempt-no-cta: halaman hasil"]);
    assert.ok(r.notes.every((n) => /footer .* hilang/.test(n)));
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

  test("nextBaseline is sticky, keeps unmeasured routes and viewports, skips unrendered pages", () => {
    const prev = { routes: { "/a": { desktop: { header: true, footer: true, checkedAt: "old" }, mobile: { header: true, footer: false, checkedAt: "old" } }, "/b": { desktop: { header: false, footer: true, checkedAt: "old" } } } };
    const next = nextBaseline(prev, [view({ footer: false }), view({ route: "/c", navError: "timeout" })], "new");
    assert.deepEqual(next.routes["/a"].desktop, { header: true, footer: true, checkedAt: "new", lastSeen: { header: "new", footer: "old" } });
    assert.equal(next.routes["/a"].mobile.checkedAt, "old");
    assert.ok(next.routes["/b"]);
    assert.equal(next.routes["/c"], undefined);
    assert.equal(prev.routes["/a"].desktop.footer, true, "previous baseline is not mutated");
  });

  test("REGRESI outranks CATATAN; the notes are still reported", () => {
    const baseline = { routes: { "/a": { desktop: { header: false, footer: true, checkedAt: "2026-11-13" } } } };
    const r = judgeViews([view({ header: false, footer: false })], baseline);
    assert.equal(r.verdict, "REGRESI");
    assert.match(r.reason, /footer .* terakhir terlihat 2026-11-13 \(baseline lengket\)/);
    assert.equal(r.notes.length, 1);
    assert.match(r.notes[0], /header .* belum pernah terlihat/);
  });

  test("forgetBaseline drops a whole route or one landmark, and ignores unknown routes", () => {
    const b = { routes: { "/a": { desktop: { header: true, footer: true, checkedAt: "d", lastSeen: { header: "d", footer: "d" } } }, "/b": { desktop: { header: true, footer: true } } } };
    const one = forgetBaseline(b, " /a#footer , /zzz ");
    assert.deepEqual(one.removed, ["/a#footer"]);
    assert.equal(one.baseline.routes["/a"].desktop.footer, false);
    assert.equal(one.baseline.routes["/a"].desktop.header, true);
    assert.equal(b.routes["/a"].desktop.footer, true, "input not mutated");
    assert.equal(forgetBaseline(b, "/b").baseline.routes["/b"], undefined);
  });

  test("a CTA exemption never exempts a landmark the baseline expects", () => {
    const config = { ctaExempt: [{ route: "/a", reason: "r" }] };
    const baseline = { routes: { "/a": { desktop: { header: true, footer: true, checkedAt: "2026-11-13" } } } };
    const r = judgeViews([view({ cta: null, footer: false })], baseline, config);
    assert.equal(r.verdict, "REGRESI");
    assert.doesNotMatch(r.reason, /CTA/);
    assert.match(r.reason, /footer/);
  });

  test("the exemption also matches after a redirect, and with a trailing slash", () => {
    const config = { ctaExempt: [{ route: "/assessment/briefing", reason: "r" }] };
    const r = judgeViews([view({ route: "/old", finalUrl: "https://balizero.com/assessment/briefing/", cta: null })], null, config);
    assert.equal(r.verdict, "HALAMAN OK", r.reason);
  });

  test("config.json exempts /assessment/briefing from the CTA check, with its reason; entries need a reason", () => {
    assert.deepEqual(loadConfig().ctaExempt, [{ route: "/assessment/briefing", reason: "noindex, halaman hasil assessment, bukan halaman konversi" }]);
    const bad = path.join(OUT, "bad-config.json");
    fs.writeFileSync(bad, JSON.stringify({ ctaExempt: [{ route: "/x" }] }));
    assert.throws(() => loadConfig(bad), /butuh route dan reason/);
  });

  test("a network failure stays BELUM DIUKUR whatever the baseline says", () => {
    assert.equal(judgeViews([view({ network: true, navError: "net::ERR_PROXY" })], null).verdict, "BELUM DIUKUR");
  });
});
