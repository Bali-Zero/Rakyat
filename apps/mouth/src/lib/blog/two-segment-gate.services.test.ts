import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { SERVICES_DATA } from "@/data/services_data";

import {
  PUBLIC_TWO_SEGMENT_ROUTES,
  classifyTwoSegmentPath,
} from "./two-segment-gate";

/**
 * `/services/[slug]` renders from the static SERVICES_DATA table, and its
 * folder has a loading.tsx, so an unknown slug used to get an HTTP 200 head
 * before the page could call notFound(). The gate now knows the real slugs
 * (read from SERVICES_DATA itself) and 404s the rest before rendering.
 */
const realSlugs = Object.keys(SERVICES_DATA);

/**
 * Static pages that sit beside `services/[slug]` (`services/compliance`), read
 * from the file tree: the gate must never 404 a page that really exists.
 */
function staticServiceSiblings(dir = path.join(process.cwd(), "src/app")) {
  const out: string[] = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!e.isDirectory() || e.name.startsWith("_")) continue;
    const child = path.join(dir, e.name);
    if (e.name === "services") {
      for (const s of fs.readdirSync(child, { withFileTypes: true })) {
        if (!s.isDirectory() || /^[[(@_]/.test(s.name)) continue;
        if (fs.existsSync(path.join(child, s.name, "page.tsx"))) {
          out.push(s.name);
        }
      }
    } else if (e.name.startsWith("(")) {
      out.push(...staticServiceSiblings(child));
    }
  }
  return out;
}
const staticSiblings = staticServiceSiblings();

describe("two-segment gate — /services/<slug>", () => {
  it("reads the real service table (positive control)", () => {
    expect(realSlugs.length).toBeGreaterThanOrEqual(4);
    expect(realSlugs).toContain("visa");
    // The route-mirror table still says the second segment is dynamic.
    expect(PUBLIC_TWO_SEGMENT_ROUTES.services).toBe("*");
  });

  it.each(realSlugs)("/services/%s is a real service: pass", (slug) => {
    expect(classifyTwoSegmentPath(`/services/${slug}`)).toBe("pass");
    expect(classifyTwoSegmentPath(`/services/${slug}/`)).toBe("pass");
  });

  it("finds the static sibling pages in the tree (positive control)", () => {
    expect(staticSiblings).toContain("compliance");
  });

  it.each(staticSiblings)("/services/%s is a static page: pass", (name) => {
    expect(classifyTwoSegmentPath(`/services/${name}`)).toBe("pass");
  });

  it.each([
    "/services/random-slug-xyz",
    "/services/visas",
    "/services/constructor",
    "/services/toString",
  ])("%s is not a service: not-found", (p) => {
    expect(classifyTwoSegmentPath(p)).toBe("not-found");
  });

  it("leaves the services index and deeper paths to their own rules", () => {
    expect(classifyTwoSegmentPath("/services")).toBe("pass");
    expect(classifyTwoSegmentPath("/services/visa/extra")).toBe("pass");
    expect(classifyTwoSegmentPath("/services/logo.png")).toBe("pass");
  });
});
