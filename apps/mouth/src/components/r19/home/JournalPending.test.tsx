import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/blog/articles", () => ({
  getAllArticles: vi.fn(),
  getPinnedMdxArticles: vi.fn(),
}));

import { JournalPending } from "./HomeJournal";

describe("JournalPending (homepage Suspense fallback)", () => {
  it("carries no loading placeholder copy into the server HTML", () => {
    const { container } = render(<JournalPending />);
    expect(container.textContent).not.toMatch(/loading/i);
  });

  it("is a meaningful fallback: the Journal heading and a link to /news", () => {
    render(<JournalPending />);
    expect(
      screen.getByRole("heading", { name: "The Bali Zero Journal" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /read the latest stories/i }),
    ).toHaveAttribute("href", "/news");
  });
});
