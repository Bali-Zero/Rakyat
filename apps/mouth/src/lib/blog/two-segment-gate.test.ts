import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { generateStaticParams } from "@/app/(blog)/[category]/page";
import { CATEGORY_SEO } from "@/lib/blog/category-seo";
import {
  INTERNAL_ROUTES,
  STATUS_GATE_404_PATH,
  canonicalPathname,
} from "@/proxy";
import {
  PUBLIC_TWO_SEGMENT_ROUTES,
  classifyTwoSegmentPath,
} from "./two-segment-gate";

const APP_DIR = path.join(process.cwd(), "src/app");

const ROUTE_FILE = /^(page|route)\.(tsx?|jsx?|mjs|cjs|mdx)$/;

/**
 * URL routes under a route tree. Route groups add no segment, private folders
 * (`_x`) are not routes, a parallel-route slot (`@panel`) adds no segment but
 * its pages are served at the parent's URL (`x/@slot/y/page.tsx` serves
 * `/x/y`), and an intercepting folder (`(.)x`, `(..)x`, `(...)x`) only
 * intercepts a soft navigation: a hard request for that URL reaches the real
 * route, so it is not a URL of its own. kbli/@panel is a live slot.
 */
function appRoutes(dir: string, segments: string[] = []): string[] {
  const out: string[] = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  if (entries.some((e) => e.isFile() && ROUTE_FILE.test(e.name))) {
    out.push(segments.join("/"));
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    if (e.name.startsWith("_")) continue;
    if (/^\(\.{1,3}\)/.test(e.name)) continue;
    const isGroup =
      (e.name.startsWith("(") && e.name.endsWith(")")) ||
      e.name.startsWith("@");
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
function publicTwoSegmentRoutes(
  root: string = APP_DIR,
): Record<string, string[] | "*"> {
  const redirected = new Set([
    ...INTERNAL_ROUTES.map((r) => r.slice(1)),
    "portal",
    "chat",
    "api",
  ]);
  const out: Record<string, string[] | "*"> = {};
  for (const route of appRoutes(root)) {
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

describe("appRoutes derivation on a synthetic tree", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "gate-tree-"));
  afterAll(() => fs.rmSync(root, { recursive: true, force: true }));
  const page = (rel: string) => {
    const file = path.join(root, rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, "export default null;\n");
  };
  page("x/@slot/y/page.tsx");
  page("g/(grp)/h/page.tsx");
  page("w/(.)ghost/page.tsx");
  page("w/(..)(..)ghost2/page.tsx");
  page("p/_private/page.tsx");
  page("d/[id]/page.tsx");

  it("[guilt] a page under a parallel slot serves the parent URL", () => {
    expect(appRoutes(root)).toContain("x/y");
    expect(publicTwoSegmentRoutes(root)["x"]).toEqual(["y"]);
  });

  it("[innocence] groups, intercepts and private folders add no route", () => {
    const routes = appRoutes(root);
    expect(routes).toContain("g/h");
    expect(routes).toContain("d/[id]");
    expect(routes.filter((r) => /ghost|_private/.test(r))).toEqual([]);
    expect(publicTwoSegmentRoutes(root)["d"]).toBe("*");
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

  it("an encoded dot in the first segment is not a file request: not-found", () => {
    const canonical = canonicalPathname("/unknown%2Ecategory/slug");
    expect(canonical).toBe("/unknown.category/slug");
    expect(classifyTwoSegmentPath(canonical)).toBe("not-found");
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
/**
 * Limits of a text check, by design: a spread from an imported object
 * (`...baseConfig`) cannot be seen, because the declaration lives in another
 * file. A `rewrites` inside a string or after a `;` on a comment line can
 * false-positive; that fails red, the safe direction, and is left as is.
 *
 * True when the config text declares `rewrites` in any form: method
 * (`async rewrites() {`), property (`rewrites: async () =>`, `"rewrites":`),
 * shorthand (`{ rewrites }`) assignment (`config.rewrites = ...`) or bracket assignment
 * (`config["rewrites"] = ...`). Comments
 * are stripped first; the name must stand alone, so `noRewritesHere` is not it.
 */
function declaresRewrites(source: string): boolean {
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/.*$/gm, "$1");
  return /(?<![\w$])['"`]?rewrites['"`]?\]?\s*[(:,}=]/.test(code);
}

describe("declaresRewrites", () => {
  it.each([
    ["method", "const c = { async rewrites() { return []; } };"],
    ["bare method", "const c = {\n  rewrites() { return []; },\n};"],
    ["property async arrow", "const c = { rewrites: async () => [] };"],
    ["property arrow", "const c = { rewrites: () => [] };"],
    ["property function", "const c = { rewrites: async function () {} };"],
    ["quoted key", `const c = { "rewrites": async () => [] };`],
    ["single-quoted key", "const c = { 'rewrites': () => [] };"],
    [
      "shorthand",
      "const rewrites = async () => [];\nexport default { rewrites };",
    ],
    ["assignment", "nextConfig.rewrites = async () => [];"],
    ["bracket assignment", `nextConfig["rewrites"] = async () => [];`],
    ["single-quoted bracket assignment", "config['rewrites'] = () => [];"],
    ["after a comment line", "// note\nconst c = { rewrites() {} };"],
  ])("[guilt] flags %s", (_name, src) => {
    expect(declaresRewrites(src)).toBe(true);
  });

  it.each([
    ["line comment", "// Do NOT add rewrites for /api/* here\nconst c = {};"],
    ["trailing comment", "const c = {}; // rewrites: would conflict"],
    [
      "block comment",
      "/* rewrites() are shadowed by the proxy */ const c = {};",
    ],
    [
      "unrelated identifier",
      "const noRewritesHere = true; const rewritesCount = 1;",
    ],
    ["prose in a string", `const s = "middleware rewrites)";`],
  ])("[innocence] ignores %s", (_name, src) => {
    expect(declaresRewrites(src)).toBe(false);
  });
});

describe("the gate cannot 404 anything that is not a page route", () => {
  const MOUTH = process.cwd();

  it("no rewrite exists to be shadowed (next.config.ts and vercel.json)", () => {
    const cfg = fs.readFileSync(path.join(MOUTH, "next.config.ts"), "utf8");
    expect(declaresRewrites(cfg)).toBe(false);
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

describe("the 404 sentinel is reserved", () => {
  const name = STATUS_GATE_404_PATH.replace(/^\//, "");

  it("is a one-segment path", () => {
    expect(name).toMatch(/^[a-z0-9-]+$/);
  });

  it("is not an article category", () => {
    expect(Object.keys(CATEGORY_SEO)).not.toContain(name);
  });

  it("is not produced by (blog)/[category] generateStaticParams", () => {
    expect(generateStaticParams().map((p) => p.category)).not.toContain(name);
  });

  it("no route under src/app resolves a one-segment route with that name", () => {
    const oneSegment = appRoutes(APP_DIR).filter(
      (r) => r !== "" && !r.includes("/"),
    );
    expect(oneSegment).not.toContain(name);
    expect(oneSegment.filter((r) => r.startsWith("["))).toEqual(["[category]"]);
  });
});
