// Guard: the visa segment footer is shown on the marketing/funnel pages and hidden
// on the self-contained product screens (GARUDA VOA flow, second home studio).
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

let mockPath = "/visa/clock";
vi.mock("next/navigation", () => ({ usePathname: () => mockPath }));
vi.mock("@/components/site/Footer", () => ({
  Footer: ({ funnel }: { funnel?: string }) => (
    <footer data-guard="site-footer" data-funnel={funnel} />
  ),
}));

import { VisaFooter, isVisaFooterHidden } from "./VisaFooter";

describe("VisaFooter", () => {
  beforeEach(() => {
    mockPath = "/visa/clock";
  });

  it.each([
    "/visa/clock",
    "/visa/clock/abc123",
    "/visa/match/abc123",
    "/visa/second-home",
    "/visa/second-home/it",
    "/visa/privacy",
  ])("renders the footer on %s", (path) => {
    mockPath = path;
    const html = renderToString(<VisaFooter />);
    expect(html).toContain('data-guard="site-footer"');
    expect(html).toContain('data-funnel="visa"');
  });

  it.each([
    "/visa/voa",
    "/visa/voa/checkout/r1",
    "/visa/voa/upload/r1",
    "/visa/voa/orders/o1/return",
    "/visa/second-home/studio",
  ])("renders nothing on %s", (path) => {
    mockPath = path;
    expect(renderToString(<VisaFooter />)).toBe("");
  });

  it("does not match a sibling that only shares the prefix text", () => {
    expect(isVisaFooterHidden("/visa/voand")).toBe(false);
    expect(isVisaFooterHidden("/visa/second-home/studios")).toBe(false);
  });
});
