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
const {
  getArticleBySlug,
  getPinnedMdxArticles,
  getAllArticles,
  getArticleByLocale,
  getAvailableLocales,
  searchArticles,
} = await import("./articles");

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

describe("one publication rule for local MDX", () => {
  const mdx = (frontmatter: string, title = "Local Title") =>
    `---\ntitle: "${title}"\ncategory: business\npublishedAt: "2026-02-20"\n${frontmatter}---\n\nBody.\n`;

  function disk(files: Record<string, string>) {
    const names = Object.keys(files);
    const folders = new Set(names.map((name) => name.split("/")[0]));
    existsSyncMock.mockImplementation((p: unknown) => {
      const s = String(p);
      if (s.endsWith("src/content/articles")) return true;
      for (const folder of folders) {
        if (s.endsWith(`src/content/articles/${folder}`)) return true;
      }
      return names.some((name) => s.endsWith(`articles/${name}`));
    });
    statSyncMock.mockReturnValue({ isDirectory: () => true });
    readdirSyncMock.mockImplementation((p: unknown) => {
      const s = String(p);
      const folder = [...folders].find((f) => s.endsWith(`articles/${f}`));
      if (!folder) return [];
      return names
        .filter((name) => name.startsWith(folder + "/"))
        .map((name) => name.slice(folder.length + 1));
    });
    readFileSyncMock.mockImplementation((p: unknown) => {
      const s = String(p);
      const name = names.find((n) => s.endsWith(`articles/${n}`));
      if (!name) throw new Error("ENOENT " + s);
      return files[name];
    });
  }

  it("a draft MDX hides the article, and no approved backend row stands in", async () => {
    disk({ [`business/${SLUG}.mdx`]: mdx("status: draft\n") });
    mockFetchOnce({ success: true, data: BACKEND_ITEM });
    expect(await getArticleBySlug(CATEGORY, SLUG)).toBeNull();
  });

  it("counts only an absent status or exactly published", async () => {
    mockFetchOnce({ success: false }, false);
    for (const line of [
      "status:\n",
      "status: false\n",
      'status: ""\n',
      "status: Published\n",
      "status: review\n",
    ]) {
      disk({ [`business/${SLUG}.mdx`]: mdx(line) });
      expect(await getArticleBySlug(CATEGORY, SLUG)).toBeNull();
    }
    for (const line of ["", "status: published\n"]) {
      disk({ [`business/${SLUG}.mdx`]: mdx(line) });
      expect((await getArticleBySlug(CATEGORY, SLUG))?.title).toBe(
        "Local Title",
      );
    }
  });

  it("keeps a broken frontmatter hidden on every read", async () => {
    disk({
      [`business/${SLUG}.mdx`]: "---\nstatus: draft\nbroken: [\n---\nBody\n",
    });
    mockFetchOnce({ success: true, data: BACKEND_ITEM });
    expect(await getArticleBySlug(CATEGORY, SLUG)).toBeNull();
    expect(await getArticleBySlug(CATEGORY, SLUG)).toBeNull();
  });

  it("skips a draft translation and never lists it", async () => {
    mockFetchOnce({ success: false }, false);
    disk({
      [`business/${SLUG}.mdx`]: mdx("", "English Title"),
      [`business/${SLUG}.it.mdx`]: mdx("status: draft\n", "Titolo"),
    });
    expect((await getArticleByLocale(CATEGORY, SLUG, "it"))?.title).toBe(
      "English Title",
    );
    expect(getAvailableLocales(CATEGORY, SLUG)).toEqual(["en"]);
    disk({
      [`business/${SLUG}.mdx`]: mdx("", "English Title"),
      [`business/${SLUG}.it.mdx`]: mdx("", "Titolo"),
    });
    expect((await getArticleByLocale(CATEGORY, SLUG, "it"))?.title).toBe(
      "Titolo",
    );
    expect(getAvailableLocales(CATEGORY, SLUG)).toEqual(["en", "it"]);
  });

  it("hides every translation of a hidden article", async () => {
    mockFetchOnce({ success: false }, false);
    disk({
      [`business/${SLUG}.mdx`]: mdx("status: draft\n"),
      [`business/${SLUG}.it.mdx`]: mdx("", "Titolo"),
    });
    expect(await getArticleByLocale(CATEGORY, SLUG, "it")).toBeNull();
  });

  it("lists and finds only published articles", async () => {
    disk({
      "business/draft-a.mdx": mdx("status: draft\n"),
      "business/live-b.mdx": mdx(""),
    });
    const rows = [
      BACKEND_ITEM,
      { ...BACKEND_ITEM, id: "news-43", slug: "draft-a" },
    ];
    mockFetchOnce({ success: true, data: rows, total: 2 });
    const { articles, total } = await getAllArticles({});
    expect(articles.map((a) => a.slug).sort()).toEqual([SLUG, "live-b"]);
    expect(total).toBe(2);
    const found = await searchArticles("KBLI");
    expect(found.map((a) => a.slug)).toEqual([SLUG]);
  });

  it("reads an Insight pin from its one published, indexed MDX file and nothing else", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: BACKEND_ITEM }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    disk({ [`business_regulations/${SLUG}.mdx`]: mdx("") });
    const [pin] = await getPinnedMdxArticles([SLUG]);
    expect([pin.category, pin.slug, pin.title]).toEqual([
      "business",
      SLUG,
      "Local Title",
    ]);
    for (const line of [
      "status: draft\n",
      "status: review\n",
      "status: false\n",
      "status: Published\n",
      "noIndex: true\n",
    ]) {
      disk({ [`business/${SLUG}.mdx`]: mdx(line) });
      expect(await getPinnedMdxArticles([SLUG])).toEqual([]);
    }
    disk({ [`business/${SLUG}.mdx`]: "---\nbroken: [\n---\nBody\n" });
    expect(await getPinnedMdxArticles([SLUG])).toEqual([]);
    expect(await getPinnedMdxArticles([SLUG])).toEqual([]);
    disk({});
    expect(await getPinnedMdxArticles([SLUG])).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("refuses an Insight pin with two MDX files or an unsafe slug, and links by the file's folder", async () => {
    disk({
      [`business/${SLUG}.mdx`]: mdx(""),
      [`lifestyle/${SLUG}.mdx`]: mdx(""),
    });
    expect(await getPinnedMdxArticles([SLUG])).toEqual([]);
    disk({ [`business/${SLUG}.mdx`]: mdx("") });
    expect(
      await getPinnedMdxArticles([
        "../business/" + SLUG,
        SLUG.toUpperCase(),
        "",
        undefined,
      ]),
    ).toEqual([]);
    disk({
      [`immigration/a-pin.mdx`]: mdx('slug: "other"\n', "A"),
      [`tax/b-pin.mdx`]: mdx("", "B"),
    });
    const pins = await getPinnedMdxArticles(["b-pin", "a-pin"]);
    expect(pins.map((p) => p.category + "/" + p.slug + "|" + p.title)).toEqual([
      "taxes/b-pin|B",
      "visas/a-pin|A",
    ]);
  });
});
