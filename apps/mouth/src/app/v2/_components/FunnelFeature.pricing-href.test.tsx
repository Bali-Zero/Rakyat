// Guard: every funnel card's "pricing" CTA must land on a public page that
// exists. Measured 12 Nov 2026:
// - kbli pointed at https://kita.balizero.com/kbli/pricing, the internal host
//   (noindex), answering "Page not found".
// - property pointed at https://balizero.com/pricing → HTTP 404.
// - tax pointed at https://tax.balizero.com/pricing → HTTP 200 "Page not found".
// - visa pointed at https://visa.balizero.com/pricing → 302 to /visa-oracle,
//   which shows no prices.
// The /services/<funnel> pages are the public pages that carry the packages.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({
  trackVisaCTA: vi.fn(),
  trackKBLICTA: vi.fn(),
  trackTaxCTA: vi.fn(),
  trackPropertyCTA: vi.fn(),
  trackFunnelCTA: vi.fn(),
}));

import { SERVICES_DATA } from "@/data/services_data";
import { FunnelFeature } from "./FunnelFeature";

const hrefs = (html: string) =>
  [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1].replace(/&amp;/g, "&"));

// No route serves /pricing on any balizero.com host.
const DEAD_PRICING = /^(https?:\/\/[a-z.]*balizero\.com)?\/pricing\/?$/;

const linksFor = (funnel: "visa" | "kbli" | "tax" | "property") =>
  hrefs(renderToString(<FunnelFeature funnel={funnel} layout="full" />));

describe("FunnelFeature KBLI pricing door", () => {
  const links = linksFor("kbli");

  it("sends no link to the internal kita host", () => {
    expect(links.length).toBeGreaterThan(0);
    expect(links.filter((h) => h.includes("kita.balizero.com"))).toEqual([]);
  });

  it("points the pricing CTA at the public company services page", () => {
    expect(links).toContain("/services/company");
    expect(Object.keys(SERVICES_DATA)).toContain("company");
  });
});

describe("FunnelFeature pricing doors never hit a dead /pricing", () => {
  it.each([
    ["property", "/services/property"],
    ["visa", "/services/visa"],
    ["tax", "/services/tax"],
  ] as const)("%s pricing CTA → %s", (funnel, target) => {
    const links = linksFor(funnel);
    expect(links.length).toBeGreaterThan(0);
    expect(links.filter((h) => DEAD_PRICING.test(h))).toEqual([]);
    expect(links).toContain(target);
    expect(Object.keys(SERVICES_DATA)).toContain(target.split("/").pop());
  });
});

describe("no source file links to a dead /pricing", () => {
  const SRC = join(__dirname, "..", "..", "..");
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(tsx?|jsx?)$/.test(name) && !/\.test\./.test(name))
        files.push(p);
    }
  };
  walk(SRC);

  it("finds no href to balizero.com/pricing or bare /pricing", () => {
    expect(files.length).toBeGreaterThan(100);
    const offenders = files.filter((f) =>
      /["'`](https?:\/\/[a-z.]*balizero\.com)?\/pricing\/?["'`]/.test(
        readFileSync(f, "utf8"),
      ),
    );
    expect(offenders).toEqual([]);
  });
});
