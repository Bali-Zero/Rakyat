// Guard: the public category (visas/taxes/living/trends) is translated to the
// backend's stored vocabulary before GET /api/news?category=… goes out. The
// backend filters with `category = $1`, and its rows carry immigration / tax /
// legal / lifestyle / tech (GET /api/news/categories, 12 Nov 2026), so sending
// the public name matched nothing and the backend items silently vanished.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
const urls: string[] = [];
vi.stubGlobal(
  "fetch",
  vi.fn(async (url: string) => {
    urls.push(String(url));
    return { ok: false } as Response;
  }),
);

const { getAllArticles } = await import("@/lib/blog/articles");
const { backendCategoriesFor } = await import("@/lib/blog/categories");

afterEach(() => {
  urls.length = 0;
});

const sentCategories = () =>
  urls
    .filter((u) => u.includes("/api/news?"))
    .map((u) => new URL(u).searchParams.get("category"))
    .sort();

describe("backend news category", () => {
  it.each([
    ["visas", "immigration"],
    ["taxes", "tax"],
    ["taxes", "legal"],
    ["living", "lifestyle"],
    ["trends", "tech"],
    ["business", "business"],
    ["property", "property"],
  ])("%s asks the backend for %s", (pub, backend) => {
    expect(backendCategoriesFor(pub)).toContain(backend);
  });

  it("asks the backend for immigration when visas is listed", async () => {
    await getAllArticles({ category: "visas" });
    const sent = sentCategories();
    expect(sent).toContain("immigration");
  });

  it("sends both tax and legal for taxes", async () => {
    await getAllArticles({ category: "taxes" });
    const sent = sentCategories();
    expect(sent).toContain("tax");
    expect(sent).toContain("legal");
  });

  it("sends no category filter when none is asked", async () => {
    await getAllArticles({ limit: 1 });
    expect(sentCategories()).toEqual([null]);
  });
});
