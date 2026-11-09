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

const { getArticleBySlug } = await import("@/lib/blog/articles");

afterEach(() => {
  urls.length = 0;
});

// A slug no corpus file carries, so the backend fallback is what answers.
describe("backend news URL", () => {
  // The encoding is defence in depth: these slugs are refused before any request.
  it.each(["../x", "a?b=c#d"])("%j is refused, never encoded", async (slug) => {
    await getArticleBySlug("visas", slug);
    expect(urls).toHaveLength(0);
  });

  it("leaves a normal slug byte for byte as it was", async () => {
    await getArticleBySlug("visas", "no-such-article-for-this-test");
    // the URL the old code built: `${BACKEND_URL}/api/news/${slug}`
    const base = urls[0].slice(0, urls[0].indexOf("/api/news/"));
    expect(urls[0]).toBe(`${base}/api/news/no-such-article-for-this-test`);
    if (!process.env.BACKEND_RAG_URL && !process.env.NEXT_PUBLIC_BACKEND_URL) {
      expect(urls[0]).toBe(
        "https://nuzantara-rag.fly.dev/api/news/no-such-article-for-this-test",
      );
    }
  });
});
