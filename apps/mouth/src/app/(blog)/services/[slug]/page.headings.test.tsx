// Guard: the on-page headings of /services/visa and /services/company carry
// the commercial keywords their titles already target. The visa H1 was only
// "Visa & Immigration" (no "visa agency", no "KITAS"), and no H2 on the
// company page named "PT PMA" (measured on origin/main, 12 Nov 2026).
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("@/components/services/ServicePricing", () => ({
  default: () => null,
}));
vi.mock("@/components/seo", () => ({
  BreadcrumbJsonLd: () => null,
  FAQJsonLd: () => null,
}));

import { SERVICES_DATA } from "@/data/services_data";
import ServiceDetailPage, { generateMetadata } from "./page";

async function headings(slug: string, tag: "h1" | "h2"): Promise<string[]> {
  const el = await ServiceDetailPage({ params: Promise.resolve({ slug }) });
  const html = renderToStaticMarkup(el);
  const re = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "g");
  return [...html.matchAll(re)].map((m) =>
    m[1]
      .replace(/<[^>]+>/g, "")
      .replace(/&#x27;|&#39;/g, "'")
      .replace(/&amp;/g, "&"),
  );
}

describe("/services/[slug] keyword headings", () => {
  it("visa: single H1 naming the visa agency and KITAS", async () => {
    const h1 = await headings("visa", "h1");
    expect(h1).toHaveLength(1);
    expect(h1[0]).toMatch(/visa agency/i);
    expect(h1[0]).toMatch(/KITAS/);
  });

  it("company: an H2 names PT PMA", async () => {
    const h2 = await headings("company", "h2");
    expect(h2.some((h) => /PT PMA/.test(h))).toBe(true);
  });

  it("company: title and single H1 name PT PMA (DK-16)", async () => {
    const meta = await generateMetadata({
      params: Promise.resolve({ slug: "company" }),
    });
    expect(meta.title).toBe("PT PMA & Company Setup in Bali 2026 | Bali Zero");
    expect(String(meta.title).length).toBeLessThanOrEqual(60);
    expect(await headings("company", "h1")).toEqual([
      "PT PMA & Company Setup in Bali",
    ]);
  });

  it("company: the H1 override leaves the shared service name alone", () => {
    expect(SERVICES_DATA.company.name).toBe("Company Setup & Licenses");
  });

  it("other services keep their plain headings", async () => {
    expect(await headings("tax", "h1")).toEqual(["Tax & BPJS Services"]);
    expect(await headings("tax", "h2")).toContain("What's Included");
  });
});
