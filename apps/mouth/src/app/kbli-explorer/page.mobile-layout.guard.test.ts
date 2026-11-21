import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import KBLIExplorerPage from "./page";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/lib/analytics", () => ({
  trackKBLISearch: vi.fn(),
}));

const inspectMock = vi.fn();
const chatMock = vi.fn();
vi.mock("@/lib/api/kbli.api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/kbli.api")>(
      "@/lib/api/kbli.api",
    );
  return {
    ...actual,
    kbliApi: {
      search: vi.fn().mockResolvedValue([]),
      inspect: (...args: unknown[]) => inspectMock(...args),
      chat: (...args: unknown[]) => chatMock(...args),
    },
  };
});

/**
 * Mobile (<768px) layout guards for /kbli-explorer:
 * 1. Menu over search lens ("menu sopra la lente"): mobile hamburger sits as a
 *    flex sibling before <form>, never overlapping the Search lens inside <form>.
 * 2. Suggestion under send arrow ("suggestion sotto la freccia invio"): the
 *    rotating suggestion overlay carries both left and right bounds clearing
 *    the Send button, with zero line-clamp / overflow-hidden truncation.
 * 3. W3d scar guard: when a conversation is open (messages.length > 0), Compare
 *    and Clear controls sit outside <form> (below the bar on <768px, inline at
 *    md:+), so the rotating suggestion keeps its full phone width and is never
 *    hidden, clipped, or squeezed to a 56-150px column.
 */
describe("/kbli-explorer <768px header & rotating suggestion layout", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    inspectMock.mockReset();
    chatMock.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("keeps the mobile menu button outside <form> as a flex sibling so it never overlaps the search lens", () => {
    render(React.createElement(KBLIExplorerPage));

    const menuBtn = screen.getByTestId("explorer-mobile-menu-button");
    const lens = screen.getByTestId("explorer-search-lens");
    const input = screen.getByRole("textbox", {
      name: "Ask a question about KBLI",
    });
    const form = input.closest("form");

    expect(form).not.toBeNull();
    expect(form!.contains(menuBtn)).toBe(false);
    expect(form!.contains(lens)).toBe(true);

    const row = menuBtn.parentElement;
    expect(row).not.toBeNull();
    expect(row!.contains(form)).toBe(true);
    expect(row!.className).toContain("flex");
    expect(row!.className).toContain("items-center");
    expect(row!.className).toContain("gap-2");

    expect(menuBtn.className).toContain("md:hidden");
    expect(menuBtn.className).toContain("shrink-0");
    expect(menuBtn.className).not.toContain("absolute");
    expect(menuBtn.className).not.toContain("-left-1");

    expect(form!.className).toContain("relative");
    expect(form!.className).toContain("flex-1");
    expect(form!.className).toContain("min-w-0");
  });

  it("bounds the rotating suggestion away from the send arrow without clipping or shrinking desktop controls", () => {
    render(React.createElement(KBLIExplorerPage));

    const suggestion = screen.getByTestId("explorer-rotating-suggestion");
    const sendBtn = screen.getByTestId("explorer-send-button");
    const input = screen.getByRole("textbox", {
      name: "Ask a question about KBLI",
    });

    expect(suggestion).toHaveTextContent(
      "What do I need to open a restaurant in Bali?",
    );
    expect(suggestion.className).toContain("left-10");
    expect(suggestion.className).toContain("md:left-14");
    expect(suggestion.className).toContain("right-14");
    expect(suggestion.className).toContain("md:right-16");

    // Must never use ellipsis / line-clamp / overflow-hidden that clipped W3d
    expect(suggestion.className).not.toMatch(
      /\b(truncate|overflow-hidden|line-clamp-\d+)\b/,
    );

    // Send button stays anchored at right-2 (<768px) / md:right-3 (>=768px), cleared by right-14 (56px > 8+44=52px)
    expect(sendBtn.className).toContain("right-2");
    expect(sendBtn.className).toContain("md:right-3");

    // Desktop padding/sizing stays untouched
    expect(input.className).toContain("md:py-5");
    expect(input.className).toContain("md:pl-14");
    expect(suggestion.className).toContain("md:text-base");
  });

  it("W3d scar guard: opening a conversation does NOT hide, clip, or narrow the mobile suggestion", async () => {
    window.sessionStorage.setItem(
      "kbli-messages",
      JSON.stringify([
        { role: "user", content: "I want to open a restaurant in Bali" },
        {
          role: "ai",
          content: "For a restaurant in Bali you need KBLI 56101.",
          detected_kbli: ["56101"],
          results: [],
          suggested_queries: [
            "What licenses are required for KBLI 56101?",
            "Can a PT PMA own 100% of a restaurant?",
          ],
        },
      ]),
    );

    render(React.createElement(KBLIExplorerPage));

    // Wait for sessionStorage hydration effect
    const convActions = await screen.findByTestId(
      "explorer-conversation-actions",
    );
    const suggestion = screen.getByTestId("explorer-rotating-suggestion");
    const input = screen.getByRole("textbox", {
      name: "Ask a question about KBLI",
    });
    const form = input.closest("form")!;

    // 1. Rotating suggestion remains visible and whole when query is empty and conversation is open
    expect(suggestion).toBeInTheDocument();
    expect(suggestion).toHaveTextContent(
      "What do I need to open a restaurant in Bali?",
    );

    // 2. Compare + Clear sit OUTSIDE <form> (below the bar on <768px via mt-2 flex, md:absolute on desktop),
    // so mobile right-bound stays `right-14` (never `right-40` / `right: 10rem` on <768px!)
    expect(form.contains(convActions)).toBe(false);
    expect(convActions.className).toContain("mt-2");
    expect(convActions.className).toContain("md:absolute");
    expect(suggestion.className).toContain("right-14");
    expect(suggestion.className).toContain("md:right-40");
    expect(suggestion.className).not.toMatch(/(?<![\w:])right-40\b/);

    // 3. Follow-up suggested_queries pills in the open conversation also remain whole and interactive
    expect(
      await screen.findByRole("button", {
        name: "What licenses are required for KBLI 56101?",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Can a PT PMA own 100% of a restaurant?",
      }),
    ).toBeInTheDocument();

    // Typing hides the rotating placeholder overlay cleanly; clearing query restores it
    fireEvent.change(input, { target: { value: "villa" } });
    expect(
      screen.queryByTestId("explorer-rotating-suggestion"),
    ).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "" } });
    expect(
      screen.getByTestId("explorer-rotating-suggestion"),
    ).toBeInTheDocument();
  });
});
