import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ContactOptions } from "./ContactOptions";

vi.mock("@/components/lead/WhatsAppLeadButton", () => ({
  FALLBACK_WA_URL: "https://wa.me/628213454721",
  WhatsAppLeadButton: ({
    children,
    fallbackHref,
    source,
    context,
  }: {
    children: React.ReactNode;
    fallbackHref: string;
    source: string;
    context: object;
  }) => (
    <a
      href={fallbackHref}
      data-source={source}
      data-context={JSON.stringify(context)}
    >
      {children}
    </a>
  ),
}));
afterEach(cleanup);
describe("R19 topic-aware contact adapter", () => {
  it("uses the existing capture source and carries a changed topic into both destinations", () => {
    render(<ContactOptions />);
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "property" },
    });
    const whatsapp = screen.getByRole("link", { name: /Continue on WhatsApp/ });
    expect(whatsapp.getAttribute("data-source")).toBe("cta_handoff");
    expect(JSON.parse(whatsapp.getAttribute("data-context")!)).toEqual({
      topic: "property",
      source_page: "/",
      section: "contact",
    });
    expect(decodeURIComponent(whatsapp.getAttribute("href")!)).toContain(
      "Property due diligence",
    );
    expect(
      decodeURIComponent(
        screen
          .getByRole("link", { name: /Write an email/ })
          .getAttribute("href")!,
      ),
    ).toContain("Property due diligence");
  });

  it("says where the message is written, since this widget has no message field", () => {
    render(<ContactOptions />);
    // The widget is a topic picker plus two hand-offs: no textarea exists, and
    // none is sent in the lead payload (context = topic/source_page/section).
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Your topic carries into WhatsApp or your email app/),
    ).toHaveTextContent(
      "Write your message there and review it before sending.",
    );
  });
});
