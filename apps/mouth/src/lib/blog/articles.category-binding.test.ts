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

// The backend fallback of getArticleBySlug answers only for the row's own
// category, and never for a slug that has a reviewed article file in any
// category folder. The tree is a fixture under a temp cwd; the backend is a
// fetch stub.

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

const root = mkdtempSync(join(tmpdir(), "articles-category-binding-"));

function writeArticle(folder: string, slug: string, frontmatter: string) {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${slug}.mdx`),
    `---\n${frontmatter}\n---\n\nBody of the reviewed article ${slug}, long enough to be a real paragraph of text.\n`,
  );
}

function backendRow(slug: string, category: string) {
  return {
    id: `row-${slug}`,
    title: "Raw staging title",
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

function stubBackendRow(row: ReturnType<typeof backendRow>) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: row }),
    }),
  );
}

let getArticleBySlug: typeof import("./articles").getArticleBySlug;

beforeAll(async () => {
  writeArticle(
    "immigration",
    "visa-file-article",
    'title: "Reviewed visa article"\ncategory: immigration\npublishedAt: "2026-03-01"',
  );
  vi.spyOn(process, "cwd").mockReturnValue(root);
  vi.resetModules();
  ({ getArticleBySlug } = await import("./articles"));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

afterAll(() => {
  vi.restoreAllMocks();
  rmSync(root, { recursive: true, force: true });
});

describe("getArticleBySlug backend fallback is bound to the row's category", () => {
  it("answers a row with no file under its own category only", async () => {
    stubBackendRow(backendRow("row-only-article", "tax"));

    const wrong = await getArticleBySlug("business", "row-only-article");
    expect(wrong).toBeNull();

    const right = await getArticleBySlug("taxes", "row-only-article");
    expect(right?.id).toBe("row-row-only-article");
    expect(right?.category).toBe("taxes");
  });

  it("answers a row with no file under an alias of its own category", async () => {
    stubBackendRow(backendRow("row-only-article", "tax"));

    const viaAlias = await getArticleBySlug("tax", "row-only-article");
    expect(viaAlias?.category).toBe("taxes");
  });

  it("never serves the row under another category when the file lives under one", async () => {
    stubBackendRow(backendRow("visa-file-article", "immigration"));

    const wrong = await getArticleBySlug("taxes", "visa-file-article");
    expect(wrong).toBeNull();
  });

  it("still returns the file for its own category and for an alias of it", async () => {
    stubBackendRow(backendRow("visa-file-article", "immigration"));

    const own = await getArticleBySlug("visas", "visa-file-article");
    expect(own?.title).toBe("Reviewed visa article");

    const viaAlias = await getArticleBySlug("immigration", "visa-file-article");
    expect(viaAlias?.title).toBe("Reviewed visa article");
    expect(viaAlias?.category).toBe("visas");
  });
});
