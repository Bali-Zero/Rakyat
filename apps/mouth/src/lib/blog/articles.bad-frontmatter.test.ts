import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

// One article file that cannot be built is skipped alone: the listing does not
// throw, the other files list, and the skip is logged once by path and error
// name. The tree is a fixture under a temp cwd; the backend is offline.

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

const root = mkdtempSync(join(tmpdir(), "articles-bad-frontmatter-"));

function writeArticle(folder: string, slug: string, title: string, extra = "") {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${slug}.mdx`),
    `---\ntitle: "${title}"\ncategory: ${folder}\npublishedAt: "2026-03-01"\n${extra}---\n\nBody of ${slug}, long enough to be a real paragraph of text for the card.\n`,
  );
}

let getAllArticles: typeof import("./articles").getAllArticles;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeAll(async () => {
  writeArticle("business", "good-one", "Good one");
  writeArticle("business", "good-two", "Good two");
  writeArticle("tax", "number-excerpt", "Number excerpt", "excerpt: 2026\n");
  vi.spyOn(process, "cwd").mockReturnValue(root);
  vi.resetModules();
  ({ getAllArticles } = await import("./articles"));
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  errorSpy.mockRestore();
});

afterAll(() => {
  vi.restoreAllMocks();
  rmSync(root, { recursive: true, force: true });
});

describe("a file that cannot be built does not abort the listing", () => {
  it("lists the good files, skips the bad one and logs once by path and error name", async () => {
    const { articles } = await getAllArticles({ limit: 500 });
    const slugs = articles.map((a) => a.slug);
    expect(slugs).toContain("good-one");
    expect(slugs).toContain("good-two");
    expect(slugs).not.toContain("number-excerpt");
    expect(errorSpy).toHaveBeenCalledTimes(1);
    const line = String(errorSpy.mock.calls[0][0]);
    expect(line).toContain("tax/number-excerpt.mdx");
    expect(line).toContain("TypeError");
    expect(line).not.toContain("2026");
  });

  it("lists good files as before and logs nothing for them", async () => {
    const { articles } = await getAllArticles({
      category: "business",
      limit: 500,
    });
    expect(articles.map((a) => a.slug).sort()).toEqual([
      "good-one",
      "good-two",
    ]);
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
