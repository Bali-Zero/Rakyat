#!/usr/bin/env node
// Verifikator live: a weekly, read-only check of what the last 7 days of merged PRs look like in production.
//
// For each recent merged PR it answers two questions, separately:
//   1. Is the PR's merge commit an ancestor of the commit production serves?  (TERSAJI / BELUM TERSAJI)
//   2. For a served PR that touches a public route: does the page load with status < 400, zero console errors,
//      a main CTA, a header and a footer, on desktop and on mobile?  (HALAMAN OK / CATATAN / REGRESI)
//      Header = <header>, [role=banner] or a <nav> before <main>. Footer = <footer> or [role=contentinfo].
//      A missing landmark is REGRESI only when the route's baseline (verifikator-out/baseline.json, carried by
//      actions/cache) has it. The baseline is sticky: a landmark seen once stays expected, and stays REGRESI on
//      every run until it is back or its entry is removed by hand (VERIFIKATOR_BASELINE_FORGET). Never seen, or
//      no baseline yet: CATATAN. A gap that already existed is a backlog item, not something a recent PR broke.
//      The main-CTA check skips the routes listed in scripts/verifikator/config.json (ctaExempt, with a reason);
//      landmarks are still checked there.
// Anything it cannot measure is reported as BELUM DIUKUR with the reason. It never claims a page "looks right":
// the semantic check stays with a human, who has the screenshots.
//
// Read-only by construction: GET requests to public endpoints, git reads, `gh` reads. The single write is one
// Todoist task, only when TODOIST_API_TOKEN is set. Client-data surfaces (kita.balizero.com, my.balizero.com,
// /portal, (workspace) routes, /api, /login) are never opened.
//
// Usage: node scripts/verifikator/run.mjs
// Env:   GH_TOKEN (gh reads)  TODOIST_API_TOKEN (optional)  GITHUB_REPOSITORY  GITHUB_STEP_SUMMARY
//        VERIFIKATOR_OUT (default ./verifikator-out; baseline.json is read from and written back to it)
//        VERIFIKATOR_DAYS (default 7)  VERIFIKATOR_MAX_PRS (default 15)
//        VERIFIKATOR_BASELINE_FORGET  comma-separated "<route>" or "<route>#header|footer": baseline entries removed
//                                    before judging, so a landmark dropped on purpose stops being expected.
//        VERIFIKATOR_SKIP_BROWSER=1  skip Playwright (routes become BELUM DIUKUR); for runs from hosts that cannot
//                                    reach balizero.com.
//        VERIFIKATOR_FRONTEND_COMMIT / VERIFIKATOR_BACKEND_SHA  use a served commit read by hand when the health
//                                    endpoint is unreachable from the running host; the report labels it as such.
//        PLAYWRIGHT_MODULE_DIR       directory whose node_modules holds `playwright` (CI installs it out of tree).
//        PLAYWRIGHT_CHANNEL          e.g. "chrome" to launch an installed Chrome instead of the bundled chromium.

import { execFileSync, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const REPO = process.env.GITHUB_REPOSITORY || "Bali-Zero/rakyat";
const OUT = path.resolve(process.env.VERIFIKATOR_OUT || "verifikator-out");
const DAYS = Number(process.env.VERIFIKATOR_DAYS || 7);
const MAX_PRS = Number(process.env.VERIFIKATOR_MAX_PRS || 15);
const MAX_URLS_PER_PR = 2;
const SKIP_BROWSER = process.env.VERIFIKATOR_SKIP_BROWSER === "1";
const FRONTEND_HEALTH = "https://balizero.com/api/health";
const BACKEND_HEALTH = "https://nuzantara-rag.fly.dev/health";
const PUBLIC_ORIGIN = "https://balizero.com";
const APP_DIR = "apps/mouth/src/app";
const TODOIST_PROJECT = "6gwJ6H5xqgJfXHR6";
const TZ = "Asia/Makassar"; // WITA, UTC+8

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

// Hosts and path prefixes the verifikator must never open (client PII lives behind them).
const FORBIDDEN_HOSTS = new Set(["kita.balizero.com", "my.balizero.com"]);
const workspaceRoutes = listDirs(path.join(APP_DIR, "(workspace)")).filter((d) => !d.startsWith("_") && d !== "__tests__");
const PRIVATE_PREFIXES = ["/portal", "/api", "/login", ...workspaceRoutes.map((d) => `/${d}`)];

const V = {
  OK: "HALAMAN OK",
  NOTE: "CATATAN",
  REG: "REGRESI",
  NOT_SERVED: "BELUM TERSAJI",
  NO_SCREEN: "TIDAK BERWUJUD LAYAR",
  UNMEASURED: "BELUM DIUKUR",
};

// ---------------------------------------------------------------------------------------------------------------
// small helpers

function sh(cmd, args, opts = {}) {
  return execFileSync(cmd, args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, ...opts });
}

