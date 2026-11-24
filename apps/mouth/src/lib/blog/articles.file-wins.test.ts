import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

// A slug that has a reviewed article file, in any category folder, is never
// listed from the backend news row: the file loop decides (its category, draft,
// noIndex). The tree is a fixture under a temp cwd; the backend is a fetch stub.

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

const root = mkdtempSync(join(tmpdir(), "articles-file-wins-"));

function writeArticle(folder: string, slug: string, frontmatter: string) {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${slug}.mdx`),
    `---\n${frontmatter}\n---\n\nBody of the reviewed article ${slug}, long enough to be a real paragraph of text.\n`,
  );
}

function backendRow(slug: string, category: string, title: string) {
  return {
    id: `row-${slug}`,
    title,
    slug,
    summary: "Raw staging summary.",
    content: "Raw staging body.",
    source: "scraper",
    source_url: null,
    category,
    priority: "normal",
    status: "approved",
    image_url: null,
    view_count: 0,
    published_at: "2026-03-01T00:00:00Z",
    created_at: "2026-03-01T00:00:00Z",
    ai_summary: null,
    ai_tags: null,
  };
}

function stubBackend(rows: ReturnType<typeof backendRow>[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: rows, total: rows.length }),
    }),
  );
}

let getAllArticles: typeof import("./articles").getAllArticles;
let searchArticles: typeof import("./articles").searchArticles;

beforeAll(async () => {
  writeArticle(
    "lifestyle",
    "pulled-from-index",
    'title: "Reviewed pulled article"\ncategory: lifestyle\npublishedAt: "2026-03-01"\nnoIndex: true',
  );
  writeArticle(
    "tax",
    "filed-under-tax",
    'title: "Reviewed tax article"\ncategory: tax\npublishedAt: "2026-03-02"',
  );
  writeArticle(
    "business",
    "file-only-article",
    'title: "Reviewed file only"\ncategory: business\npublishedAt: "2026-03-03"',
  );
  vi.spyOn(process, "cwd").mockReturnValue(root);
  vi.resetModules();
  ({ getAllArticles, searchArticles } = await import("./articles"));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

afterAll(() => {
  vi.restoreAllMocks();
  rmSync(root, { recursive: true, force: true });
});

describe("a reviewed article file wins over the backend row", () => {
  it("never lists a backend row whose slug has a noIndex file", async () => {
    stubBackend([
      backendRow("pulled-from-index", "lifestyle", "Raw pulled title"),
    ]);

    const { articles } = await getAllArticles({ limit: 500 });
    expect(articles.map((a) => a.slug)).not.toContain("pulled-from-index");

    const found = await searchArticles("Raw pulled title");
    expect(found.map((a) => a.slug)).not.toContain("pulled-from-index");
  });

  it("lists a slug whose file is in another category once, with the file's category and title", async () => {
    stubBackend([
      backendRow("filed-under-tax", "immigration", "Raw staging title"),
    ]);

    const { articles } = await getAllArticles({ limit: 500 });
    const listed = articles.filter((a) => a.slug === "filed-under-tax");
    expect(listed).toHaveLength(1);
    expect(listed[0].category).toBe("taxes");
    expect(listed[0].title).toBe("Reviewed tax article");

    const underRowCategory = await getAllArticles({
      category: "visas",
      limit: 500,
    });
    expect(underRowCategory.articles.map((a) => a.slug)).not.toContain(
      "filed-under-tax",
    );

    const found = await searchArticles("Raw staging title");
    expect(found.map((a) => a.slug)).not.toContain("filed-under-tax");
  });

  it("keeps listing a backend row with no file, and a file with no row, without duplicates", async () => {
    stubBackend([backendRow("row-only-article", "business", "Row only title")]);

    const { articles, total } = await getAllArticles({ limit: 500 });
    const slugs = articles.map((a) => a.slug);
    expect(slugs).toContain("row-only-article");
    expect(articles.find((a) => a.slug === "row-only-article")?.title).toBe(
      "Row only title",
    );
    expect(slugs).toContain("filed-under-tax");
    expect(articles.find((a) => a.slug === "file-only-article")?.title).toBe(
      "Reviewed file only",
    );
    expect(new Set(slugs).size).toBe(slugs.length);
    expect(total).toBe(slugs.length);

    const found = await searchArticles("Row only title");
    expect(found.map((a) => a.slug)).toEqual(["row-only-article"]);
  });
});
