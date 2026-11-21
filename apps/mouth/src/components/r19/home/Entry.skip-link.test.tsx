import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("@/components/site/NavWhatsAppCTA", () => ({
  NavWhatsAppCTA: () => null,
}));
vi.mock("@/components/lead/WhatsAppLeadButton", () => ({
  WhatsAppLeadButton: () => null,
}));
vi.mock("@/lib/whatsapp-utm", () => ({ buildWhatsAppLink: () => "#" }));

import { SiteHeader } from "./Entry";

window.matchMedia ??= (() =>
  ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList) as typeof window.matchMedia;

describe("R19 home header keyboard access", () => {
  it("renders the skip link as the first focusable element", () => {
    render(<SiteHeader />);
    const header = screen.getByRole("banner");
    const first = header.querySelector("a, button");
    expect(first?.getAttribute("href")).toBe("#main-content");
    expect(first?.textContent).toMatch(/skip to main content/i);
  });

  it("makes the page inert only while the mobile menu is open", () => {
    render(
      <>
        <SiteHeader />
        <main id="main-content" />
      </>,
    );
    const main = document.getElementById("main-content")!;
    const toggle = screen.getByRole("button", { name: /menu/i });
    expect(main.hasAttribute("inert")).toBe(false);
    fireEvent.click(toggle);
    expect(main.hasAttribute("inert")).toBe(true);
    fireEvent.click(toggle);
    expect(main.hasAttribute("inert")).toBe(false);
  });
});
