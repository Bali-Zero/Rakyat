// DK-16: the /services/company meta description lists what the PT PMA service
// covers. The exact string is pinned because the previous description already
// named PT PMA and fit the length limit, so those two checks alone cannot tell
// the old copy from the new one.
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
  "Set up a PT PMA in Bali with a licensed Indonesian team: capital rules, KBLI choice, OSS licensing, notary deed, NPWP and bank account.";

const metadataFor = (slug: string) =>
  generateMetadata({ params: Promise.resolve({ slug }) });

describe("/services/company meta description (DK-16)", () => {
  it("names PT PMA within the search snippet limit", async () => {
    const meta = await metadataFor("company");
    expect(meta.description).toBe(DESCRIPTION);
    expect(meta.description).toContain("PT PMA");
    expect(String(meta.description).length).toBeLessThanOrEqual(155);
  });

  it("Open Graph and Twitter carry the same description", async () => {
    const meta = await metadataFor("company");
    expect(meta.openGraph?.description).toBe(DESCRIPTION);
    expect(meta.twitter?.description).toBe(DESCRIPTION);
  });

  it("control: another service keeps its own description", async () => {
    const meta = await metadataFor("tax");
    expect(meta.description).toContain("NPWP registration");
    expect(meta.description).not.toBe(DESCRIPTION);
  });
});
