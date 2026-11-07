import { describe, expect, it } from "vitest";
import type { ArticleListItem } from "@/lib/blog/types";
import { selectInsightArticles, selectJournalArticles } from "./adapter";

function story(slug: string, date = "2026-09-25T02:00:00Z"): ArticleListItem {
  return {
    slug,
    title: "Synthetic story",
    category: "business",
    coverImage: "/static/blog/bali-skyline.jpg",
    publishedAt: new Date(date),
  } as ArticleListItem;
}
describe("R19 Journal existing-reader adapter", () => {
  it("honours pinned slots and fills missing ones without duplicate stories", () => {
    const rows = selectJournalArticles(
      [story("new"), story("pinned"), story("other")],
      { hero_main: "pinned", hero_2: "gone", latest_1: "pinned" },
    );
    expect(rows.map((row) => row.slug)).toEqual(["pinned", "new", "other"]);
  });
  it("preserves canonical article routes and supplied images", () => {
    const [row] = selectJournalArticles([story("example")], {});
    expect(row.localHref).toBe("/business/example");
    expect(row.image?.src).toBe("/static/blog/bali-skyline.jpg");
  });
  it("does not invent dates or stories", () => {
    expect(selectJournalArticles([], {})).toEqual([]);
    expect(
      selectJournalArticles([story("invalid", "invalid")], {})[0].date,
    ).toBeNull();
  });
  it("skips an unknown category without hiding healthy articles or consuming a slot", () => {
    const invalid = {
      ...story("malformed"),
      category: "unknown-category",
    } as unknown as ArticleListItem;
    const rows = selectJournalArticles(
      [invalid, story("healthy")],
      { hero_main: "malformed" },
      1,
    );
    expect(rows.map((row) => row.slug)).toEqual(["healthy"]);
    expect(rows[0].destinationStatus).toBe("available");
  });
  it("caps the visible edition after deduplication", () => {
    expect(
      selectJournalArticles(
        Array.from({ length: 12 }, (_, i) => story("story-" + i)),
        {},
        7,
      ),
    ).toHaveLength(7);
  });
});

describe("R19 Journal Insight row", () => {
  const feed = [story("a"), story("b"), story("c"), story("d")];
  it("shows the pinned insight slots in slot order", () => {
    const rows = selectInsightArticles(feed, {
      insight_1: "c",
      insight_2: "a",
      insight_3: "d",
    });
    expect(rows.map((row) => row.slug)).toEqual(["c", "a", "d"]);
    expect(rows[0].localHref).toBe("/business/c");
  });
  it("never fills an empty or missing slot from the feed", () => {
    expect(selectInsightArticles(feed, {})).toEqual([]);
    const rows = selectInsightArticles(feed, {
      insight_1: "gone",
      insight_3: "b",
    });
    expect(rows.map((row) => row.slug)).toEqual(["b"]);
  });
  it("skips a story already shown in the journal and a repeated slug", () => {
    const rows = selectInsightArticles(
      feed,
      { insight_1: "a", insight_2: "b", insight_3: "b" },
      ["a"],
    );
    expect(rows.map((row) => row.slug)).toEqual(["b"]);
  });
  it("skips an unknown category", () => {
    const invalid = {
      ...story("malformed"),
      category: "unknown-category",
    } as unknown as ArticleListItem;
    const rows = selectInsightArticles([invalid, ...feed], {
      insight_1: "malformed",
      insight_2: "d",
    });
    expect(rows.map((row) => row.slug)).toEqual(["d"]);
  });
  it("reads only insight keys, never hero or latest", () => {
    expect(
      selectInsightArticles(feed, { hero_main: "a", latest_1: "b" }),
    ).toEqual([]);
  });
});
