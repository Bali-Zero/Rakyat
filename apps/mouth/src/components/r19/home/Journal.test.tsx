import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Journal } from "./Journal";
import { ArticleDestination } from "./journal/ArticleDestination";
import type { JournalArticle } from "./journal/types";
afterEach(cleanup);
const articles: JournalArticle[] = [1, 2].map((i) => ({
  title: "Synthetic story " + i,
  slug: "story-" + i,
  category: "Business",
  date: null,
  image: null,
  sourceUrl: "/business/story-" + i,
  finalSourceUrl: null,
  destinationStatus: "available",
}));
describe("R19 Journal presentation", () => {
  it("moves between real supplied stories using buttons and arrow keys", () => {
    render(<Journal articles={articles} />);
    expect(
      screen.getByRole("link", { name: "Synthetic story 1" }),
    ).toBeDefined();
    fireEvent.click(
      screen.getByRole("button", { name: "Next editorial story" }),
    );
    expect(
      screen.getByRole("link", { name: "Synthetic story 2" }),
    ).toBeDefined();
    fireEvent.keyDown(screen.getByLabelText("Featured editorial stories"), {
      key: "ArrowLeft",
    });
    expect(
      screen.getByRole("link", { name: "Synthetic story 1" }),
    ).toBeDefined();
  });
  it("renders an unavailable destination without a clickable link", () => {
    render(
      <ArticleDestination
        article={{ ...articles[0], destinationStatus: "unavailable" }}
      >
        Synthetic story
      </ArticleDestination>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Synthetic story")).toBeDefined();
  });
  it("distinguishes unavailable from empty and hides stale supplied stories", () => {
    render(<Journal articles={articles} status="unavailable" />);
    expect(screen.getByRole("status").textContent).toContain(
      "temporarily unavailable",
    );
    expect(
      screen.queryByRole("link", { name: "Synthetic story 1" }),
    ).toBeNull();
  });
});

describe("R19 Journal Insight row", () => {
  const insights: JournalArticle[] = [1, 2, 3].map((i) => ({
    title: "Insight story " + i,
    slug: "insight-" + i,
    category: "Tax & Legal",
    date: null,
    image: { src: "/static/news/insight-" + i + ".jpg", alt: "cover " + i },
    sourceUrl: "/tax-legal/insight-" + i,
    finalSourceUrl: null,
    destinationStatus: "available",
  }));
  it("renders the pinned insights as text links under one Insight label", () => {
    render(<Journal articles={articles} insights={insights} />);
    const row = screen.getByRole("region", { name: "Insight" });
    for (const i of [1, 2, 3]) {
      expect(
        screen.getByRole("link", { name: new RegExp("Insight story " + i) }),
      ).toBeDefined();
    }
    expect(row.querySelectorAll("article")).toHaveLength(3);
    expect(row.querySelectorAll("img")).toHaveLength(0);
  });
  it("renders no Insight row without pinned insights", () => {
    render(<Journal articles={articles} />);
    expect(screen.queryByRole("region", { name: "Insight" })).toBeNull();
  });
  it("renders no Insight row when the feed is not ready", () => {
    render(
      <Journal articles={articles} insights={insights} status="unavailable" />,
    );
    expect(screen.queryByRole("region", { name: "Insight" })).toBeNull();
  });
});

describe("R19 Journal homepage position markers", () => {
  const placed: JournalArticle[] = ["hero_main", "hero_2", undefined].map(
    (slot, i) => ({
      ...articles[0],
      title: "Placed story " + i,
      slug: "placed-" + i,
      sourceUrl: "/business/placed-" + i,
      localHref: "/business/placed-" + i,
      ...(slot ? { homepagePosition: slot } : {}),
    }),
  );
  it("keeps every featured story's link in the page with its slot, the inactive ones hidden", () => {
    const { container } = render(<Journal articles={placed} />);
    const marked = () =>
      [...container.querySelectorAll("a[data-homepage-position]")].map((a) => [
        a.getAttribute("data-homepage-position"),
        a.getAttribute("href"),
        a.closest("[hidden]") !== null,
      ]);
    expect(marked()).toEqual([
      ["hero_main", "/business/placed-0", false],
      ["hero_2", "/business/placed-1", true],
    ]);
    expect(screen.queryByRole("link", { name: "Placed story 1" })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Next editorial story" }),
    );
    expect(marked()).toEqual([
      ["hero_2", "/business/placed-1", false],
      ["hero_main", "/business/placed-0", true],
    ]);
  });
  it("marks the Insight cards with their insight slots", () => {
    const pins: JournalArticle[] = [1, 2].map((i) => ({
      ...articles[0],
      title: "Pin " + i,
      slug: "pin-" + i,
      sourceUrl: "/visas/pin-" + i,
      localHref: "/visas/pin-" + i,
      homepagePosition: "insight_" + i,
    }));
    render(<Journal articles={articles} insights={pins} />);
    const row = screen.getByRole("region", { name: "Insight" });
    expect(
      [...row.querySelectorAll("a")].map((a) =>
        a.getAttribute("data-homepage-position"),
      ),
    ).toEqual(["insight_1", "insight_2"]);
  });
});
