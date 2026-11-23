import { afterEach, describe, expect, it, vi } from "vitest";

const getAllArticles = vi.fn();
const searchArticles = vi.fn();
vi.mock("@/lib/blog/articles", () => ({
  getAllArticles: (...args: unknown[]) => getAllArticles(...args),
  searchArticles: (...args: unknown[]) => searchArticles(...args),
  getCategoryCounts: vi.fn(),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

const { GET } = await import("./route");

function call(query: string) {
  return GET({
    url: `https://balizero.com/api/blog/articles${query}`,
  } as never);
}

afterEach(() => vi.resetAllMocks());

describe("GET /api/blog/articles errors", () => {
  it("answers 500, not a demo list, when the reader throws", async () => {
    getAllArticles.mockRejectedValue(new Error("reader down"));
    const res = await call("");
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Failed to fetch articles" });
  });
});

describe("GET /api/blog/articles bounds", () => {
  it("keeps the default request: 12 items from offset 0", async () => {
    getAllArticles.mockResolvedValue({ articles: [{ slug: "a" }], total: 1 });
    const res = await call("");
    expect(res.status).toBe(200);
    expect(getAllArticles).toHaveBeenCalledWith({
      category: undefined,
      featured: undefined,
      limit: 12,
      offset: 0,
    });
    expect(await res.json()).toEqual({
      articles: [{ slug: "a" }],
      total: 1,
      hasMore: false,
    });
  });

  it("clamps an absurd limit", async () => {
    getAllArticles.mockResolvedValue({ articles: [], total: 0 });
    await call("?limit=100000");
    expect(getAllArticles.mock.calls[0][0].limit).toBe(12);
  });

  it.each(["-5", "abc", "0"])(
    "falls back to a sane limit for %s",
    async (limit) => {
      getAllArticles.mockResolvedValue({ articles: [], total: 0 });
      await call(`?limit=${limit}`);
      const used = getAllArticles.mock.calls[0][0].limit;
      expect(used).toBeGreaterThanOrEqual(1);
      expect(used).toBeLessThanOrEqual(12);
    },
  );

  it.each(["-3", "abc", "NaN"])(
    "turns offset %s into a non-negative integer",
    async (offset) => {
      getAllArticles.mockResolvedValue({ articles: [], total: 0 });
      await call(`?offset=${offset}`);
      expect(getAllArticles.mock.calls[0][0].offset).toBe(0);
    },
  );

  it("keeps a valid offset", async () => {
    getAllArticles.mockResolvedValue({ articles: [], total: 30 });
    await call("?offset=24");
    expect(getAllArticles.mock.calls[0][0].offset).toBe(24);
  });

  it("clamps the search limit too", async () => {
    searchArticles.mockResolvedValue([]);
    await call("?q=visa&limit=100000");
    expect(searchArticles).toHaveBeenCalledWith("visa", 12);
  });

  it("answers an unknown category with an empty result and never reads", async () => {
    const res = await call("?category=unknown-cat");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      articles: [],
      total: 0,
      hasMore: false,
    });
    expect(getAllArticles).not.toHaveBeenCalled();
  });

  it("forwards a known category", async () => {
    getAllArticles.mockResolvedValue({ articles: [], total: 0 });
    await call("?category=visas");
    expect(getAllArticles.mock.calls[0][0].category).toBe("visas");
  });

  it.each(["immigration", "tax", "lifestyle", "tax-legal", "visas"])(
    "forwards the legacy or canonical category %s unchanged",
    async (category) => {
      getAllArticles.mockResolvedValue({
        articles: [{ slug: "x" }],
        total: 1,
      });
      const res = await call(`?category=${category}`);
      expect(getAllArticles.mock.calls[0][0].category).toBe(category);
      expect((await res.json()).articles).toEqual([{ slug: "x" }]);
    },
  );
});
