import React from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * One filled red CTA per page. /kbli/[code] showed two: the header
 * "Get Started" (kbli/layout.tsx) and the page's KBLIConsultationCTA
 * (measured in the built kbli/01111.html: 2 `var(--accent-funnel)` fills;
 * builder and decoder: 2 each). The header now steps down to an outline
 * on pages that carry their own red CTA.
 */

const { mockPathname } = vi.hoisted(() => ({ mockPathname: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => mockPathname() }));
vi.mock("@/components/lead/WhatsAppLeadButton", () => ({
  WhatsAppLeadButton: ({
    children,
    style,
  }: {
    children: React.ReactNode;
    style?: React.CSSProperties;
  }) => (
    <a href="#wa" style={style}>
      {children}
    </a>
  ),
}));

import { KBLIHeaderCTA, pageHasOwnPrimaryCTA } from "./KBLIHeaderCTA";

const fill = () =>
  (screen.getByRole("link", { name: "Get Started" }) as HTMLElement).style
    .background;

beforeEach(() => mockPathname.mockReset());

describe("KBLI header CTA", () => {
  it("is an outline on /kbli/[code], which has its own red CTA", () => {
    mockPathname.mockReturnValue("/kbli/01111");
    render(<KBLIHeaderCTA />);
    expect(fill()).not.toContain("--accent-funnel");
  });

  it("positive control: stays the filled red CTA on /kbli", () => {
    mockPathname.mockReturnValue("/kbli");
    render(<KBLIHeaderCTA />);
    expect(fill()).toContain("var(--accent-funnel)");
  });

  it("knows which pages carry their own red CTA", () => {
    expect(pageHasOwnPrimaryCTA("/kbli/62019")).toBe(true);
    expect(pageHasOwnPrimaryCTA("/kbli/builder")).toBe(true);
    expect(pageHasOwnPrimaryCTA("/kbli/decoder")).toBe(true);
    expect(pageHasOwnPrimaryCTA("/kbli")).toBe(false);
    expect(pageHasOwnPrimaryCTA("/kbli/sectors")).toBe(false);
    expect(pageHasOwnPrimaryCTA(null)).toBe(false);
  });

  it("the layout no longer hardcodes a filled red header CTA", () => {
    const layout = readFileSync(join(__dirname, "..", "layout.tsx"), "utf8");
    expect(layout).toMatch(/<KBLIHeaderCTA \/>/);
    expect(layout).not.toMatch(/background:\s*"var\(--accent-funnel\)"/);
  });
});
