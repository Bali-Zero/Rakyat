import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

type Redirect = {
  source: string;
  destination: string;
  permanent?: boolean;
  statusCode?: number;
};

// The table is the only thing that keeps old inbound links (shared URLs,
// search results, emails) from becoming a 404 when a page is renamed or
// removed. This file puts the table itself under test: its size, its shape,
// and the absence of a source that two entries claim.
const EXPECTED_ENTRY_COUNT = 115;

// Two-hop paths that already exist in the table. They are not loops (each ends
// on a real page after two permanent redirects) and are recorded here so a NEW
// chain fails this test while these two do not.
const KNOWN_CHAINS = [
  "/immigration/e33f-spouse-dependent-kitas-guide",
  "/immigration/e33e-child-dependent-kitas-guide",
];

// The one entry that is temporary on purpose: its target is a versioned file.
const TEMPORARY_SOURCE = "/api/prime/zones-geojson";

async function table(): Promise<Redirect[]> {
  expect(nextConfig.redirects).toBeDefined();
  return (await nextConfig.redirects!()) as Redirect[];
}

// Compile a Next.js path pattern (`/a/:slug*`, `/a/:id`) into a RegExp that
// answers "would a request for this concrete path be caught by this source".
function sourceMatcher(source: string): RegExp {
  const parts = source.split(/(:\w+\*|:\w+)/);
  const body = parts
    .map((part) => {
      if (!part.startsWith(":")) {
        return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      }
      return part.endsWith("*") ? ".*" : "[^/]+";
    })
    .join("");
  return new RegExp(`^${body}$`);
}

function entryFor(redirects: Redirect[], path: string): Redirect | undefined {
  return redirects.find((r) => sourceMatcher(r.source).test(path));
}

describe("next.config.ts redirects table", () => {
  it("has the expected number of entries", async () => {
    expect((await table()).length).toBe(EXPECTED_ENTRY_COUNT);
  });

  it("is well formed: rooted paths, permanent, not redirecting to itself", async () => {
    for (const r of await table()) {
      expect(r.source.startsWith("/"), r.source).toBe(true);
      expect(
        r.destination.startsWith("/"),
        `${r.source} -> ${r.destination}`,
      ).toBe(true);
      expect(r.permanent, r.source).toBe(r.source !== TEMPORARY_SOURCE);
      expect(r.source, "self redirect").not.toBe(r.destination);
    }
  });

  it("claims every source once (no duplicate source)", async () => {
    const sources = (await table()).map((r) => r.source);
    const dupes = sources.filter((s, i) => sources.indexOf(s) !== i);
    expect(dupes).toEqual([]);
  });

  it("has no loop, and no chain beyond the two already known", async () => {
    const redirects = await table();
    const chains: string[] = [];
    for (const r of redirects) {
      const seen = new Set([r.source]);
      let next = entryFor(redirects, r.destination);
      let hops = 1;
      while (next) {
        expect(
          seen.has(next.source),
          `loop through ${r.source} -> ${r.destination}`,
        ).toBe(false);
        seen.add(next.source);
        hops += 1;
        next = entryFor(redirects, next.destination);
      }
      if (hops > 1) chains.push(r.source);
    }
    expect(chains.sort()).toEqual([...KNOWN_CHAINS].sort());
  });
});

describe("retired /v2 generation of the homepage and news index", () => {
  it("sends /v2 and /v2/news permanently to the pages that replaced them", async () => {
    const redirects = await table();
    expect(redirects).toContainEqual({
      source: "/v2",
      destination: "/",
      permanent: true,
    });
    expect(redirects).toContainEqual({
      source: "/v2/news",
      destination: "/news",
      permanent: true,
    });
  });

  it("each of the two sources appears once and lands outside the table", async () => {
    const redirects = await table();
    for (const source of ["/v2", "/v2/news"]) {
      expect(redirects.filter((r) => r.source === source)).toHaveLength(1);
      const entry = redirects.find((r) => r.source === source)!;
      // No chain: the destination is a real page, not another redirect.
      expect(entryFor(redirects, entry.destination), source).toBeUndefined();
    }
  });

  it("keeps the earlier /v2/* redirects, and does not claim children that never existed", async () => {
    const redirects = await table();
    for (const source of [
      "/v2/privacy",
      "/v2/terms",
      "/v2/cookies",
      "/v2/company/about",
      "/v2/company/careers",
      "/v2/company/press",
    ]) {
      expect(
        redirects.some((r) => r.source === source),
        source,
      ).toBe(true);
    }
    // /v2/news/<slug> was a 404 before this change and stays one: no wildcard.
    expect(entryFor(redirects, "/v2/news/some-slug")).toBeUndefined();
    expect(entryFor(redirects, "/v2/other")).toBeUndefined();
  });
});
