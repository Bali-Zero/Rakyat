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
import { dirname, join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";

// Every page route under (visa-oracle) is found on disk, so a route added
// later is covered without touching this test. A route passes only when the
// canonical Next would EFFECTIVELY resolve for it is its own URL; the parent
// layout names /visa-oracle, so a child that fails this would tell crawlers
// it duplicates the Oracle page.
//
// Nothing here parses metadata out of source text. Each module in a route's
// chain is imported and its EVALUATED `metadata` export is read, so
// concatenations, constants and URL objects are seen as Next sees them.
// Next's own resolver is internal and needs a built route tree, so its rules
// are mirrored (and said so):
//   - URL from path: (group) segments dropped, _private folders skipped,
//     @slot and intercepting (.) folders FAIL loudly (not handled);
//   - metadata merges per top-level key and the NEAREST module whose
//     `metadata.alternates` is defined decides, page before its layout,
//     nearer layout before farther; an alternates object without a canonical
//     leaves none (Next drops the inherited one);
//   - client components cannot export metadata, so a "use client" module is
//     not imported; a module exporting generateMetadata fails ("add an
//     explicit test"), it is never called; an import that throws fails
//     loudly, naming the file.
const APP_ROOT = join(process.cwd(), "src/app");
const GROUP = "(visa-oracle)";
const BASE = "https://balizero.com";
const SCRATCH = join(process.cwd(), ".vitest-canonical-guard");

type Resolution = string | "NONE";

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

function isClientModule(path: string): boolean {
  const head = readFileSync(path, "utf8").replace(
    /^(\s|\/\/[^\n]*\n|\/\*[\s\S]*?\*\/)*/,
    "",
  );
  return /^["']use client["']/.test(head);
}

async function evaluatedMetadata(
  path: string,
): Promise<Record<string, unknown> | undefined> {
  if (isClientModule(path)) return undefined;
  let mod: Record<string, unknown>;
  try {
    mod = (await import(/* @vite-ignore */ path)) as Record<string, unknown>;
  } catch (error) {
    throw new Error(`Could not import ${path} to read its metadata: ${error}`);
  }
  if (typeof mod.generateMetadata === "function") {
    throw new Error(
      `${path} exports generateMetadata (dynamic metadata): add an explicit test`,
    );
  }
  const metadata = mod.metadata;
  return metadata && typeof metadata === "object"
    ? (metadata as Record<string, unknown>)
    : undefined;
}

function normalise(canonical: unknown, where: string): string {
  if (typeof canonical === "string" || canonical instanceof URL) {
    return new URL(String(canonical), BASE).href;
  }
  throw new Error(`Unsupported canonical value in ${where}`);
}

async function effectiveCanonical(
  appRoot: string,
  segments: string[],
): Promise<Resolution> {
  // page first, then each layout from the route up to the group root
  const chain = [join(appRoot, ...segments, "page.tsx")];
  const groupDepth = segments.indexOf(GROUP) + 1 || 1;
  for (let depth = segments.length; depth >= groupDepth; depth--) {
    chain.push(join(appRoot, ...segments.slice(0, depth), "layout.tsx"));
  }
  for (const path of chain) {
    if (!existsSync(path)) continue;
    const metadata = await evaluatedMetadata(path);
    const alternates = metadata?.alternates as
      { canonical?: unknown } | undefined;
    if (alternates === undefined || alternates === null) continue;
    return alternates.canonical == null
      ? "NONE"
      : normalise(alternates.canonical, path);
  }
  return "NONE";
}

async function violations(appRoot: string, start: string[]): Promise<string[]> {
  const out: string[] = [];
  for (const segments of pageRoutes(appRoot, start)) {
    const url = routeUrl(segments);
    const got = await effectiveCanonical(appRoot, segments);
    if (got !== new URL(url, BASE).href) {
      out.push(`${url}: effective canonical is ${got}`);
    }
  }
  return out;
}

describe("every (visa-oracle) page route resolves its own self canonical", () => {
  const routes = pageRoutes(APP_ROOT, [GROUP]);

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
    async (url, segments) => {
      expect(await effectiveCanonical(APP_ROOT, [...segments])).toBe(
        new URL(url, BASE).href,
      );
    },
  );

  it("keeps the titles of the layouts it guards (positive control)", async () => {
    const title = async (rel: string) =>
      (await evaluatedMetadata(join(APP_ROOT, GROUP, "visa-oracle", rel)))
        ?.title;
    expect(await title("layout.tsx")).toBe("Visa Oracle");
    expect(await title("privacy/layout.tsx")).toBe(
      "Visa Oracle Privacy Policy",
    );
  });
});

// The guard itself, on throwaway trees of REAL importable modules.
describe("the guard resolves evaluated metadata, not source text", () => {
  const roots: string[] = [];
  afterAll(() => {
    for (const root of roots) rmSync(root, { recursive: true, force: true });
    rmSync(SCRATCH, { recursive: true, force: true });
  });

  function tree(files: Record<string, string>): string {
    // inside the project root: vite only transforms (tsx) modules it can serve
    mkdirSync(SCRATCH, { recursive: true });
    const root = mkdtempSync(join(SCRATCH, "tree-"));
    roots.push(root);
    for (const [rel, body] of Object.entries(files)) {
      const full = join(root, rel);
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, body);
    }
    return root;
  }

  const meta = (canonical?: string, extra = "") =>
    `export const metadata = { ${extra} alternates: { ${canonical ?? ""} } };`;
  const PAGE = "export default function P() { return null; }\n";

  it("a concatenated canonical is evaluated, so its real value is judged", async () => {
    const root = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta(
        'canonical: "https://balizero.com/a" + "/wrong"',
      ),
    });
    expect(await violations(root, ["(g)"])).toEqual([
      "/a: effective canonical is https://balizero.com/a/wrong",
    ]);
  });

  it("a constant and a URL object resolve to their values", async () => {
    const root = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": `const SELF = "https://balizero.com/a";\n${meta("canonical: SELF")}`,
      "(g)/b/page.tsx": PAGE,
      "(g)/b/layout.tsx": meta(
        'canonical: new URL("/b", "https://balizero.com")',
      ),
    });
    expect(await violations(root, ["(g)"])).toEqual([]);
  });

  it("(a) a route inside a (group) is mapped without the group: correct passes, wrong fails", async () => {
    const good = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta('canonical: "https://balizero.com/a"'),
    });
    const bad = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta('canonical: "https://balizero.com/(g)/a"'),
    });
    expect(await violations(good, ["(g)"])).toEqual([]);
    expect(await violations(bad, ["(g)"])).toHaveLength(1);
  });

  it("(b) a correct layout with a wrong page-level override fails; the reverse passes", async () => {
    const bad = tree({
      "(g)/a/page.tsx": `${PAGE}${meta('canonical: "https://balizero.com/other"')}`,
      "(g)/a/layout.tsx": meta('canonical: "https://balizero.com/a"'),
    });
    const good = tree({
      "(g)/a/page.tsx": `${PAGE}${meta('canonical: "https://balizero.com/a"')}`,
      "(g)/a/layout.tsx": meta('canonical: "https://balizero.com/other"'),
    });
    expect(await violations(bad, ["(g)"])).toHaveLength(1);
    expect(await violations(good, ["(g)"])).toEqual([]);
  });

  it("an alternates object without a canonical leaves none, even over a correct parent", async () => {
    const root = tree({
      "(g)/layout.tsx": meta('canonical: "https://balizero.com/a"'),
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": meta(undefined, ""),
    });
    expect(await violations(root, ["(g)"])).toEqual([
      "/a: effective canonical is NONE",
    ]);
  });

  it("a route with no canonical anywhere in its chain is caught", async () => {
    const root = tree({ "(g)/a/page.tsx": PAGE });
    expect(await violations(root, ["(g)"])).toEqual([
      "/a: effective canonical is NONE",
    ]);
  });

  it("a client module is not imported, dynamic metadata and import errors fail loudly", async () => {
    const client = tree({
      "(g)/a/page.tsx": `"use client";\n${PAGE}`,
      "(g)/a/layout.tsx": meta('canonical: "https://balizero.com/a"'),
    });
    const dynamic = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx":
        "export async function generateMetadata() { return {}; }",
    });
    const broken = tree({
      "(g)/a/page.tsx": PAGE,
      "(g)/a/layout.tsx": "throw new Error('boom');",
    });
    expect(await violations(client, ["(g)"])).toEqual([]);
    await expect(violations(dynamic, ["(g)"])).rejects.toThrow(
      /generateMetadata/,
    );
    await expect(violations(broken, ["(g)"])).rejects.toThrow(
      /Could not import/,
    );
  });

  it("private folders are skipped, and slots or intercepting folders fail loudly", async () => {
    expect(
      await violations(tree({ "(g)/_lib/page.tsx": PAGE }), ["(g)"]),
    ).toEqual([]);
    await expect(
      violations(tree({ "(g)/@modal/page.tsx": PAGE }), ["(g)"]),
    ).rejects.toThrow(/Unsupported/);
    await expect(
      violations(tree({ "(g)/(.)photo/page.tsx": PAGE }), ["(g)"]),
    ).rejects.toThrow(/Unsupported/);
  });
});
