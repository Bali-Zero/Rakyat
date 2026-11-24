/**
 * The same rule on the articles the site really serves: no translation of an
 * English article kept out of search is read as indexable.
 *
 * Nothing here names an article, so the corpus can change freely. The invented
 * cases, and the proof that the rule bites, are in
 * `articles.translation-noindex.test.ts`.
 */
import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
const fetchSpy = vi.fn(() => {
  throw new Error("fetch must not be called");
});
vi.stubGlobal("fetch", fetchSpy);

const { getArticleByLocale, getArticleBySlug } =
  await import("@/lib/blog/articles");

const ARTICLES = path.join(process.cwd(), "src/content/articles");
const TRANSLATION = /^(.+)\.(id|it|ru|fr)\.mdx$/;

function frontmatterOf(file: string): Record<string, unknown> {
  try {
    return matter(fs.readFileSync(file, "utf8"), {}).data;
  } catch {
    return {};
  }
}

/** Every translation file whose English article says `noIndex`. */
function translationsOfArticlesKeptOutOfSearch(): [string, string, string][] {
  const found: [string, string, string][] = [];
  for (const folder of fs.readdirSync(ARTICLES)) {
    const dir = path.join(ARTICLES, folder);
    if (!fs.statSync(dir).isDirectory()) continue;
    const names = new Set(fs.readdirSync(dir));
    for (const name of names) {
      const translation = TRANSLATION.exec(name);
      if (!translation || !names.has(`${translation[1]}.mdx`)) continue;
      const english = frontmatterOf(path.join(dir, `${translation[1]}.mdx`));
      if (english.noIndex) found.push([folder, translation[1], translation[2]]);
    }
  }
  return found;
}

describe("the articles the site serves", () => {
  it("no translation of an article kept out of search is read as indexable", async () => {
    const indexable: string[] = [];

    for (const [
      folder,
      slug,
      locale,
    ] of translationsOfArticlesKeptOutOfSearch()) {
      const english = await getArticleBySlug(folder, slug);
      // The page asks with the category of its address; the folder is asked too.
      for (const category of new Set([folder, english?.category ?? folder])) {
        const article = await getArticleByLocale(category, slug, locale);
        // A hidden article is not served at all, which is stronger.
        if (article && !article.noIndex) {
          indexable.push(`${category}/${slug}?lang=${locale}`);
        }
      }
    }

    expect(indexable).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
