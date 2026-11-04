import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Regression guard for the "backend row wins over composed MDX" bug
// (2026-09-05). getArticleBySlug used to try the backend `/api/news/<slug>`
// FIRST and return unconditionally on a hit. For News Room articles both a
// `news_items` row (raw staging body: "Facts, Bali Zero Take, In Practice,
// Next Steps") AND a composed local MDX file (TL;DR <InfoCard>, SEO
// frontmatter, aiOptimization, translations) can exist for the same slug —
// the MDX is the reviewed, published artifact and must win. See
// apps/mouth/src/lib/blog/articles.ts `getArticleBySlug`.

const existsSyncMock = vi.fn();
const readFileSyncMock = vi.fn();
const readdirSyncMock = vi.fn();
const statSyncMock = vi.fn();

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

vi.mock("fs", () => ({
  default: {
    existsSync: (...args: unknown[]) =>
      (existsSyncMock as (...a: unknown[]) => unknown)(...args),
    readFileSync: (...args: unknown[]) =>
      (readFileSyncMock as (...a: unknown[]) => unknown)(...args),
    readdirSync: (...args: unknown[]) =>
      (readdirSyncMock as (...a: unknown[]) => unknown)(...args),
    statSync: (...args: unknown[]) =>
      (statSyncMock as (...a: unknown[]) => unknown)(...args),
  },
  existsSync: (...args: unknown[]) =>
    (existsSyncMock as (...a: unknown[]) => unknown)(...args),
  readFileSync: (...args: unknown[]) =>
    (readFileSyncMock as (...a: unknown[]) => unknown)(...args),
  readdirSync: (...args: unknown[]) =>
    (readdirSyncMock as (...a: unknown[]) => unknown)(...args),
  statSync: (...args: unknown[]) =>
    (statSyncMock as (...a: unknown[]) => unknown)(...args),
}));

// Imported AFTER the vi.mock("fs") factory above so the module under test
// picks up the mocked fs.
const { getArticleBySlug, getAllArticles } = await import("./articles");

const SLUG = "kbli-2025-explained";
const CATEGORY = "business";

// The composed, reviewed MDX artifact for this slug — what a human editor
// approved: TL;DR InfoCard first, then the body, plus SEO/aiOptimization
// frontmatter that the raw backend row never carries.
const MDX_CONTENT = `---
title: "KBLI 2025 Explained"
seoTitle: "KBLI 2025 Explained | Bali Zero"
seoDescription: "The complete guide to the KBLI 2025 code overhaul."
category: business
publishedAt: "2026-02-20"
aiOptimization:
  answerSnippet: "KBLI 2025 unifies legacy 5-digit codes into a single risk-based system."
---

<InfoCard>TL;DR: KBLI 2025 replaces the old code book.</InfoCard>

## Facts

This is the composed, editor-reviewed MDX body.
`;

// The raw staging body a `news_items` row would carry for the SAME slug —
// what the intel pipeline wrote before editorial composition.
const BACKEND_ITEM = {
  id: "news-42",
  title: "KBLI 2025 Explained",
  slug: SLUG,
  summary: "Raw staging summary.",
  content:
    "## Facts\n\nRaw staging body from the scraper.\n\n## Bali Zero Take\n\n...",
  source: "scraper",
  source_url: null,
  category: "business",
  priority: "normal",
  status: "approved",
  image_url: null,
  view_count: 0,
  published_at: "2026-02-20T00:00:00Z",
  created_at: "2026-02-20T00:00:00Z",
  ai_summary: null,
  ai_tags: null,
};

function mockFetchOnce(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok,
      json: async () => body,
    }),
  );
}

