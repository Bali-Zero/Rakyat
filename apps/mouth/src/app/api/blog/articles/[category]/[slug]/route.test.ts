import { afterEach, describe, expect, it, vi } from "vitest";

const getArticleBySlug = vi.fn();
const isArticleHidden = vi.fn();
vi.mock("@/lib/blog/articles", () => ({
  getArticleBySlug: (...args: unknown[]) => getArticleBySlug(...args),
  isArticleHidden: (...args: unknown[]) => isArticleHidden(...args),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

const { GET } = await import("./route");

function call(category: string, slug: string) {
  return GET({} as never, { params: Promise.resolve({ category, slug }) });
}

afterEach(() => vi.clearAllMocks());

describe("GET /api/blog/articles/[category]/[slug] and hidden articles", () => {
  it("answers 404 for a hidden article, even on a demo slug", async () => {
    getArticleBySlug.mockResolvedValue(null);
    isArticleHidden.mockReturnValue(true);
    const res = await call("business", "oss-2-complete-guide");
    expect(res.status).toBe(404);
  });

  it("never serves the demo for a hidden article when the reader throws", async () => {
    getArticleBySlug.mockRejectedValue(new Error("reader down"));
    isArticleHidden.mockReturnValue(true);
    const res = await call("business", "oss-2-complete-guide");
    expect(res.status).toBe(500);
  });

  it("answers 404, not a demo article, when no real article has the slug", async () => {
    getArticleBySlug.mockResolvedValue(null);
    isArticleHidden.mockReturnValue(false);
    for (const [category, slug] of [
      ["business", "oss-2-complete-guide"],
      ["immigration", "golden-visa-revolution"],
      ["property", "ota-delisting-bali-2026"],
    ]) {
      const res = await call(category, slug);
      expect(res.status).toBe(404);
      expect(await res.json()).toEqual({ error: "Article not found" });
    }
  });

  it("answers 500, not a demo article, when the reader throws on a demo slug", async () => {
    getArticleBySlug.mockRejectedValue(new Error("reader down"));
    isArticleHidden.mockReturnValue(false);
    const res = await call("business", "oss-2-complete-guide");
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Failed to fetch article" });
  });

  it("still serves a real article with its content", async () => {
    getArticleBySlug.mockResolvedValue({
      id: "a1",
      slug: "real-one",
      title: "Real one",
      category: "business",
      content: "Hello **world**",
    });
    const res = await call("business", "real-one");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.slug).toBe("real-one");
    expect(body.content).toContain("Hello");
  });
});
