// Guard: all 4 L1 funnel layouts (visa, kbli, tax, property) render the shared
// site Footer, the same one /kbli already had. Shell pieces are stubbed so the
// test only asks "does the layout mount <Footer />?". kbli is the control.
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/app/v2/_components/Footer", () => ({
  Footer: () => <footer data-guard="site-footer" />,
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/visa/clock" }));
vi.mock("@balizero/core", () => ({
  NavShell: () => null,
  BZLogo: () => null,
}));
vi.mock("@balizero/core/fonts/montserrat", () => ({
  montserrat: { variable: "font-guard" },
}));
vi.mock("@/app/v2/_components/MobileNav", () => ({ MobileNav: () => null }));
vi.mock("@/components/funnel/SessionInit", () => ({ SessionInit: () => null }));
vi.mock("@/components/funnel/HeaderWhatsAppCTA", () => ({
  HeaderWhatsAppCTA: () => null,
}));
vi.mock("@/app/kbli/_components/KBLIHeaderCTA", () => ({
  KBLIHeaderCTA: () => null,
}));
vi.mock("@/components/r19/R19Presentation", () => ({
  R19Presentation: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
}));
vi.mock("@/styles/kbli-r19-wrapper.css", () => ({}));

import VisaLayout from "@/app/visa/layout";
import KBLILayout from "@/app/kbli/layout";
import TaxCalendarLayout from "@/app/(tax-calendar)/tax-calendar/layout";
import PropertyLayout from "@/app/property/eligibility/layout";

const CASES: Array<[string, () => React.ReactElement]> = [
  ["visa", () => <VisaLayout>funnel child</VisaLayout>],
  ["kbli", () => <KBLILayout panel={null}>funnel child</KBLILayout>],
  ["tax", () => <TaxCalendarLayout>funnel child</TaxCalendarLayout>],
  ["property", () => <PropertyLayout>funnel child</PropertyLayout>],
];

describe("L1 funnel layouts render the site footer", () => {
  it.each(CASES)("%s layout renders <Footer /> after the page", (_, el) => {
    const html = renderToString(el());
    expect(html).toContain("funnel child");
    expect(html.match(/data-guard="site-footer"/g)).toHaveLength(1);
    expect(html.indexOf("funnel child")).toBeLessThan(
      html.indexOf('data-guard="site-footer"'),
    );
  });
});
