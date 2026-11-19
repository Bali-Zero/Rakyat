import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";
import manifest from "../data/prime-zones.json";

// The Prime zones map is a versioned static file the CDN caches (the old route
// re-proxied 33 MB past Vercel's 10 MB cache limit). The manifest is what ties
// the page, the legacy address and the file together, so these tests take
// their obligations from the file on disk.

const ROOT = join(__dirname, "..", "..");
const file = join(ROOT, "public", manifest.path);

describe("prime zones manifest and its data file", () => {
  it("points at a file that exists under public/", () => {
    expect(manifest.path).toMatch(/^\/data\/prime\/zones-[0-9a-f]{16}\.json$/);
    expect(existsSync(file)).toBe(true);
  });

  it("names the file by the first 16 hex of its content hash", () => {
    const body = readFileSync(file);
    const digest = createHash("sha256").update(body).digest("hex");
    expect(manifest.sha256).toBe(digest);
    expect(manifest.path).toBe(`/data/prime/zones-${digest.slice(0, 16)}.json`);
    expect(manifest.bytes).toBe(body.length);
  });

  it("holds the feature count the manifest states", () => {
    const doc = JSON.parse(readFileSync(file, "utf8"));
    expect(doc.type).toBe("FeatureCollection");
    expect(doc.features).toHaveLength(manifest.features);
  });
});

describe("prime zones delivery", () => {
  it("keeps the old API address alive as a temporary redirect to the current file", async () => {
    const redirects = (await nextConfig.redirects!()) as {
      source: string;
      destination: string;
      permanent?: boolean;
    }[];
    const entry = redirects.find(
      (r) => r.source === "/api/prime/zones-geojson",
    );
    expect(entry?.destination).toBe(manifest.path);
    expect(entry?.permanent).toBe(false);
  });

  it("caches the versioned files for a year, immutable", async () => {
    const headers = await nextConfig.headers!();
    const rule = headers.find((h) => h.source === "/data/prime/:file*");
    expect(rule?.headers).toContainEqual({
      key: "Cache-Control",
      value: "public, max-age=31536000, immutable",
    });
  });

  it("makes the map fetch the manifest path and no longer the removed route", () => {
    const src = readFileSync(
      join(ROOT, "src", "components", "maps", "PrimeMap3D.tsx"),
      "utf8",
    );
    expect(src).toContain("fetch(primeZones.path)");
    expect(src).not.toContain('fetch("/api/prime/zones-geojson")');
    expect(
      existsSync(join(ROOT, "src", "app", "api", "prime", "zones-geojson")),
    ).toBe(false);
  });
});
