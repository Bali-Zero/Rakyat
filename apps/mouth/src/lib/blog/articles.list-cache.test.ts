import { afterEach, describe, expect, it, vi } from "vitest";

// A cache that behaves like unstable_cache: one stored entry per distinct
// serialized argument list, and the wrapped computation runs on a miss only.
const computations: string[] = [];
vi.mock("next/cache", () => ({
  unstable_cache: <A extends unknown[], R>(fn: (...a: A) => Promise<R>) => {
    const store = new Map<string, Promise<R>>();
    return (...args: A) => {
      const key = JSON.stringify(args);
      if (!store.has(key)) {
        computations.push(key);
        store.set(key, fn(...args));
      }
      return store.get(key)!;
    };
  },
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
const fetchSpy = vi.fn(async () => ({ ok: false }) as Response);
vi.stubGlobal("fetch", fetchSpy);

const { getAllArticles } = await import("@/lib/blog/articles");

afterEach(() => {
  computations.length = 0;
});

describe("getAllArticles cache entries", () => {
  it("computes once for different offsets and limits of one category", async () => {
    await getAllArticles({ category: "business", limit: 12, offset: 0 });
    await getAllArticles({ category: "business", limit: 12, offset: 12 });
    await getAllArticles({ category: "business", limit: 5, offset: 777 });
    expect(computations).toHaveLength(1);
  });

  it("keeps separate entries per category and per featured flag", async () => {
    await getAllArticles({ category: "property", limit: 1 });
    await getAllArticles({ category: "living", limit: 1 });
    await getAllArticles({ featured: true, limit: 1 });
    expect(computations).toHaveLength(3);
  });

  it("slices outside the cache and keeps the total", async () => {
    const all = await getAllArticles({});
    const page = await getAllArticles({ limit: 2, offset: 1 });
    expect(page.total).toBe(all.total);
    expect(page.articles).toEqual(all.articles.slice(1, 3));
  });
});
