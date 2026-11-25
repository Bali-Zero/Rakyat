// The /services/visa meta description served "1000+ expats served", a client
// count that trust-figures.ts deliberately does not publish because it is not
// verified, and that contradicts the homepage figure. It also ran past the
// search snippet limit. The new copy names KITAS, fits the limit and carries
// no figure at all.
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
  "Bali visas and KITAS handled end to end by a licensed Indonesian team: visit visas, investor, working, family and retirement KITAS, documents, timelines.";

const metadataFor = (slug: string) =>
  generateMetadata({ params: Promise.resolve({ slug }) });

describe("/services/visa meta description", () => {
  it("names KITAS within the search snippet limit, with no figure", async () => {
    const meta = await metadataFor("visa");
    expect(meta.description).toBe(DESCRIPTION);
    expect(meta.description).toContain("KITAS");
    expect(String(meta.description).length).toBeLessThanOrEqual(155);
    expect(meta.description).not.toMatch(/\d/);
  });

  it("Open Graph and Twitter carry the same description", async () => {
    const meta = await metadataFor("visa");
    expect(meta.openGraph?.description).toBe(DESCRIPTION);
    expect(meta.twitter?.description).toBe(DESCRIPTION);
  });

  it("control: the visa title is untouched", async () => {
    const meta = await metadataFor("visa");
    expect(meta.title).toBe(
      "Bali Visa Agency 2026 | KITAS, Visit & Working Visa Indonesia | Bali Zero",
    );
  });
});
