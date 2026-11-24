import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

// The category filter of the article list, read through the real list
// function and the /news page: an alias lists what its category lists from
// one cache entry, and a name that is no category creates no cache entry and
// calls no backend. The tree is a fixture under a temp cwd; the backend
// answers with an error, so only the local files list.

// A cache that behaves like unstable_cache: one stored entry per distinct
// serialized argument list, and the wrapped computation runs on a miss only.
const computations: string[] = [];
vi.mock("next/cache", () => ({
  unstable_cache: <A extends unknown[], R>(fn: (...a: A) => Promise<R>) => {
    const store = new Map<string, Promise<R>>();
    return (...args: A) => {
      const key = JSON.stringify(args);
      if (!store.has(key)) {
        computations.push(key);
        store.set(key, fn(...args));
      }
      return store.get(key)!;
    };
  },
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
vi.mock("@/app/(blog)/NewsPageClient", () => ({
  default: () => null,
}));

const root = mkdtempSync(join(tmpdir(), "articles-category-alias-"));

function writeArticle(folder: string, slug: string, title: string) {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${slug}.mdx`),
    `---\ntitle: "${title}"\ncategory: ${folder}\npublishedAt: "2026-03-01"\n---\n\nBody of ${slug}, long enough to be a real paragraph of text for the card.\n`,
  );
}

const backendCategories: (string | null)[] = [];
const fetchSpy = vi.fn(async (url: string | URL) => {
  backendCategories.push(new URL(String(url)).searchParams.get("category"));
  return { ok: false } as Response;
});

let getAllArticles: typeof import("./articles").getAllArticles;
let NewsRoute: typeof import("@/app/(blog)/news/page").default;

beforeAll(async () => {
  writeArticle("immigration", "visa-file", "Visa file");
  writeArticle("business", "business-file", "Business file");
  vi.spyOn(process, "cwd").mockReturnValue(root);
  vi.resetModules();
  ({ getAllArticles } = await import("./articles"));
  ({ default: NewsRoute } = await import("@/app/(blog)/news/page"));
});

beforeEach(() => {
  vi.stubGlobal("fetch", fetchSpy);
});

afterAll(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  rmSync(root, { recursive: true, force: true });
});

function reset() {
  computations.length = 0;
  backendCategories.length = 0;
}

async function newsPageArticles(category: string) {
  const page = await NewsRoute({ searchParams: Promise.resolve({ category }) });
  return (page.props as { articles: { slug: string }[] }).articles;
}

describe("getAllArticles category filter", () => {
  it("lists an alias like its category, from one cache entry", async () => {
    reset();
    const alias = await getAllArticles({ category: "immigration" });
    const canonical = await getAllArticles({ category: "visas" });
    expect(alias.articles.map((a) => a.slug)).toEqual(["visa-file"]);
    expect(canonical).toEqual(alias);
    expect(alias.articles[0].category).toBe("visas");
    expect(computations).toHaveLength(1);
    expect(backendCategories.sort()).toEqual(["immigration", "visas"]);
  });

  it("answers a name that is no category with an empty list and asks nothing", async () => {
    reset();
    const names = [
      ...Array.from({ length: 100 }, (_, i) => `anything-${i}`),
      "Visas",
      " visas",
      "constructor",
      "toString",
      "__proto__",
      "hasOwnProperty",
    ];
    for (const category of names) {
      expect(await getAllArticles({ category, limit: 12 })).toEqual({
        articles: [],
        total: 0,
      });
    }
    expect(computations).toEqual([]);
    expect(backendCategories).toEqual([]);
  });

  it("keeps an unfiltered list unchanged", async () => {
    reset();
    const all = await getAllArticles({});
    expect(all.articles.map((a) => a.slug).sort()).toEqual([
      "business-file",
      "visa-file",
    ]);
  });
});

describe("/news?category=", () => {
  it("lists an alias like its category", async () => {
    expect((await newsPageArticles("immigration")).map((a) => a.slug)).toEqual([
      "visa-file",
    ]);
  });

  it("creates no cache entry and calls no backend for unknown names", async () => {
    await newsPageArticles("visas");
    reset();
    for (let i = 0; i < 100; i++) {
      expect(await newsPageArticles(`news-anything-${i}`)).toEqual([]);
    }
    expect(computations).toEqual([]);
    expect(backendCategories).toEqual([]);
  });
});
