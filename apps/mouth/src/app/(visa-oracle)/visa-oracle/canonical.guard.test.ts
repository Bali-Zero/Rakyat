import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(
    join(process.cwd(), "src/app/(visa-oracle)/visa-oracle", rel),
    "utf8",
  );

describe("/visa-oracle canonical points at itself, not the root", () => {
  const layout = read("layout.tsx");

  it("declares alternates.canonical https://balizero.com/visa-oracle", () => {
    expect(layout).toMatch(
      /canonical:\s*"https:\/\/balizero\.com\/visa-oracle"/,
    );
  });

  it("does not point the canonical at the root domain", () => {
    expect(layout).not.toMatch(/canonical:\s*"https:\/\/balizero\.com\/?"/);
  });

  it("keeps its title (positive control)", () => {
    expect(layout).toMatch(/title:\s*"Visa Oracle"/);
  });
});

describe("/visa-oracle/privacy canonical points at itself, not the root", () => {
  const layout = read("privacy/layout.tsx");

  it("declares alternates.canonical https://balizero.com/visa-oracle/privacy", () => {
    expect(layout).toMatch(
      /canonical:\s*"https:\/\/balizero\.com\/visa-oracle\/privacy"/,
    );
  });

  it("does not point the canonical at the root domain", () => {
    expect(layout).not.toMatch(/canonical:\s*"https:\/\/balizero\.com\/?"/);
  });

  it("keeps its title (positive control)", () => {
    expect(layout).toMatch(/title:\s*"Visa Oracle Privacy Policy"/);
  });
});

describe("/visa-oracle/unlock canonical points at itself, not at /visa-oracle", () => {
  it("evaluates to its own canonical and keeps noindex/nofollow", async () => {
    const { metadata } = await import("./unlock/layout");
    expect(metadata.alternates?.canonical).toBe(
      "https://balizero.com/visa-oracle/unlock",
    );
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });

  it("the page is a client component, so the layout's metadata is the effective one", () => {
    // A client component cannot export metadata. If this page ever becomes a
    // server component it may declare its own metadata that wins over the
    // layout's: this test must then be revisited.
    expect(read("unlock/page.tsx")).toMatch(
      /^\s*(\/\/[^\n]*\n|\/\*[\s\S]*?\*\/\s*)*["']use client["']/,
    );
  });
});

// A route-set test, not a generic resolver: re-implementing Next's metadata
// resolution here could not be proved faithful, so every route gets its own
// explicit canonical test above and a new route fails THIS test until it does.
describe("the set of (visa-oracle) page routes is the set with an explicit canonical test", () => {
  const GROUP_ROOT = join(process.cwd(), "src/app/(visa-oracle)");

  function pageRoutes(dir: string, segments: string[]): string[] {
    const found: string[] = [];
    if (existsSync(join(dir, "page.tsx"))) {
      const parts = segments.filter((s) => !/^\(.+\)$/.test(s));
      found.push(`/${parts.join("/")}`);
    }
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (!statSync(full).isDirectory() || entry.startsWith("_")) continue;
      found.push(...pageRoutes(full, [...segments, entry]));
    }
    return found;
  }

  it("is exactly /visa-oracle, /visa-oracle/privacy, /visa-oracle/unlock", () => {
    expect(pageRoutes(GROUP_ROOT, []).sort()).toEqual(
      ["/visa-oracle", "/visa-oracle/privacy", "/visa-oracle/unlock"],
      // a new route needs its own explicit canonical test in this file
    );
  });
});
