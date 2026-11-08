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

  it("keeps the demo stand-in when no file hides the slug", async () => {
    getArticleBySlug.mockResolvedValue(null);
    isArticleHidden.mockReturnValue(false);
    const res = await call("business", "oss-2-complete-guide");
    expect(res.status).toBe(200);
    expect((await res.json()).slug).toBe("oss-2-complete-guide");
  });
});
