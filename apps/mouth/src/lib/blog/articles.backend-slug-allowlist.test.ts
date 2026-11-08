import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
const urls: string[] = [];
const fetchSpy = vi.fn(async (url: string) => {
  urls.push(String(url));
  return {
    ok: true,
    json: async () => ({
      success: true,
      data: {
        id: "i",
        title: "Backend title",
        slug: "s",
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
  } as unknown as Response;
});
vi.stubGlobal("fetch", fetchSpy);

const { getArticleBySlug } = await import("@/lib/blog/articles");

afterEach(() => {
  urls.length = 0;
  fetchSpy.mockClear();
});

// The backend proxy decodes the path again on its second hop, so only a slug the backend can
// have generated may go out. The stub answers 200 to everything: a null here is the guard's.
describe("backend news slug allowlist", () => {
  const refused = [
    "../news/target",
    "..%2Fnews%2Ftarget",
    "../x",
    "target?x=1",
    "target#frag",
    "a/b",
    "a%2Fb",
    "a b",
    "a\\b",
    ".",
    "..",
    "",
    "bаli-news", // Cyrillic "а"
    "ｂali-news", // fullwidth "ｂ"
  ];
  it.each(refused)("%j: null and zero fetch", async (slug) => {
    expect(await getArticleBySlug("visas", slug)).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([
    "bali-gets-direct-links",
    "news_20261108_120000_abc123",
    "-breaking-news-a1b2c3", // generate_news_slug on a title that starts with a symbol
    "a".repeat(201),
    "a".repeat(1000),
    "-",
    "_",
    "--__--",
    `${"how-to-get-a-kitas-in-bali-".repeat(5).slice(0, 126)}a1b2c3d`, // 133 chars, prod-shaped
  ])("%s resolves and the URL is exact", async (slug) => {
    const a = await getArticleBySlug("visas", slug);
    expect(a?.title).toBe("Backend title");
    expect(urls).toHaveLength(1);
    expect(urls[0].endsWith(`/api/news/${slug}`)).toBe(true);
    expect(new URL(urls[0]).pathname).toBe(`/api/news/${slug}`);
  });
});
