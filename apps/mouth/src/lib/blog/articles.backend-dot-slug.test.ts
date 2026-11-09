import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
const urls: string[] = [];
let reply: () => unknown = () => ({ ok: false });
const fetchSpy = vi.fn(async (url: string) => {
  urls.push(String(url));
  return reply() as Response;
});
vi.stubGlobal("fetch", fetchSpy);

const { getArticleBySlug } = await import("@/lib/blog/articles");
const { GET } = await import("@/app/api/blog/articles/[category]/[slug]/route");

afterEach(() => {
  urls.length = 0;
  reply = () => ({ ok: false });
  fetchSpy.mockClear();
});

// Next 16.3.5 strips a leading _NEXTSEP_ from a param value, so the article API can be
// handed "..", "." and "" as the slug. encodeURIComponent leaves the first two alone and the
// outgoing URL parser collapses them ("/api/news/.." is "/api/").
describe("empty and dot-segment slugs never reach the backend", () => {
  it.each(["..", ".", ""])(
    "getArticleBySlug(%j) makes no fetch",
    async (slug) => {
      expect(await getArticleBySlug("visas", slug)).toBeNull();
      expect(fetchSpy).not.toHaveBeenCalled();
    },
  );

  // The route unit-test seam receives params already decoded; the _NEXTSEP_ stripping is done
  // by Next's router and cannot run here, so the handler is driven with the stripped values.
  it.each(["..", ".", ""])(
    "the article API route with slug %j makes no fetch",
    async (slug) => {
      const res = await GET({} as never, {
        params: Promise.resolve({ category: "visas", slug }),
      });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(res.status).toBe(404);
    },
  );

  it("a backend-only staging id still resolves through the backend", async () => {
    const slug = "news_20261108_120000_abc123";
    reply = () => ({
      ok: true,
      json: async () => ({
        success: true,
        data: {
          id: slug,
          title: "Backend only title",
          slug,
          summary: "s",
          content: "body",
          source: "x",
          source_url: null,
          category: "immigration",
          priority: "normal",
          status: "approved",
          image_url: null,
          view_count: 0,
          published_at: "2026-11-08T12:00:00Z",
          created_at: "2026-11-08T12:00:00Z",
          ai_summary: null,
          ai_tags: null,
        },
      }),
    });
    const a = await getArticleBySlug("visas", slug);
    expect(a?.title).toBe("Backend only title");
    expect(urls).toHaveLength(1);
    expect(urls[0].endsWith(`/api/news/${slug}`)).toBe(true);
  });
});
