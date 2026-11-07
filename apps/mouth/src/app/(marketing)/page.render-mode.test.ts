import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The homepage must stay statically regenerable. `force-dynamic` sent
 * `Cache-Control: private, no-cache, no-store` on every visit (measured on
 * balizero.com 2026-11-01) for data that lib/blog/articles.ts already serves
 * from a 60 s cache. The page revalidation window must not be shorter than
 * that data window, or it would promise a freshness the data cannot give.
 */
const PAGE = readFileSync(join(__dirname, "page.tsx"), "utf-8");
const ARTICLES = readFileSync(
  join(__dirname, "..", "..", "lib", "blog", "articles.ts"),
  "utf-8",
);

describe("homepage render mode", () => {
  it("does not force dynamic rendering", () => {
    expect(PAGE).not.toMatch(
      /export const dynamic\s*=\s*["']force-dynamic["']/,
    );
  });

  it("revalidates at least as often as the articles cache", () => {
    const page = PAGE.match(/export const revalidate\s*=\s*(\d+)/);
    expect(page, "page.tsx exports a numeric revalidate").not.toBeNull();
    const data = ARTICLES.match(/unstable_cache\([\s\S]*?revalidate:\s*(\d+)/);
    expect(data, "articles.ts caches with a numeric revalidate").not.toBeNull();
    expect(Number(page![1])).toBeGreaterThanOrEqual(Number(data![1]));
  });
});
