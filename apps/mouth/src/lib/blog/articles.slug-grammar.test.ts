import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
// The backend answers "not found": a slug that gets past the MDX lookup falls back to it
// (backend news_items slugs may have other shapes), and the call is observable.
const fetchSpy = vi.fn(async () => ({ ok: false }) as Response);
vi.stubGlobal("fetch", fetchSpy);

const { getArticleBySlug, getAvailableLocales, getArticleByLocale } =
  await import("@/lib/blog/articles");
const { isPublishedFrontmatter } = await import("@/lib/blog/publication");

const ROOT = path.join(process.cwd(), "src/content/articles");
const LOCALE_FILE = /\.(id|it|ru|fr)\.mdx$/;
const GRAMMAR = /^[a-z0-9][a-z0-9-]*$/;

// Every real English corpus file (a locale file or a sync conflict is not one).
const corpus: { folder: string; slug: string }[] = [];
for (const folder of fs.readdirSync(ROOT)) {
  const dir = path.join(ROOT, folder);
  if (!fs.statSync(dir).isDirectory()) continue;
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith(".mdx") || LOCALE_FILE.test(file)) continue;
    if (file.includes(".sync-conflict-")) continue;
    corpus.push({ folder, slug: file.slice(0, -".mdx".length) });
  }
}

// Two real, published articles in different folders; B is the traversal target.
const A = {
  cat: "visas",
  folder: "immigration",
  slug: "au-pair-visa-indonesia",
};
const B = { folder: "business", slug: "accounting-software-indonesia" };
const titleOf = (folder: string, slug: string) =>
  String(
    matter(fs.readFileSync(path.join(ROOT, folder, `${slug}.mdx`), "utf8")).data
      .title,
  );

afterEach(() => fetchSpy.mockClear());

describe("article slug grammar", () => {
  it("every real corpus slug matches the grammar", () => {
    expect(corpus.length).toBeGreaterThan(800);
    expect(corpus.filter((c) => !GRAMMAR.test(c.slug))).toEqual([]);
  });

  it("every published real article still resolves", async () => {
    let published = 0;
    for (const { folder, slug } of corpus) {
      const raw = fs.readFileSync(
        path.join(ROOT, folder, `${slug}.mdx`),
        "utf8",
      );
      let data: Record<string, unknown>;
      try {
        data = matter(raw).data;
      } catch {
        continue; // unreadable files are hidden by design
      }
      if (!isPublishedFrontmatter(data)) continue;
      published++;
      const a = await getArticleBySlug(folder, slug);
      expect(a, `${folder}/${slug}`).not.toBeNull();
    }
    console.log(
      `corpus English files: ${corpus.length}, published resolved: ${published}`,
    );
    expect(published).toBeGreaterThan(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("a real article resolves through its category and keeps its locales", async () => {
    const a = await getArticleBySlug(A.cat, A.slug);
    expect(a?.title).toBe(titleOf(A.folder, A.slug));
    expect(getAvailableLocales(A.cat, A.slug)).toContain("id");
  });

  const hostile = [
    `../${B.folder}/${B.slug}`,
    `..%2F${B.folder}%2F${B.slug}`,
    `../../immigration/../${B.folder}/${B.slug}`,
    `..\\${B.folder}\\${B.slug}`,
    `${B.slug}/../${B.slug}`,
    `${B.slug}\0`,
    "",
  ];
  it.each(hostile)("%j never returns another article", async (slug) => {
    const r = await getArticleBySlug(A.cat, slug);
    expect(r?.title).not.toBe(titleOf(B.folder, B.slug));
    // outside the grammar, so no MDX: the backend fallback is what answers, here with "not found"
    expect(r).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("a hostile slug builds no locale path", async () => {
    const slug = `../${B.folder}/${B.slug}`;
    expect(getAvailableLocales(A.cat, slug)).toEqual(["en"]);
    expect(
      (await getArticleByLocale(A.cat, slug, "id"))?.title,
    ).toBeUndefined();
  });
});
