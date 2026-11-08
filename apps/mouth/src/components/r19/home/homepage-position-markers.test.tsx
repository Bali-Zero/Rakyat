import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import homepageLayout from "@/content/homepage-layout.json";

vi.mock("next/cache", () => ({ unstable_cache: <T,>(fn: T) => fn }));
vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
const { getAllArticles, getPinnedMdxArticles } =
  await import("@/lib/blog/articles");
const { HomeJournal } = await import("./HomeJournal");
afterEach(cleanup);

const layout: Record<string, string> = homepageLayout;
const heroSlots = ["hero_main", "hero_2", "hero_3", "hero_4", "hero_5"];
const insightSlots = ["insight_1", "insight_2", "insight_3"];

/**
 * The News Room live verifier (nuzantara-mcp workspace_marketing.py,
 * _homepage_position_live) confirms a hero_* or insight_* publication only
 * when the home has a link with data-homepage-position equal to the slot and
 * href equal to the article's URL. This renders the home's Journal over the
 * real articles and the committed layout and asks that question per slot.
 */
describe("homepage position markers on the rendered home", () => {
  it("give every placed hero and Insight slot of the committed layout a marked link", async () => {
    const { articles } = await getAllArticles({});
    const listed = new Map(articles.map((a) => [a.slug, a.category]));
    const pinned = new Map(
      (await getPinnedMdxArticles(insightSlots.map((s) => layout[s]))).map(
        (a) => [a.slug, a.category],
      ),
    );
    const { container } = render(await HomeJournal());
    const marks = new Set(
      [...container.querySelectorAll("a[data-homepage-position]")].map(
        (a) =>
          a.getAttribute("data-homepage-position") +
          " " +
          a.getAttribute("href"),
      ),
    );
    const seen = new Set<string>();
    const expected: string[] = [];
    for (const [slots, source] of [
      [heroSlots, listed],
      [insightSlots, pinned],
    ] as const) {
      for (const slot of slots) {
        const slug = layout[slot];
        const category = slug ? source.get(slug) : undefined;
        if (!slug || !category || seen.has(slug)) continue;
        seen.add(slug);
        expected.push(slot + " /" + category + "/" + slug);
      }
    }
    expect(
      expected.filter((e) => e.startsWith("hero_")).length,
    ).toBeGreaterThan(1);
    expect(
      expected.filter((e) => e.startsWith("insight_")).length,
    ).toBeGreaterThan(0);
    expect(expected.filter((e) => !marks.has(e))).toEqual([]);
  }, 120_000);
});
