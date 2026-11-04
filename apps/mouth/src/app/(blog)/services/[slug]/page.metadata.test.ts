import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/services/ServicePricing", () => ({
  default: () => null,
}));
vi.mock("@/components/seo", () => ({
  BreadcrumbJsonLd: () => null,
  FAQJsonLd: () => null,
}));

import { generateMetadata } from "./page";
import { metadata as servicesLayoutMetadata } from "../layout";

describe("BZ-010: service detail and layout Open Graph / Twitter metadata", () => {
  it("services layout defines og:image and twitter card alongside canonical", () => {
    expect(servicesLayoutMetadata.alternates?.canonical).toBe(
      "https://balizero.com/services",
    );
    expect(servicesLayoutMetadata.openGraph?.url).toBe(
      "https://balizero.com/services",
    );
    const images = Array.isArray(servicesLayoutMetadata.openGraph?.images)
      ? servicesLayoutMetadata.openGraph.images
      : [servicesLayoutMetadata.openGraph?.images];
    expect(images[0]).toMatchObject({
      url: "/static/og-image.jpg",
      width: 1024,
      height: 1024,
    });
  });

  it.each(["visa", "company", "tax", "property"])(
    "emits page-specific canonical, openGraph, and twitter metadata for /services/%s",
    async (slug) => {
      const meta = await generateMetadata({
        params: Promise.resolve({ slug }),
      });

      const expectedUrl = `https://balizero.com/services/${slug}`;
      expect(meta.alternates?.canonical).toBe(expectedUrl);
      expect(meta.openGraph?.url).toBe(expectedUrl);
      expect(meta.openGraph?.title).toBe(meta.title);
      expect(meta.openGraph?.description).toBe(meta.description);
      expect(meta.openGraph?.siteName).toBe("Bali Zero");

      const ogImages = Array.isArray(meta.openGraph?.images)
        ? meta.openGraph.images
        : [meta.openGraph?.images];
      expect(ogImages[0]).toMatchObject({
        url: "/static/og-image.jpg",
        width: 1024,
        height: 1024,
        alt: meta.title,
      });

      expect(meta.twitter).toMatchObject({
        card: "summary_large_image",
        site: "@balizero",
        creator: "@balizero",
        title: meta.title,
        description: meta.description,
        images: ["/static/og-image.jpg"],
      });
    },
  );

  it.each(["unknown-service", "constructor", "__proto__", "toString"])(
    "returns not-found title and no social preview override for invalid or prototype slug %s",
    async (slug) => {
      const meta = await generateMetadata({
        params: Promise.resolve({ slug }),
      });
      expect(meta).toEqual({
        title: "Service Not Found | Bali Zero",
      });
      expect(meta.openGraph).toBeUndefined();
      expect(meta.alternates).toBeUndefined();
    },
  );
});
