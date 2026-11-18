import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { INTERNAL_ROUTES } from "@/proxy";
import {
  PUBLIC_TWO_SEGMENT_ROUTES,
  classifyTwoSegmentPath,
} from "./two-segment-gate";

const APP_DIR = path.join(process.cwd(), "src/app");

const ROUTE_FILE = /^(page|route)\.(tsx?|jsx?|mjs|cjs|mdx)$/;

/**
 * URL routes under src/app, two segments deep. Route groups add no segment,
 * private folders (`_x`) are not routes, and parallel-route slots and
 * intercepting markers (`@panel`, `(.)x`) are not URLs of their own.
 * scripts/lib/app-routes.mjs refuses those conventions, and kbli/@panel is one.
 */
function appRoutes(dir: string, segments: string[] = []): string[] {
  const out: string[] = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  if (entries.some((e) => e.isFile() && ROUTE_FILE.test(e.name))) {
    out.push(segments.join("/"));
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    if (e.name.startsWith("_") || e.name.startsWith("@")) continue;
    if (/^\(\.{1,3}\)/.test(e.name)) continue;
    const isGroup = e.name.startsWith("(") && e.name.endsWith(")");
    out.push(
      ...appRoutes(
        path.join(dir, e.name),
        isGroup ? segments : [...segments, e.name],
      ),
    );
  }
  return out;
}

/**
 * What the proxy hands to rendering on the public domain: everything under
 * src/app except the paths it redirects first (workspace routes, /portal,
 * /chat) and the API.
 */
function publicTwoSegmentRoutes(): Record<string, string[] | "*"> {
  const redirected = new Set([
    ...INTERNAL_ROUTES.map((r) => r.slice(1)),
    "portal",
    "chat",
    "api",
  ]);
  const out: Record<string, string[] | "*"> = {};
  for (const route of appRoutes(APP_DIR)) {
    const segments = route.split("/");
    if (segments.length !== 2) continue;
    const [first, second] = segments;
    if (first === "[category]" || redirected.has(first)) continue;
    if (second.startsWith("[")) out[first] = "*";
    else if (out[first] !== "*") out[first] = [...(out[first] ?? []), second];
  }
  return out;
}

describe("PUBLIC_TWO_SEGMENT_ROUTES mirrors src/app", () => {
  it("lists every two-segment route the app serves, and only those", () => {
    const fromTree = publicTwoSegmentRoutes();
    const sorted = (t: Record<string, readonly string[] | "*">) =>
      Object.fromEntries(
        Object.keys(t)
          .sort()
          .map((k) => [k, t[k] === "*" ? "*" : [...(t[k] as string[])].sort()]),
      );
    expect(sorted(PUBLIC_TWO_SEGMENT_ROUTES)).toEqual(sorted(fromTree));
  });

  it("[guilt control] the comparison sees a route the table lacks", () => {
    const fromTree = publicTwoSegmentRoutes();
    fromTree["invented-route"] = ["page"];
    expect(Object.keys(fromTree)).not.toEqual(
      Object.keys(PUBLIC_TWO_SEGMENT_ROUTES),
    );
  });
});

describe("classifyTwoSegmentPath", () => {
  it.each([
    "/foo/bar",
    "/company/about",
    "/visa/nonexistent-xyz",
    "/news/nonexistent-xyz",
  ])("%s has no owner and no article category: not-found", (p) => {
    expect(classifyTwoSegmentPath(p)).toBe("not-found");
  });

  it.each([
    "/visas/nonexistent-xyz",
    "/business/x",
    "/taxes/x",
    "/property/x",
    "/living/x",
    "/trends/x",
  ])("%s is under an article category: left to the page", (p) => {
    expect(classifyTwoSegmentPath(p)).toBe("pass");
  });

  it.each([
    "/visa/second-home",
    "/visa/voa",
    "/visa/clock",
    "/visa/match",
    "/taxes/gap",
    "/property/eligibility",
    "/services/visa",
    "/kbli/56101",
    "/visa-oracle/unlock",
  ])("%s belongs to a real route: pass", (p) => {
    expect(classifyTwoSegmentPath(p)).toBe("pass");
  });

  it.each([
    "/",
    "/visas",
    "/news",
    "/visa/voa/abc123",
    "/visa/clock/abc123",
    "/visa/match/abc123",
    "/abc123",
    "/visas/some-article/extra",
  ])("%s is not a two-segment path: pass", (p) => {
    expect(classifyTwoSegmentPath(p)).toBe("pass");
  });

  it("ignores a trailing slash and a dotted second segment", () => {
    expect(classifyTwoSegmentPath("/visa/voa/")).toBe("pass");
    expect(classifyTwoSegmentPath("/foo/bar/")).toBe("not-found");
    expect(classifyTwoSegmentPath("/foo/file.txt")).toBe("pass");
  });

  it("does not read inherited object keys as owners or categories", () => {
    expect(classifyTwoSegmentPath("/constructor/x")).toBe("not-found");
    expect(classifyTwoSegmentPath("/toString/x")).toBe("not-found");
  });
});

/**
 * The proxy runs before next.config rewrites and before public/ is served, so
 * the owner table alone cannot prove the gate 404s only non-pages. Two
 * mechanical facts close that, and both are asserted from the files.
 */
describe("the gate cannot 404 anything that is not a page route", () => {
  const MOUTH = process.cwd();

  it("no rewrite exists to be shadowed (next.config.ts and vercel.json)", () => {
    const cfg = fs.readFileSync(path.join(MOUTH, "next.config.ts"), "utf8");
    expect(cfg).not.toMatch(/^\s*(async\s+)?rewrites\s*\(/m);
    const vercel = JSON.parse(
      fs.readFileSync(path.join(MOUTH, "vercel.json"), "utf8"),
    );
    expect(vercel.rewrites ?? []).toEqual([]);
  });

  it("every file under public/, at any depth, passes the gate", () => {
    const files: string[] = [];
    const walk = (dir: string, rel: string) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const r = `${rel}/${e.name}`;
        if (e.isDirectory()) walk(path.join(dir, e.name), r);
        else files.push(r);
      }
    };
    walk(path.join(MOUTH, "public"), "");
    expect(files.length).toBeGreaterThan(10);
    expect(files.filter((f) => classifyTwoSegmentPath(f) !== "pass")).toEqual(
      [],
    );
  });

  it("dotted paths and the proxy's own skip prefixes are never gated", () => {
    expect(classifyTwoSegmentPath("/.well-known/security.txt")).toBe("pass");
    expect(classifyTwoSegmentPath("/images/a.jpg")).toBe("pass");
  });
});
