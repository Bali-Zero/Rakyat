import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
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
// /visa-oracle, so a child whose EFFECTIVE canonical is not its own URL would
// silently tell crawlers it duplicates the Oracle page.
//
// This mirrors Next's metadata resolution rather than calling it (the
// resolver is internal and needs a built route tree):
//   - the URL drops (group) segments and never includes _private folders;
//     @slot and intercepting (.) folders are not handled and FAIL loudly;
//   - metadata merges per top-level key and the NEAREST definition wins: the
//     page's own `alternates` beats its layout's, a nearer layout beats a
//     farther one, so only the nearest file that declares `alternates`
//     decides the canonical (a declaration without a canonical leaves none);
//   - a canonical that is not a string literal cannot be verified here and
//     fails rather than passing by omission.
const APP_ROOT = join(process.cwd(), "src/app");
const GROUP_START = ["(visa-oracle)"];

type Resolution = string | "NONE" | "UNVERIFIABLE";

function routeUrl(segments: string[]): string {
  const parts = segments.filter((s) => !/^\([^.][^)]*\)$/.test(s));
  return parts.length === 0 ? "/" : `/${parts.join("/")}`;
}

function pageRoutes(appRoot: string, segments: string[]): string[][] {
  const dir = join(appRoot, ...segments);
  const routes: string[][] = [];
  if (existsSync(join(dir, "page.tsx"))) routes.push(segments);
  for (const entry of readdirSync(dir)) {
    if (!statSync(join(dir, entry)).isDirectory() || entry.startsWith("_")) {
      continue;
    }
    if (entry.startsWith("@") || /^\(\.+\)/.test(entry)) {
      throw new Error(
        `Unsupported route folder "${entry}" under ${dir}: extend the guard before adding slots or intercepting routes`,
      );
    }
    routes.push(...pageRoutes(appRoot, [...segments, entry]));
  }
  return routes;
}

function declaredCanonical(source: string): Resolution | undefined {
  if (!/\balternates\s*:/.test(source)) return undefined;
  const block = source.match(/\balternates\s*:\s*\{([^}]*)\}/);
  if (!block) return "UNVERIFIABLE";
  if (!/\bcanonical\s*:/.test(block[1])) return "NONE";
  const literal = block[1].match(/\bcanonical\s*:\s*"([^"]+)"/);
  return literal ? literal[1] : "UNVERIFIABLE";
}

function effectiveCanonical(appRoot: string, segments: string[]): Resolution {
  for (let depth = segments.length; depth >= 0; depth--) {
    const dir = join(appRoot, ...segments.slice(0, depth));
    const files =
      depth === segments.length ? ["page.tsx", "layout.tsx"] : ["layout.tsx"];
    for (const file of files) {
      const path = join(dir, file);
      if (!existsSync(path)) continue;
      const found = declaredCanonical(readFileSync(path, "utf8"));
      if (found !== undefined) return found;
    }
  }
  return "NONE";
}

function violations(appRoot: string, start: string[]): string[] {
  return pageRoutes(appRoot, start).flatMap((segments) => {
    const url = routeUrl(segments);
    const got = effectiveCanonical(appRoot, segments);
    return got === `https://balizero.com${url}`
      ? []
      : [`${url}: effective canonical is ${got}`];
  });
}

function tree(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), "vo-canonical-"));
  for (const [rel, body] of Object.entries(files)) {
    const full = join(root, rel);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, body);
  }
  return root;
}

const meta = (canonical?: string) =>
  `export const metadata = { alternates: { ${canonical ? `canonical: "${canonical}"` : ""} } };`;
const PAGE = "export default function P() { return null; }";

describe("every (visa-oracle) page route resolves its own self canonical", () => {
  const routes = pageRoutes(APP_ROOT, GROUP_START);

  it("finds the routes that exist today (non-vacuity)", () => {
    expect(routes.map(routeUrl)).toEqual(
      expect.arrayContaining([
        "/visa-oracle",
        "/visa-oracle/privacy",
        "/visa-oracle/unlock",
      ]),
    );
  });

  it.each(routes.map((r) => [routeUrl(r), r] as const))(
    "%s: the effective canonical is its own URL",
    (url, segments) => {
      expect(effectiveCanonical(APP_ROOT, [...segments])).toBe(
        `https://balizero.com${url}`,
      );
    },
  );
});

describe("the guard itself (temp route trees)", () => {
  const make = (files: Record<string, string>) => {
    const root = tree(files);
    return { root, done: () => rmSync(root, { recursive: true, force: true }) };
  };

  it("(a) a route inside a (group) is mapped without the group and passes when correct", () => {
    const { root, done } = make({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta("https://balizero.com/a"),
    });
    try {
      expect(violations(root, ["(g)"])).toEqual([]);
    } finally {
      done();
    }
  });

  it("(a) the same route with a wrong canonical fails", () => {
    const { root, done } = make({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta("https://balizero.com/(g)/a"),
    });
    try {
      expect(violations(root, ["(g)"])).toHaveLength(1);
    } finally {
      done();
    }
  });

  it("(b) a correct layout with a wrong page-level override fails", () => {
    const { root, done } = make({
      "(g)/a/page.tsx": `${PAGE}\n${meta("https://balizero.com/other")}`,
      "(g)/a/layout.tsx": meta("https://balizero.com/a"),
    });
    try {
      expect(violations(root, ["(g)"])).toHaveLength(1);
    } finally {
      done();
    }
  });

  it("(b) a correct page-level override over a wrong layout passes", () => {
    const { root, done } = make({
      "(g)/a/page.tsx": `${PAGE}\n${meta("https://balizero.com/a")}`,
      "(g)/a/layout.tsx": meta("https://balizero.com/other"),
    });
    try {
      expect(violations(root, ["(g)"])).toEqual([]);
    } finally {
      done();
    }
  });

  it("a nearer layout wins over a farther one, and a missing canonical is caught", () => {
    const { root, done } = make({
      "(g)/layout.tsx": meta("https://balizero.com/parent"),
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta("https://balizero.com/a"),
      "(g)/b/page.tsx": PAGE,
    });
    try {
      expect(violations(root, ["(g)"])).toEqual([
        "/b: effective canonical is https://balizero.com/parent",
      ]);
    } finally {
      done();
    }
  });

  it("an alternates block without a canonical leaves none, and a dynamic one cannot be verified", () => {
    const { root, done } = make({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta(),
      "(g)/b/page.tsx": PAGE,
      "(g)/b/layout.tsx":
        "export const metadata = { alternates: { canonical: someUrl } };",
    });
    try {
      expect(violations(root, ["(g)"])).toEqual([
        "/a: effective canonical is NONE",
        "/b: effective canonical is UNVERIFIABLE",
      ]);
    } finally {
      done();
    }
  });

  it("private folders are skipped, and slots or intercepting folders fail loudly", () => {
    const ok = make({ "(g)/_lib/page.tsx": PAGE });
    const bad = make({ "(g)/@modal/page.tsx": PAGE });
    const bad2 = make({ "(g)/(.)photo/page.tsx": PAGE });
    try {
      expect(violations(ok.root, ["(g)"])).toEqual([]);
      expect(() => violations(bad.root, ["(g)"])).toThrow(/Unsupported/);
      expect(() => violations(bad2.root, ["(g)"])).toThrow(/Unsupported/);
    } finally {
      ok.done();
      bad.done();
      bad2.done();
    }
  });
});