beforeEach(() => {
  existsSyncMock.mockReset();
  readFileSyncMock.mockReset();
  readdirSyncMock.mockReset();
  statSyncMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getArticleBySlug (MDX-first)", () => {
  it("returns the composed MDX article when both a local MDX and a backend row exist", async () => {
    existsSyncMock.mockImplementation((p: unknown) =>
      String(p).endsWith(`business/${SLUG}.mdx`),
    );
    readFileSyncMock.mockReturnValue(MDX_CONTENT);
    mockFetchOnce({ success: true, data: BACKEND_ITEM });

    const article = await getArticleBySlug(CATEGORY, SLUG);

    expect(article).not.toBeNull();
    expect(article!.seoTitle).toBe("KBLI 2025 Explained | Bali Zero");
    expect(article!.aiOptimization).toEqual({
      answerSnippet:
        "KBLI 2025 unifies legacy 5-digit codes into a single risk-based system.",
    });
    expect(article!.content.trim().startsWith("<InfoCard>")).toBe(true);
    expect(article!.content).not.toContain(
      "Raw staging body from the scraper.",
    );
    // MDX short-circuits before the backend is ever consulted.
    expect(fetch).not.toHaveBeenCalled();
  });

  it("falls back to the backend article when no local MDX exists (regression guard)", async () => {
    existsSyncMock.mockReturnValue(false);
    mockFetchOnce({ success: true, data: BACKEND_ITEM });

    const article = await getArticleBySlug(CATEGORY, SLUG);

    expect(article).not.toBeNull();
    expect(article!.id).toBe(BACKEND_ITEM.id);
    expect(article!.content).toBe(BACKEND_ITEM.content);
    expect(article!.seoTitle).toBeUndefined();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("renders a backend article whose cover is only a storage key", async () => {
    existsSyncMock.mockReturnValue(false);
    mockFetchOnce({
      success: true,
      data: { ...BACKEND_ITEM, image_url: "covers/news-example.jpg" },
    });

    const article = await getArticleBySlug(CATEGORY, SLUG);

    expect(article?.coverImage).toBe("/static/blog/oss-guide.jpg");
  });

  it("returns null when neither a local MDX nor a backend row exists", async () => {
    existsSyncMock.mockReturnValue(false);
    mockFetchOnce({ success: false }, true);

    const article = await getArticleBySlug(CATEGORY, SLUG);

    expect(article).toBeNull();
  });

  it("getAllArticles propagates frontmatter updatedAt and never leaks Date.now() when publishedAt is absent", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-26T23:59:59.000Z"));
      mockFetchOnce({ success: false }, false);
      existsSyncMock.mockImplementation((p: unknown) => {
        const s = String(p);
        return (
          s.endsWith("src/content/articles") ||
          s.endsWith("src/content/articles/business") ||
          s.endsWith("business/updated-guide.mdx") ||
          s.endsWith("business/dateless-guide.mdx")
        );
      });
      statSyncMock.mockReturnValue({ isDirectory: () => true });
      readdirSyncMock.mockReturnValue([
        "updated-guide.mdx",
        "dateless-guide.mdx",
      ]);
      readFileSyncMock.mockImplementation((p: unknown) => {
        if (String(p).endsWith("updated-guide.mdx")) {
          return `---\ntitle: "Updated Guide"\ncategory: business\npublishedAt: "2026-02-20"\nupdatedAt: "2026-04-15"\n---\n\nBody text.\n`;
        }
        return `---\ntitle: "Dateless Guide"\ncategory: business\n---\n\nBody text.\n`;
      });

      const { articles } = await getAllArticles({ category: "business" });
      const updated = articles.find((a) => a.slug === "updated-guide");
      const dateless = articles.find((a) => a.slug === "dateless-guide");

      expect(updated?.publishedAt.toISOString()).toBe(
        "2026-02-20T00:00:00.000Z",
      );
      expect(updated?.updatedAt?.toISOString()).toBe(
        "2026-04-15T00:00:00.000Z",
      );
      expect(dateless).toBeDefined();
      expect(Number.isNaN(dateless!.publishedAt.getTime())).toBe(true);
      expect(dateless!.updatedAt).toBeUndefined();

      // Detail loader keeps Date.now() fallback so dateless .??.mdx locale pages never render 1970-01-01
      const detailDateless = await getArticleBySlug(
        "business",
        "dateless-guide",
      );
      expect(detailDateless?.createdAt.toISOString()).toBe(
        "2026-10-26T23:59:59.000Z",
      );
    } finally {
      vi.useRealTimers();
    }
  });
});
