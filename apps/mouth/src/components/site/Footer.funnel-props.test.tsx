// Guard: the shared Footer can be pinned to a funnel and to an absolute link
// origin, which tax.balizero.com needs (the proxy rewrites every path there under
// /tax-calendar, so a root-relative link 404s and the pathname never says "tax").
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
vi.mock("@balizero/core/components/BZLogo", () => ({ BZLogo: () => null }));
vi.mock("@balizero/core/analytics", () => ({ trackFunnelEvent: vi.fn() }));
vi.mock("@balizero/core/auth", () => ({ getOrCreateSessionId: () => "s" }));

import { buildWhatsAppLink } from "@/lib/whatsapp-utm";
import { Footer } from "./Footer";

const hrefs = (html: string) =>
  [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1].replace(/&amp;/g, "&"));

describe("Footer funnel pinning and link origin", () => {
  it("with linkOrigin every site link is absolute and none is root-relative", () => {
    const links = hrefs(
      renderToString(<Footer funnel="tax" linkOrigin="https://balizero.com" />),
    );
    const site = links.filter(
      (h) => h.startsWith("/") || h.includes("balizero.com/"),
    );
    expect(site.length).toBeGreaterThanOrEqual(12);
    expect(links.filter((h) => h.startsWith("/"))).toEqual([]);
    expect(links).toContain("https://balizero.com/about");
    expect(links).toContain("https://balizero.com/privacy");
    expect(links).toContain("https://balizero.com/cookies");
  });

  it("the funnel prop wins over a pathname that does not show the funnel", () => {
    const links = hrefs(renderToString(<Footer funnel="tax" />));
    expect(links).toContain(buildWhatsAppLink("tax"));
    expect(links).not.toContain(buildWhatsAppLink("home"));
  });

  it("without props the links stay root-relative and the funnel follows the pathname", () => {
    const links = hrefs(renderToString(<Footer />));
    expect(links).toContain("/about");
    expect(links).toContain(buildWhatsAppLink("home"));
  });
});