function listDirs(dir) {
  try {
    return fs.readdirSync(dir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return [];
  }
}

function dateInTz(d, tz = TZ) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

function short(sha) {
  return sha && /^[0-9a-f]{7,40}$/.test(sha) ? sha.slice(0, 7) : sha || "unknown";
}

function cell(s) {
  return String(s ?? "").replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

function isPrivatePath(p) {
  return PRIVATE_PREFIXES.some((pre) => p === pre || p.startsWith(`${pre}/`));
}

// ---------------------------------------------------------------------------------------------------------------
// a. merged PRs

function sinceDate() {
  return dateInTz(new Date(Date.now() - DAYS * 86400_000), "UTC");
}

function fetchMergedPrs(since) {
  const fields = "number,title,body,mergeCommit,mergedAt,files";
  try {
    const raw = sh("gh", [
      "pr", "list", "-R", REPO, "--state", "merged", "--search", `merged:>=${since}`,
      "--limit", "300", "--json", fields,
    ]);
    return { source: "gh pr list", prs: JSON.parse(raw).map(normalizeGraphPr) };
  } catch (err) {
    // `gh pr list` speaks GraphQL. Hosts that only allow the REST API (some sandboxes) fall back to it.
    process.stderr.write(`gh pr list failed (${String(err.message).split("\n")[0]}); falling back to REST\n`);
    return { source: "REST fallback", prs: fetchMergedPrsRest(since) };
  }
}

function normalizeGraphPr(p) {
  return {
    number: p.number,
    title: p.title,
    body: p.body || "",
    mergeCommit: p.mergeCommit?.oid || null,
    mergedAt: p.mergedAt,
    files: (p.files || []).map((f) => f.path),
  };
}

function ghApi(route, ...extra) {
  return JSON.parse(sh("gh", ["api", "-X", "GET", route, ...extra]));
}

function fetchMergedPrsRest(since) {
  const out = [];
  for (let page = 1; page <= 3; page++) {
    const res = ghApi("search/issues", "-f", `q=repo:${REPO} is:pr is:merged merged:>=${since}`, "-f", "per_page=100", "-f", `page=${page}`);
    for (const it of res.items) out.push({ number: it.number, title: it.title, body: it.body || "", mergedAt: it.pull_request?.merged_at });
    if (res.items.length < 100) break;
  }
  return out.map((p) => ({ ...p, mergeCommit: null, files: null }));
}

function hydrateRest(pr) {
  if (pr.files) return pr;
  const detail = ghApi(`repos/${REPO}/pulls/${pr.number}`);
  const files = JSON.parse(sh("gh", ["api", "--paginate", `repos/${REPO}/pulls/${pr.number}/files?per_page=100`, "--jq", "[.[].filename]"])
    .replace(/\]\s*\[/g, ","));
  return { ...pr, mergeCommit: detail.merge_commit_sha, files };
}

// ---------------------------------------------------------------------------------------------------------------
// classification

const NON_VISUAL_FILE = [
  /^\.github\//, /(^|\/)__tests__\//, /\.(test|spec)\.[cm]?[jt]sx?$/, /(^|\/)tests?\//, /(^|\/)e2e\//,
  /^docs\//, /\.md$/i, /^scripts\/ci\//, /^PERIMETER\.txt$/, /^research\//, /(^|\/)test_[^/]+\.py$/,
  /(^|\/)conftest\.py$/, /\.snap$/,
];
const NON_VISUAL_TYPE = /^(test|ci|build|refactor)(\([^)]*\))?!?:/i;

export function classify(pr) {
  const files = pr.files || [];
  const backend = files.some((f) => f.startsWith("apps/backend-rag/"));
  const frontend = files.some((f) => f.startsWith("apps/mouth/") || f.startsWith("packages/"));
  const nonVisualFiles = files.length > 0 && files.every((f) => NON_VISUAL_FILE.some((re) => re.test(f)));
  const nonVisualType = NON_VISUAL_TYPE.test(pr.title);
  const routes = mapRoutes(files);
  return { backend, frontend, nonVisual: nonVisualFiles || nonVisualType, nonVisualWhy: nonVisualFiles ? "hanya CI/tes/dokumen" : nonVisualType ? `tipe "${pr.title.split(/[(:]/)[0]}"` : "", routes };
}

// Priority for the 15-PR cut: public route > other frontend > rest > private-only, newest first within a tier.
// Private-only PRs go last: they are never opened, so they would only fill the cut with BELUM DIUKUR rows.
function priority(pr, c) {
  if (c.routes.publicStatic.length || c.routes.publicDynamic.length) return 0;
  const privateOnly = c.routes.private.length > 0 && !c.routes.unmapped;
  if (c.frontend && !privateOnly) return 1;
  if (!privateOnly) return 2;
  return 3;
}

// ---------------------------------------------------------------------------------------------------------------
// d. route mapping

function hasPage(dir) {
  return ["page.tsx", "page.ts", "page.jsx", "page.js", "page.mdx"].some((f) => fs.existsSync(path.join(dir, f)));
}

function dirToRoute(relDir) {
  const segs = relDir.split("/").filter(Boolean);
  const kept = [];
  let dynamic = false;
  let privateGroup = false;
  for (const s of segs) {
    if (s === "(workspace)") privateGroup = true;
    if (/^\(.*\)$/.test(s) || s.startsWith("@")) continue; // route groups and parallel slots
    if (s.startsWith("_")) return null; // private folder: not a route
    if (/^\[.*\]$/.test(s)) dynamic = true;
    kept.push(s);
  }
  const route = `/${kept.join("/")}`;
  return { route, dynamic, private: privateGroup || isPrivatePath(route) };
}

export function mapRoutes(files) {
  const out = { publicStatic: new Set(), publicDynamic: new Set(), private: new Set(), unmapped: 0 };
  for (const f of files) {
    if (NON_VISUAL_FILE.some((re) => re.test(f))) continue;
    // Content that renders on a known public URL. Locale variants (<slug>.<lang>.mdx) have no URL derivable here.
    const art = f.match(/^apps\/mouth\/src\/content\/articles\/([^/]+)\/([^/.]+)\.mdx$/);
    if (art) {
      out.publicStatic.add(`/${art[1]}/${art[2]}`); // the site may redirect to the canonical category; Playwright follows
      continue;
    }
    if (f === "apps/mouth/src/content/homepage-layout.json") {
      out.publicStatic.add("/");
      continue;
    }
    if (!f.startsWith(`${APP_DIR}/`)) continue;
    const rel = f.slice(APP_DIR.length + 1);
    // API handlers are never pages, and must not walk up into a page directory (api/prime/... is not /prime).
    if (rel.split("/").filter((x) => !/^\(.*\)$/.test(x))[0] === "api" || /(^|\/)route\.[cm]?[jt]sx?$/.test(rel)) {
      out.private.add("/api");
      continue;
    }
    if (/^(globals\.css|layout\.tsx|not-found\.tsx|error\.tsx|loading\.tsx)$/.test(rel)) {
      out.publicStatic.add("/");
      continue;
    }
    let dir = path.posix.dirname(rel);
    let found = null;
    while (true) {
      if (hasPage(path.join(APP_DIR, dir === "." ? "" : dir))) {
        found = dir === "." ? "" : dir;
        break;
      }
      if (dir === "." || dir === "") break;
      dir = path.posix.dirname(dir);
    }
    if (found === null) {
      out.unmapped++;
      continue;
    }
    const r = dirToRoute(found);
    if (!r) out.unmapped++;
    else if (r.private) out.private.add(r.route);
    else if (r.dynamic) out.publicDynamic.add(r.route);
    else out.publicStatic.add(r.route);
  }
  return {
    publicStatic: [...out.publicStatic],
    publicDynamic: [...out.publicDynamic],
    private: [...out.private],
    unmapped: out.unmapped,
  };
}

export function routePattern(route) {
  const src = route
    .split("/")
    .map((s) => (/^\[\[?\.\.\..*\]\]?$/.test(s) ? ".+" : /^\[.*\]$/.test(s) ? "[^/]+" : s.replace(/[.*+?^${}()|\\]/g, "\\$&")))
    .join("/");
  return new RegExp(`^${src}/?$`);
}

export function bodyUrls(body) {
  const found = [];
  for (const m of (body || "").matchAll(/https?:\/\/(?:www\.)?balizero\.com(\/[^\s)\]>"'`<]*)?/gi)) {
    let p = (m[1] || "/").replace(/[.,;:!?]+$/, "");
    try {
      const u = new URL(p, PUBLIC_ORIGIN);
      if (FORBIDDEN_HOSTS.has(u.hostname) || isPrivatePath(u.pathname)) continue;
      if (/[[\]]/.test(u.pathname)) continue;
      found.push(u.pathname + u.search);
    } catch {
      /* not a URL */
    }
  }
  return [...new Set(found)];
}

// ---------------------------------------------------------------------------------------------------------------
// b. served commits

async function fetchJson(url) {
  const res = await fetch(url, { headers: { "user-agent": "bali-zero-verifikator/1 (read-only)" }, signal: AbortSignal.timeout(20_000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function servedCommits() {
  const out = { frontend: null, frontendSource: "", backend: null, backendSource: "", errors: [] };
  try {
    const j = await fetchJson(FRONTEND_HEALTH);
    out.frontend = j.commit || null;
    out.frontendSource = FRONTEND_HEALTH;
  } catch (e) {
    out.errors.push(`${FRONTEND_HEALTH}: ${e.message}`);
  }
  try {
    const j = await fetchJson(BACKEND_HEALTH);
    out.backend = j.build_sha || "unknown";
    out.backendSource = BACKEND_HEALTH;
  } catch (e) {
    out.errors.push(`${BACKEND_HEALTH}: ${e.message}`);
  }
  if (!out.frontend && process.env.VERIFIKATOR_FRONTEND_COMMIT) {
    out.frontend = process.env.VERIFIKATOR_FRONTEND_COMMIT;
    out.frontendSource = "override VERIFIKATOR_FRONTEND_COMMIT (dibaca manual)";
  }
  if (!out.backend && process.env.VERIFIKATOR_BACKEND_SHA) {
    out.backend = process.env.VERIFIKATOR_BACKEND_SHA;
    out.backendSource = "override VERIFIKATOR_BACKEND_SHA (dibaca manual)";
  }
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// c. ancestry

const SHA_RE = /^[0-9a-f]{40}$/;

// true / false / null (cannot tell: commit missing from the checkout, or no served commit)
function isAncestor(a, b) {
  if (!a || !b || !SHA_RE.test(a) || !SHA_RE.test(b)) return null;
  const r = spawnSync("git", ["merge-base", "--is-ancestor", a, b]);
  if (r.status === 0) return true;
  if (r.status === 1) return false;
  return null;
}

function controls(served) {
  // Positive control: a commit is its own ancestor. Negative control: a commit is not an ancestor of its parent.
  const res = [];
  for (const [name, sha] of [["frontend", served.frontend], ["backend", served.backend]]) {
    if (!sha || !SHA_RE.test(sha)) {
      res.push({ name, ok: null, note: `tidak ada commit (${sha || "kosong"})` });
      continue;
    }
    const pos = isAncestor(sha, sha);
    let parent = null;
    try {
      parent = sh("git", ["rev-parse", `${sha}^`]).trim();
    } catch {
      /* shallow or root */
    }
    const neg = parent ? isAncestor(sha, parent) : null;
    res.push({ name, ok: pos === true && neg !== true, note: `positif ${pos} · negatif ${neg === null ? "n/a" : !neg}` });
  }
  return res;
}

// ---------------------------------------------------------------------------------------------------------------
// e. Playwright

export function loadPlaywright() {
  const dir = process.env.PLAYWRIGHT_MODULE_DIR;
  const req = createRequire(dir ? path.join(path.resolve(dir), "noop.js") : import.meta.url);
  return req("playwright");
}

const NETWORK_ERR = /ERR_(TUNNEL_CONNECTION_FAILED|PROXY|NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|CONNECTION_REFUSED|ADDRESS_UNREACHABLE)/;
const CTA_TEXT = /whatsapp|contact|book|consult|get started|start now|talk to|hubungi|konsultasi|chat with/i;
const CTA_HREF = /wa\.me\/|api\.whatsapp\.com|whatsapp:/i;
const BASELINE_FILE = "baseline.json";
const CONFIG_FILE = new URL("./config.json", import.meta.url);

// Runs in the page. Header: <header>, [role=banner], or a <nav> that comes before <main> in document order
// (article layouts carry the site navigation in a bare <nav>). Footer: <footer> or [role=contentinfo].
// Main CTA: a link or button whose text, aria-label or WhatsApp href matches, inside <main> or the header area.
function readLandmarks({ ctaText, ctaHref }) {
  const textRe = new RegExp(ctaText, "i");
  const hrefRe = new RegExp(ctaHref, "i");
  const main = document.querySelector("main");
  const before = (el) => main && !el.contains(main) && !main.contains(el) && Boolean(el.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING);
  const headerEl = document.querySelector("header") ? "header" : document.querySelector("[role=banner]") ? "[role=banner]" : null;
  const navs = [...document.querySelectorAll("nav")].filter(before);
  const headerVia = headerEl || (navs.length ? "nav" : null);
  const scopes = [main, ...document.querySelectorAll("header, [role=banner]"), ...navs].filter(Boolean);
  let cta = null;
  for (const scope of scopes) {
    for (const el of scope.querySelectorAll("a, button")) {
      const label = `${el.textContent || ""} ${el.getAttribute("aria-label") || ""}`.replace(/\s+/g, " ").trim();
      if (textRe.test(label) || hrefRe.test(el.getAttribute("href") || "")) {
        cta = label.slice(0, 40) || (el.getAttribute("href") || "").slice(0, 40);
        break;
      }
    }
    if (cta) break;
  }
  return { header: Boolean(headerVia), headerVia, footer: Boolean(document.querySelector("footer, [role=contentinfo]")), cta };
}

export async function checkPage(browser, urlPath, prNumber, idx, origin = PUBLIC_ORIGIN) {
  const url = new URL(urlPath, origin).toString();
  const views = [];
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, isMobile: vp.name === "mobile", hasTouch: vp.name === "mobile" });
    const page = await ctx.newPage();
    const consoleErrors = [];
    page.on("console", (m) => {
      if (m.type() === "error") consoleErrors.push(m.text().slice(0, 160));
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e.message).slice(0, 160)}`));
    const shot = path.join(OUT, `PR${prNumber}${idx ? `_u${idx + 1}` : ""}_${vp.name}.png`);
    const v = { viewport: vp.name, route: urlPath, url, status: null, header: false, headerVia: null, footer: false, cta: null, consoleErrors, shot: null, navError: null, network: false };
    try {
      const resp = await page.goto(url, { waitUntil: "load", timeout: 45_000 });
      v.status = resp ? resp.status() : null;
      v.finalUrl = page.url();
      await page.waitForTimeout(2_500); // let hydration and late console errors land
      Object.assign(v, await page.evaluate(readLandmarks, { ctaText: CTA_TEXT.source, ctaHref: CTA_HREF.source }));
      await page.screenshot({ path: shot, fullPage: true });
      v.shot = path.basename(shot);
    } catch (e) {
      v.navError = String(e.message).split("\n")[0].slice(0, 160);
      v.network = NETWORK_ERR.test(v.navError);
    }
    await ctx.close();
    views.push(v);
  }
  return views;
}

// ---------------------------------------------------------------------------------------------------------------
// landmark baseline: what each route showed on the previous run

// { version, updatedAt, routes: { "<path>": { "<viewport>": { header, footer, checkedAt } } } } or null.
export function readBaseline(file) {
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch {
    return { baseline: null, note: "tidak ada (run pertama atau cache kosong)" };
  }
  try {
    const b = JSON.parse(raw);
    if (!b || typeof b.routes !== "object" || b.routes === null) throw new Error("tanpa routes");
    return { baseline: b, note: `${Object.keys(b.routes).length} rute, diperbarui ${b.updatedAt || "?"}` };
  } catch (e) {
    return { baseline: null, note: `tidak terbaca (${String(e.message).slice(0, 80)}) — diperlakukan sebagai run pertama` };
  }
}

// Only views that actually rendered the page (no navigation error, status < 400) say anything about landmarks.
function measurable(v) {
  return !v.navError && v.status !== null && v.status < 400;
}

// The next baseline: the previous one plus what this run measured. Sticky: a landmark seen once stays expected
// (true) whatever later runs show; only forgetBaseline() takes it back. lastSeen keeps the date each landmark was
// last observed, for the REGRESI message. Judgement always uses the previous baseline, never a value written
// earlier in the same run.
export function nextBaseline(prev, views, today) {
  const routes = structuredClone(prev?.routes || {});
  for (const v of views) {
    if (!v.route || !measurable(v)) continue;
    const old = routes[v.route]?.[v.viewport] || {};
    const entry = { checkedAt: today, lastSeen: { ...(old.lastSeen || {}) } };
    for (const [key] of LANDMARKS) {
      entry[key] = old[key] === true || v[key] === true;
      if (v[key]) entry.lastSeen[key] = today;
      else if (old[key] === true && !entry.lastSeen[key]) entry.lastSeen[key] = old.checkedAt || "?"; // pre-lastSeen file
    }
    routes[v.route] = { ...(routes[v.route] || {}), [v.viewport]: entry };
  }
  return { version: 2, updatedAt: today, routes };
}

// Manual removal: "<route>" drops the whole route, "<route>#footer" only that landmark's expectation.
export function forgetBaseline(baseline, spec) {
  const removed = [];
  if (!baseline || !spec) return { baseline, removed };
  const out = structuredClone(baseline);
  for (const item of spec.split(",").map((x) => x.trim()).filter(Boolean)) {
    const [route, landmark] = item.split("#");
    if (!out.routes[route]) continue;
    if (!landmark) {
      delete out.routes[route];
      removed.push(route);
      continue;
    }
    for (const entry of Object.values(out.routes[route])) {
      entry[landmark] = false;
      if (entry.lastSeen) delete entry.lastSeen[landmark];
    }
    removed.push(item);
  }
  return { baseline: out, removed };
}

// scripts/verifikator/config.json: { ctaExempt: [{ route, reason }] }. Every exemption must say why.
export function loadConfig(file = CONFIG_FILE) {
  const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
  const ctaExempt = cfg.ctaExempt || [];
  for (const e of ctaExempt) {
    if (typeof e.route !== "string" || !e.route.startsWith("/") || typeof e.reason !== "string" || !e.reason.trim()) {
      throw new Error(`config ctaExempt butuh route dan reason: ${JSON.stringify(e)}`);
    }
  }
  return { ctaExempt };
}

function normPath(p) {
  return p.length > 1 ? p.replace(/\/+$/, "") : p;
}

// Matches the requested route or, after a redirect, the final path.
export function ctaExemption(v, config) {
  const paths = [v.route, v.finalUrl ? new URL(v.finalUrl).pathname : null].filter(Boolean).map(normPath);
  return (config?.ctaExempt || []).find((e) => paths.includes(normPath(e.route))) || null;
}

const LANDMARKS = [
  ["header", "header (<header>/[role=banner]/<nav> sebelum <main>)"],
  ["footer", "footer (<footer>/[role=contentinfo])"],
];

export function judgeViews(views, baseline = null, config = null) {
  if (views.some((v) => v.network)) {
    return { verdict: V.UNMEASURED, reason: "jaringan runner tidak menjangkau balizero.com", notes: [], exempt: [] };
  }
  const exempt = new Set();
  const fails = [];
  const notes = [];
  for (const v of views) {
    const tag = views.some((o) => o.route !== v.route) ? `${v.route} ${v.viewport}` : v.viewport;
    if (v.navError) {
      fails.push(`${tag}: navigasi gagal (${v.navError})`);
      continue;
    }
    if (v.status === null || v.status >= 400) {
      fails.push(`${tag}: HTTP ${v.status}`); // an error page's landmarks say nothing about the page
      continue;
    }
    if (v.consoleErrors.length) fails.push(`${tag}: ${v.consoleErrors.length} console error ("${v.consoleErrors[0].slice(0, 80)}")`);
    const ex = ctaExemption(v, config);
    if (ex) exempt.add(`${ex.route}: ${ex.reason}`);
    else if (!v.cta) fails.push(`${tag}: CTA utama hilang`);
    const before = baseline?.routes?.[v.route]?.[v.viewport];
    for (const [key, label] of LANDMARKS) {
      if (v[key]) continue;
      if (before?.[key] === true) fails.push(`${tag}: ${label} hilang — terakhir terlihat ${before.lastSeen?.[key] || before.checkedAt || "?"} (baseline lengket)`);
      else if (before) notes.push(`${tag}: ${label} hilang — belum pernah terlihat (dicek terakhir ${before.checkedAt || "?"})`);
      else notes.push(`${tag}: ${label} hilang — belum ada baseline untuk rute ini`);
    }
  }
  const ex = [...exempt];
  if (fails.length) return { verdict: V.REG, reason: fails.join("; "), notes, exempt: ex };
  if (notes.length) return { verdict: V.NOTE, reason: "", notes, exempt: ex };
  return { verdict: V.OK, reason: "", notes, exempt: ex };
}

// ---------------------------------------------------------------------------------------------------------------
// f. verdict per PR

function ancestryFor(pr, c, served) {
  const parts = [];
  let state = true; // true = served, false = not, null = unknown
  const need = [];
  if (c.backend) need.push(["backend", served.backend]);
  if (c.frontend || !c.backend) need.push(["frontend", served.frontend]);
  for (const [name, sha] of need) {
    if (name === "backend" && (!sha || sha === "unknown")) {
      parts.push("backend: build_sha unknown");
      state = state === false ? false : null;
      continue;
    }
    const a = isAncestor(pr.mergeCommit, sha);
    parts.push(`${name}: ${a === null ? "tak terukur" : a ? "TERSAJI" : "BELUM TERSAJI"} @${short(sha)}`);
    if (a === false) state = false;
    else if (a === null && state !== false) state = null;
  }
  return { state, text: parts.join(" · ") };
}

// ---------------------------------------------------------------------------------------------------------------
// g. report

function renderReport(ctx) {
  const { rows, served, ctrl, today, totalMerged, skipped, source, baselineInfo } = ctx;
  const count = (v) => rows.filter((r) => r.verdict === v).length;
  const head =
    `**Verifikator live ${today}** — ${rows.length} PR diperiksa (dari ${totalMerged} merged ${DAYS} hari terakhir) · ` +
    `${V.OK} ${count(V.OK)} · ${V.NOTE} ${count(V.NOTE)} · ${V.REG} ${count(V.REG)} · ${V.NOT_SERVED} ${count(V.NOT_SERVED)} · ` +
    `${V.NO_SCREEN} ${count(V.NO_SCREEN)} · ${V.UNMEASURED} ${count(V.UNMEASURED)} · ` +
    `frontend \`${short(served.frontend)}\` · backend build_sha \`${short(served.backend)}\``;
  const lines = [head, ""];
  lines.push("| PR | Title | URL | Ancestry | Verdict | Evidence |", "|---|---|---|---|---|---|");
  for (const r of rows) {
    lines.push(`| #${r.number} | ${cell(r.title)} | ${cell(r.urls.join("<br>") || "—")} | ${cell(r.ancestry)} | **${r.verdict}** | ${cell(r.evidence)} |`);
  }
  const noted = rows.filter((r) => r.notes?.length);
  lines.push("", "**Catatan**");
  if (!noted.length) lines.push("- tidak ada landmark yang hilang tanpa pernah terlihat");
  else {
    lines.push("_Landmark yang hilang tetapi belum pernah terlihat di rute ini (atau rute belum punya baseline). Bukan regresi PR ini; backlog perbaikan layout._");
    for (const r of noted) for (const n of r.notes) lines.push(`- #${r.number}: ${n}`);
  }
  lines.push("", "**Kontrol ancestry**");
  for (const c of ctrl) lines.push(`- ${c.name}: ${c.ok === null ? "tidak terukur" : c.ok ? "lulus" : "GAGAL"} (${c.note})`);
  lines.push("", "**Sumber**");
  lines.push(`- frontend commit: \`${served.frontend || "—"}\` dari ${served.frontendSource || "— (gagal)"}`);
  lines.push(`- backend build_sha: \`${served.backend || "—"}\` dari ${served.backendSource || "— (gagal)"}`);
  for (const e of served.errors) lines.push(`- health tidak terjangkau dari host ini: ${e}`);
  lines.push(`- baseline landmark (lengket): ${baselineInfo.before} → ${baselineInfo.after}`);
  if (baselineInfo.forgotten.length) lines.push(`- dihapus manual dari baseline (VERIFIKATOR_BASELINE_FORGET): ${baselineInfo.forgotten.join(", ")}`);
  for (const e of ctx.config.ctaExempt) lines.push(`- cek CTA dikecualikan: ${e.route} — ${e.reason} (landmark tetap dicek)`);
  lines.push(`- daftar PR: ${source}${SKIP_BROWSER ? " · browser dilewati (VERIFIKATOR_SKIP_BROWSER=1)" : ""}`);
  if (skipped.length) lines.push(`- ${skipped.length} PR merged lain di luar batas ${MAX_PRS} (prioritas rendah): ${skipped.map((p) => `#${p.number}`).join(", ")}`);
  lines.push("", "_Verdict mesin: status, console, CTA utama, header, footer (landmark dibandingkan dengan baseline). Apakah halaman benar secara isi tetap dicek manusia lewat screenshot di artifact._");
  return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------------------------------------------------------
// h. Todoist (single write, optional)

async function createTodoistTask(ctx, reportUrl) {
  const token = process.env.TODOIST_API_TOKEN;
  if (!token) return "TODOIST_API_TOKEN tidak ada — task dilewati";
  const { rows, today, served } = ctx;
  const regs = rows.filter((r) => r.verdict === V.REG);
  const notes = rows.flatMap((r) => (r.notes || []).map((n) => `#${r.number} ${n}`));
  const count = (v) => rows.filter((r) => r.verdict === v).length;
  const desc = [
    `PR diperiksa: ${rows.length} · OK ${count(V.OK)} · CATATAN ${count(V.NOTE)} · REGRESI ${regs.length} · BELUM TERSAJI ${count(V.NOT_SERVED)} · TIDAK BERWUJUD LAYAR ${count(V.NO_SCREEN)} · BELUM DIUKUR ${count(V.UNMEASURED)}`,
    `Frontend ${short(served.frontend)} · backend build_sha ${short(served.backend)}`,
    regs.length ? "REGRESI:" : "REGRESI: tidak ada",
    ...regs.slice(0, 8).map((r) => `- #${r.number} ${r.urls[0] || ""}: ${r.evidence.slice(0, 120)}`),
    regs.length > 8 ? `- +${regs.length - 8} lainnya di laporan` : null,
    notes.length ? `Catatan landmark (bukan regresi): ${notes.length}, mis. ${notes[0].slice(0, 110)}` : null,
    `Run: ${reportUrl}`,
    "Screenshot ada di artifact run. Cek isi halaman tetap manual.",
  ].filter(Boolean).slice(0, 15);
  // 16:00 WITA (UTC+8) == 08:00 UTC on the same calendar day.
  const body = {
    content: `[UNTUK SECRETARY] Verifikasi live mingguan ${today}`,
    description: desc.join("\n"),
    project_id: TODOIST_PROJECT,
    labels: ["Bali-Zero"],
    priority: regs.length ? 4 : 3, // API: 4 = p1, 3 = p2. p1 only on REGRESI; CATATAN alone stays p2.
    due_datetime: `${today}T08:00:00Z`,
  };
  const res = await fetch("https://api.todoist.com/api/v1/tasks", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(20_000),
  });
  const text = await res.text();
  if (!res.ok) return `Todoist gagal: HTTP ${res.status} ${text.slice(0, 200)}`;
  return `Todoist task dibuat: ${JSON.parse(text).id}`;
}

// ---------------------------------------------------------------------------------------------------------------
// main

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const today = dateInTz(new Date());
  const since = sinceDate();

  const { source, prs: listed } = fetchMergedPrs(since);
  const totalMerged = listed.length;

  // Classify cheaply first (REST fallback needs a files call per PR, so hydrate lazily in priority order).
  const ranked = listed
    .map((pr) => ({ pr, hint: /\b(mouth|seo|homepage|article|ui|page|landing|mobile)\b/i.test(pr.title) ? 0 : 1 }))
    .sort((a, b) => a.hint - b.hint || String(b.pr.mergedAt).localeCompare(String(a.pr.mergedAt)));
  const hydrated = [];
  for (const { pr } of ranked) {
    if (source !== "gh pr list" && hydrated.length >= MAX_PRS * 3) break;
    const full = hydrateRest(pr);
    hydrated.push({ pr: full, c: classify(full) });
  }
  hydrated.sort((a, b) => priority(a.pr, a.c) - priority(b.pr, b.c) || String(b.pr.mergedAt).localeCompare(String(a.pr.mergedAt)));
  const picked = hydrated.slice(0, MAX_PRS);
  const skipped = listed.filter((p) => !picked.some((x) => x.pr.number === p.number));

  const baselineFile = path.join(OUT, BASELINE_FILE);
  const config = loadConfig();
  const read = readBaseline(baselineFile);
  const { baseline, removed: forgotten } = forgetBaseline(read.baseline, process.env.VERIFIKATOR_BASELINE_FORGET);
  const baselineNote = read.note;
  const measuredViews = [];

  const served = await servedCommits();
  const ctrl = controls(served);
  const ancestryTrusted = ctrl.every((c) => c.ok !== false);

  let browser = null;
  let browserError = null;
  const needsBrowser = !SKIP_BROWSER;
  async function getBrowser() {
    if (browser || browserError || !needsBrowser) return browser;
    try {
      browser = await loadPlaywright().chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {});
    } catch (e) {
      browserError = String(e.message).split("\n")[0];
    }
    return browser;
  }

  const rows = [];
  for (const { pr, c } of picked) {
    const anc = ancestryFor(pr, c, served);
    // A PR-body URL is used only as the concrete instance of one of the PR's own dynamic routes.
    const bodyU = bodyUrls(pr.body).filter((u) => c.routes.publicDynamic.some((r) => routePattern(r).test(u.split("?")[0])));
    const urls = [...new Set([...c.routes.publicStatic, ...bodyU])].slice(0, MAX_URLS_PER_PR);
    const row = { number: pr.number, title: pr.title, urls: [], ancestry: anc.text, verdict: V.UNMEASURED, evidence: "", notes: [] };

    if (!ancestryTrusted) {
      row.evidence = "kontrol ancestry gagal — ancestry tidak dipercaya";
    } else if (anc.state === false) {
      row.verdict = V.NOT_SERVED;
      row.evidence = `merge ${short(pr.mergeCommit)} belum ada di commit yang disajikan`;
    } else if (c.nonVisual) {
      row.verdict = V.NO_SCREEN;
      row.evidence = c.nonVisualWhy;
    } else if (anc.state === null) {
      row.evidence = c.backend && (!served.backend || served.backend === "unknown") ? "build_sha unknown" : "ancestry tak terukur (commit tidak ada di checkout atau health gagal)";
    } else if (!urls.length) {
      if (c.routes.publicDynamic.length) row.evidence = `rute dinamis ${c.routes.publicDynamic.join(", ")} tanpa URL konkret di body PR`;
      else if (c.routes.private.length) row.evidence = `rute privat ${c.routes.private.slice(0, 3).join(", ")} — sengaja tidak dibuka`;
      else if (c.backend && !c.frontend) row.evidence = "perubahan backend, tanpa URL publik di body PR";
      else row.evidence = "tidak ada rute publik terpetakan (komponen/data bersama)";
    } else {
      row.urls = urls;
      const b = await getBrowser();
      if (!b) {
        row.evidence = SKIP_BROWSER ? "browser dilewati (host tidak menjangkau balizero.com)" : `Playwright tidak jalan: ${browserError}`;
      } else {
        const allViews = [];
        for (let i = 0; i < urls.length; i++) allViews.push(...(await checkPage(b, urls[i], pr.number, i)));
        measuredViews.push(...allViews);
        const j = judgeViews(allViews, baseline, config);
        row.verdict = j.verdict;
        row.notes = j.notes;
        if (j.verdict === V.UNMEASURED) {
          row.evidence = j.reason;
        } else {
          const shots = allViews.filter((v) => v.shot).map((v) => v.shot);
          const stat = allViews.map((v) => `${v.viewport} ${v.status ?? "—"}`).join(", ");
          const cta = allViews.find((v) => v.cta)?.cta;
          const moved = [...new Set(allViews.filter((v) => v.finalUrl && v.finalUrl !== v.url).map((v) => `→ ${new URL(v.finalUrl).pathname}`))];
          const navHeader = allViews.some((v) => v.headerVia === "nav") ? "header = <nav> sebelum <main>" : null;
          const exRef = j.exempt.length ? `CTA dikecualikan (${j.exempt.join("; ")})` : null;
          const noteRef = j.notes.length ? `${j.notes.length} catatan landmark (lihat Catatan)` : null;
          row.evidence = [j.reason, `status ${stat}`, ...moved, `CTA: ${cta ? `"${cta}"` : "tidak terdeteksi"}`, exRef, navHeader, noteRef, shots.join(", ") || null].filter(Boolean).join(" · ");
        }
      }
    }
    rows.push(row);
  }
  if (browser) await browser.close();

  const next = nextBaseline(baseline, measuredViews, today);
  fs.writeFileSync(baselineFile, JSON.stringify(next, null, 2) + "\n");
  const baselineInfo = { before: baselineNote, after: `${Object.keys(next.routes).length} rute ditulis ke ${BASELINE_FILE}`, forgotten };

  const ctx = { rows, served, ctrl, today, totalMerged, skipped, source, baselineInfo, config };
  const report = renderReport(ctx);
  fs.writeFileSync(path.join(OUT, "report.md"), report);
  fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify({ ...ctx, skipped: skipped.map((p) => p.number) }, null, 2));
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, report);

  const runUrl = process.env.GITHUB_RUN_ID
    ? `${process.env.GITHUB_SERVER_URL}/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`
    : "(lokal, tanpa run URL)";
  const todo = await createTodoistTask(ctx, runUrl);
  process.stdout.write(report + "\n" + todo + "\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((e) => {
    process.stderr.write(`verifikator crashed: ${e.stack || e}\n`);
    process.exit(1);
  });
}
