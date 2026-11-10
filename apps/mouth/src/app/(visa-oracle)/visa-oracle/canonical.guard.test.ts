import {
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
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

// Every page route under (visa-oracle) is found on disk, so a route added
// later is covered without touching this test. The parent layout names
// /visa-oracle, so a child that does not declare its own canonical would
// silently inherit it and tell crawlers it is a duplicate of the Oracle page.
const GROUP_ROOT = join(process.cwd(), "src/app/(visa-oracle)");

function pageRoutes(dir: string, route: string): string[] {
  const routes: string[] = [];
  if (existsSync(join(dir, "page.tsx"))) routes.push(route);
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    // private folders (_lib, _components) and route groups never add a URL
    // segment of their own that holds a page of its own to guard
    if (!statSync(full).isDirectory() || entry.startsWith("_")) continue;
    routes.push(...pageRoutes(full, `${route}/${entry}`));
  }
  return routes;
}

function ownCanonical(routeDir: string): string | undefined {
  for (const file of ["layout.tsx", "page.tsx"]) {
    const path = join(routeDir, file);
    if (!existsSync(path)) continue;
    const match = readFileSync(path, "utf8").match(
      /canonical:\s*"([^"]+)"/,
    );
    if (match) return match[1];
  }
  return undefined;
}

describe("every (visa-oracle) page route declares its own self canonical", () => {
  const routes = pageRoutes(GROUP_ROOT, "");

  it("finds the routes that exist today (non-vacuity)", () => {
    expect(routes).toEqual(
      expect.arrayContaining([
        "/visa-oracle",
        "/visa-oracle/privacy",
        "/visa-oracle/unlock",
      ]),
    );
  });

  it.each(routes)("%s resolves a canonical equal to its own URL", (route) => {
    expect(ownCanonical(join(GROUP_ROOT, route))).toBe(
      `https://balizero.com${route}`,
    );
  });

  it("guilt control: a route directory without a canonical of its own is caught", () => {
    const dir = mkdtempSync(join(tmpdir(), "vo-canonical-"));
    try {
      writeFileSync(join(dir, "page.tsx"), "export default function P() { return null; }");
      writeFileSync(
        join(dir, "layout.tsx"),
        "export const metadata = { robots: { index: false, follow: false } };",
      );
      expect(ownCanonical(dir)).toBeUndefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
