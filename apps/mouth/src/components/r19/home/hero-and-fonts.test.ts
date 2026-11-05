import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const MOUTH = join(__dirname, "..", "..", "..", "..");
const ENTRY = readFileSync(join(__dirname, "Entry.tsx"), "utf-8");
const HOME_CSS = readFileSync(join(__dirname, "home.css"), "utf-8");
const PAGE = readFileSync(
  join(MOUTH, "src", "app", "(marketing)", "page.tsx"),
  "utf-8",
);

describe("hero image (LCP) ships responsive candidates", () => {
  const hero = ENTRY.match(/<img[^>]*fetchPriority="high"[^>]*>/s);

  it("has a srcSet whose every candidate exists on disk", () => {
    expect(hero).not.toBeNull();
    const srcSet = hero![0].match(/srcSet="([^"]+)"/);
    expect(srcSet).not.toBeNull();
    const candidates = srcSet![1].split(",").map((c) => c.trim().split(/\s+/));
    expect(candidates.length).toBeGreaterThanOrEqual(3);
    for (const [path, descriptor] of candidates) {
      expect(existsSync(join(MOUTH, "public", path)), path).toBe(true);
      expect(descriptor).toMatch(/^\d+w$/);
    }
  });

  it("declares intrinsic size and a sizes hint", () => {
    expect(hero![0]).toMatch(/width=\{\d+\}/);
    expect(hero![0]).toMatch(/height=\{\d+\}/);
    expect(hero![0]).toMatch(/sizes="/);
  });
});

describe("R19 home fonts are preloaded, and only the ones home.css declares", () => {
  const declared = [
    ...HOME_CSS.matchAll(/@font-face\{[^}]*url\("([^"]+)"\)/g),
  ].map((m) => m[1]);
  const preloaded = [...PAGE.matchAll(/"(\/fonts\/[^"]+\.woff2)"/g)].map(
    (m) => m[1],
  );

  it("preloads exactly the declared faces", () => {
    expect(declared.length).toBeGreaterThan(0);
    expect([...preloaded].sort()).toEqual([...declared].sort());
  });

  it("every preloaded face exists on disk", () => {
    for (const href of preloaded) {
      expect(existsSync(join(MOUTH, "public", href)), href).toBe(true);
    }
  });
});
