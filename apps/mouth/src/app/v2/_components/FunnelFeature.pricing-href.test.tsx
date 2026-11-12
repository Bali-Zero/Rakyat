// Guard: the KBLI card's "pricing" CTA must land on a public page that shows
// prices. It pointed at https://kita.balizero.com/kbli/pricing, which is the
// internal host (noindex) and answers "Page not found" (measured 12 Nov 2026).
// /services/company carries the company-setup and licensing packages.
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

describe("FunnelFeature KBLI pricing door", () => {
  const links = hrefs(
    renderToString(<FunnelFeature funnel="kbli" layout="full" />),
  );

  it("sends no link to the internal kita host", () => {
    expect(links.length).toBeGreaterThan(0);
    expect(links.filter((h) => h.includes("kita.balizero.com"))).toEqual([]);
  });

  it("points the pricing CTA at the public company services page", () => {
    expect(links).toContain("/services/company");
    expect(Object.keys(SERVICES_DATA)).toContain("company");
  });
});
