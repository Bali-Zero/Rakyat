/**
 * A translation of an article kept out of search is kept out too.
 *
 * The English file governs its translations. An editor marks the English
 * article `noIndex`; its translations were written before, or by a job that
 * does not copy the flag, and were served `index, follow` under `?lang=`.
 *
 * Invented articles in a directory of their own: the library reads
 * `<cwd>/src/content/articles`, so the working directory is set before it loads.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
// Every case is answered from the files below: a request to the backend would
// be another path, so fetch throws and is asserted unused.
const fetchSpy = vi.fn(() => {
  throw new Error("fetch must not be called");
});
vi.stubGlobal("fetch", fetchSpy);

const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "translation-noindex-"));
const FOLDER = path.join(ROOT, "src/content/articles/lifestyle");
fs.mkdirSync(FOLDER, { recursive: true });
vi.spyOn(process, "cwd").mockReturnValue(ROOT);

const { getArticleByLocale, getArticleBySlug } =
  await import("@/lib/blog/articles");
const { generateArticleMetadata } = await import("@/lib/blog/metadata");

afterAll(() => {
  fs.rmSync(ROOT, { recursive: true, force: true });
});

type Flag = boolean | undefined;
const LOCALES = ["id", "it", "ru", "fr"] as const;
// The page asks with the category of its address; the files live in the folder.
const AS_THE_PAGE_ASKS = ["living", "lifestyle"] as const;

let made = 0;

function file(
  slug: string,
  locale: string | null,
  noIndex: Flag,
  status?: string,
) {
  const lines = [
    "---",
    `title: "${locale ? `Judul ${locale}` : "A title"} ${slug}"`,
    `slug: "${slug}"`,
    'category: "lifestyle"',
    'publishedAt: "2026-01-01"',
    ...(noIndex === undefined ? [] : [`noIndex: ${noIndex}`]),
    ...(status ? [`status: "${status}"`] : []),
    ...(locale ? [`locale: "${locale}"`] : []),
    "---",
    "",
    "An invented body.",
    "",
  ];
  const name = locale ? `${slug}.${locale}.mdx` : `${slug}.mdx`;
  fs.writeFileSync(path.join(FOLDER, name), lines.join("\n"));
}

/** One English article and one translation of it, each with its own flag. */
function anArticle(english: Flag, translation: Flag, locale: string): string {
  made += 1;
  const slug = `an-invented-article-${made}`;
  file(slug, null, english);
  file(slug, locale, translation);
  return slug;
}

describe("the English flag reaches the translation", () => {
  const ROWS: [string, Flag, Flag, boolean][] = [
    ["kept out, and the translation says nothing", true, undefined, true],
    ["kept out, and the translation says indexed", true, false, true],
    ["kept out, and the translation says kept out", true, true, true],
    ["indexed, and the translation says nothing", undefined, undefined, false],
    ["indexed in so many words", false, undefined, false],
    ["indexed, and the translation says kept out", undefined, true, true],
    ["indexed, and the translation says indexed", false, false, false],
  ];

  describe.each(LOCALES)("in %s", (locale) => {
    it.each(ROWS)(
      "English %s",
      async (_label, english, translation, expected) => {
        const slug = anArticle(english, translation, locale);

        for (const category of AS_THE_PAGE_ASKS) {
          const article = await getArticleByLocale(category, slug, locale);

          expect(article?.title).toBe(`Judul ${locale} ${slug}`);
          expect(article?.locale).toBe(locale);
          expect(article?.noIndex).toBe(expected);
        }
        expect(fetchSpy).not.toHaveBeenCalled();
      },
    );
  });
});

describe("what the page then tells a crawler", () => {
  it.each(LOCALES)(
    "noindex, nofollow for the %s translation of an article kept out of search",
    async (locale) => {
      const slug = anArticle(true, undefined, locale);

      const article = await getArticleByLocale("living", slug, locale);

      expect(generateArticleMetadata(article!).robots).toEqual({
        index: false,
        follow: false,
      });
    },
  );

  it("index, follow for the translation of an indexed article", async () => {
    const slug = anArticle(undefined, undefined, "it");

    const article = await getArticleByLocale("living", slug, "it");

    expect(generateArticleMetadata(article!).robots).toMatchObject({
      index: true,
      follow: true,
    });
  });
});

describe("what does not change", () => {
  it("the English article reads its own flag", async () => {
    const keptOut = anArticle(true, undefined, "it");
    const indexed = anArticle(undefined, true, "it");

    expect((await getArticleBySlug("living", keptOut))?.noIndex).toBe(true);
    expect((await getArticleBySlug("living", indexed))?.noIndex).toBe(false);
    expect((await getArticleByLocale("living", keptOut, "en"))?.noIndex).toBe(
      true,
    );
  });

  it("a language with no file gets the English article, with its flag", async () => {
    const slug = anArticle(true, undefined, "it");

    const article = await getArticleByLocale("living", slug, "fr");

    expect(article?.title).toBe(`A title ${slug}`);
    expect(article?.noIndex).toBe(true);
  });

  it("an unpublished translation gets the English article, with its flag", async () => {
    made += 1;
    const slug = `an-invented-article-${made}`;
    file(slug, null, true);
    file(slug, "it", undefined, "draft");

    const article = await getArticleByLocale("living", slug, "it");

    expect(article?.title).toBe(`A title ${slug}`);
    expect(article?.noIndex).toBe(true);
  });

  it.each([
    ["says nothing", undefined, false],
    ["says kept out", true, true],
  ] as [string, Flag, boolean][])(
    "a translation with no English file keeps its own word: it %s",
    async (_label, own, expected) => {
      made += 1;
      const slug = `an-invented-article-${made}`;
      file(slug, "it", own);

      const article = await getArticleByLocale("living", slug, "it");

      expect(article?.title).toBe(`Judul it ${slug}`);
      expect(article?.noIndex).toBe(expected);
    },
  );

  it("a hidden English article still hides its translation", async () => {
    made += 1;
    const slug = `an-invented-article-${made}`;
    file(slug, null, true, "draft");
    file(slug, "it", undefined);

    expect(await getArticleByLocale("living", slug, "it")).toBeNull();
  });
});
