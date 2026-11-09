import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The questions and the key must never reach the public bundle. The browser
 * ships every module reachable from a "use client" file, so the rule is about
 * the import GRAPH, not about one directory: no client file may reach a file
 * under `_server/`, or any file that imports "server-only". Both halves have a
 * planted violation so a green run proves the detector is awake.
 */
const ROOT = join(__dirname, "..");
const SRC = resolve(ROOT, "../../..");
const SECRET_STRINGS = [
  "Bu Lestari",
  "Mas Galih",
  "INV-108",
  "21.800.000",
  "BPPU-FIKTIF",
  "Client Reply Library",
  "Tenang dan mengakui",
  "Satu Hari di Meja Pajak",
];

type Graph = Record<string, string>;

function resolveImport(from: string, spec: string): string | null {
  const base = spec.startsWith("@/")
    ? join(SRC, spec.slice(2))
    : spec.startsWith(".")
      ? resolve(dirname(from), spec)
      : null;
  return base;
}

export function clientReach(graph: Graph): {
  reached: string[];
  violations: string[];
} {
  const known = (p: string) =>
    [p, `${p}.ts`, `${p}.tsx`, `${p}/index.ts`].find((c) => c in graph);
  const reached = new Set<string>();
  const stack = Object.keys(graph).filter((f) =>
    /^\s*(\/\*[\s\S]*?\*\/\s*)?["']use client["']/.test(graph[f]),
  );
  while (stack.length) {
    const f = stack.pop()!;
    if (reached.has(f)) continue;
    reached.add(f);
    for (const m of graph[f].matchAll(
      /(?:import|export)\s[^;]*?from\s+["']([^"']+)["']|import\s+["']([^"']+)["']/g,
    )) {
      const target = resolveImport(f, m[1] ?? m[2]);
      const file = target && known(target);
      if (file) stack.push(file);
    }
  }
  const violations = [...reached].filter(
    (f) =>
      f.includes("/_server/") || /import\s+["']server-only["']/.test(graph[f]),
  );
  return { reached: [...reached], violations };
}

function load(dir: string, out: Graph = {}): Graph {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) {
      if (e !== "__tests__") load(p, out);
    } else if (/\.(ts|tsx)$/.test(e)) out[p] = readFileSync(p, "utf8");
  }
  return out;
}

describe("tax_interview: nothing secret in the client graph", () => {
  const graph = load(ROOT);

  it("no client component reaches a _server module or a server-only module", () => {
    const { reached, violations } = clientReach(graph);
    expect(reached.length).toBeGreaterThan(3);
    expect(reached.some((f) => f.endsWith("Interview.tsx"))).toBe(true);
    expect(violations).toEqual([]);
  });

  it("every _server module declares server-only", () => {
    const server = Object.keys(graph).filter((f) => f.includes("/_server/"));
    expect(server.length).toBeGreaterThanOrEqual(5);
    for (const f of server)
      expect(graph[f], f).toMatch(/^import "server-only";/m);
  });

  it("no client-reachable source contains a question, a key or a rubric string", () => {
    const { reached } = clientReach(graph);
    for (const f of reached)
      for (const s of SECRET_STRINGS)
        expect(graph[f], `${s} in ${f}`).not.toContain(s);
  });

  it("the page never hands content to the client without the cookie check", () => {
    const page = graph[join(ROOT, "page.tsx")];
    expect(page).toContain("verifyToken");
    expect(page.indexOf("verifyToken")).toBeLessThan(
      page.indexOf("<Interview"),
    );
    expect(page).not.toMatch(/generateStaticParams|revalidate\s*=/);
    expect(page).toContain('dynamic = "force-dynamic"');
  });

  it("guilt control: the detector flags a client file that imports a key, directly or through a helper", () => {
    const planted: Graph = {
      "/s/_components/Bad.tsx":
        '"use client";\nimport { KEY } from "../_server/keys";\n',
      "/s/_server/keys.ts": 'import "server-only";\nexport const KEY = 1;\n',
      "/s/_components/Via.tsx":
        '"use client";\nimport { x } from "../helper";\n',
      "/s/helper.ts":
        'import { KEY } from "./_server/keys";\nexport const x = KEY;\n',
    };
    const v = clientReach(planted).violations;
    expect(v).toContain("/s/_server/keys.ts");
    const innocent: Graph = {
      "/s/Ok.tsx": '"use client";\nimport { t } from "./types";\n',
      "/s/types.ts": "export const t = 1;\n",
      "/s/_server/keys.ts": 'import "server-only";\n',
    };
    expect(clientReach(innocent).violations).toEqual([]);
  });
});
