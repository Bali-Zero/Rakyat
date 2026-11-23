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
