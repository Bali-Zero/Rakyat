// The /services/tax meta description ran to 166 characters, past the search
// snippet limit, so the served snippet was cut. The new copy names what the
// tax service covers (NPWP, monthly reporting, annual SPT, BPJS), fits the
// limit and carries no figure. The exact string is pinned because the old
// copy also named NPWP, so the NPWP check alone cannot tell them apart.
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/services/ServicePricing", () => ({
  default: () => null,
}));
vi.mock("@/components/seo", () => ({
  BreadcrumbJsonLd: () => null,
  FAQJsonLd: () => null,
}));

import { generateMetadata } from "./page";

const DESCRIPTION =
  "Indonesia tax compliance for expats and companies in Bali: NPWP registration, monthly tax reporting, annual SPT filing and BPJS, by a licensed team.";

const metadataFor = (slug: string) =>
  generateMetadata({ params: Promise.resolve({ slug }) });

describe("/services/tax meta description", () => {
  it("names NPWP within the search snippet limit, with no figure", async () => {
    const meta = await metadataFor("tax");
    expect(meta.description).toBe(DESCRIPTION);
    expect(meta.description).toContain("NPWP");
    expect(String(meta.description).length).toBeLessThanOrEqual(155);
    expect(meta.description).not.toMatch(/\d/);
  });

  it("Open Graph and Twitter carry the same description", async () => {
    const meta = await metadataFor("tax");
    expect(meta.openGraph?.description).toBe(DESCRIPTION);
    expect(meta.twitter?.description).toBe(DESCRIPTION);
  });

  it("control: the tax title is untouched", async () => {
    const meta = await metadataFor("tax");
    expect(meta.title).toBe(
      "Indonesia Tax Compliance 2026 | NPWP, Tax Filing & Advisory | Bali Zero",
    );
  });
});
