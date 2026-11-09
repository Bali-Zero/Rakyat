import { AsyncLocalStorage } from "node:async_hooks";
import { describe, expect, it, vi } from "vitest";
import nextConfig from "../../../../next.config";
import homepageLayout from "@/content/homepage-layout.json";
import { insightSlots } from "./journal/adapter";

// Next's own matcher reads AsyncLocalStorage from the global scope, which jsdom does not set.
(globalThis as { AsyncLocalStorage?: unknown }).AsyncLocalStorage ??=
  AsyncLocalStorage;
vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

const { unstable_getResponseFromNextConfig } =
  await import("next/experimental/testing/server");
const { getArticleBySlug, getPinnedMdxArticles } =
  await import("@/lib/blog/articles");

async function route(path: string) {
  const res = await unstable_getResponseFromNextConfig({
    url: "https://balizero.com" + path,
    nextConfig,
  });
  return { status: res.status, location: res.headers.get("location") };
}

// The layout is a JSON file that changes only through a PR, so this guard runs on every
// pin before it ships: a pin the row would show must open its own article page.
describe("Insight pins in homepage-layout.json", () => {
  it("open their article page, never a next.config redirect", async () => {
    const layout: Record<string, string> = homepageLayout;
    const pins = await getPinnedMdxArticles(
      insightSlots.map((slot) => layout[slot]),
    );
    for (const pin of pins) {
      const path = "/" + pin.category + "/" + pin.slug;
      expect(await route(path), path).toEqual({ status: 200, location: null });
      expect(
        (await getArticleBySlug(pin.category, pin.slug))?.title,
        path,
      ).toBe(pin.title);
    }
  });

  it("sees next.config redirects, so a pass is not vacuous", async () => {
    expect(await route("/business/kbli-codes-explained")).toEqual({
      status: 308,
      location: "https://balizero.com/kbli",
    });
  });
});
