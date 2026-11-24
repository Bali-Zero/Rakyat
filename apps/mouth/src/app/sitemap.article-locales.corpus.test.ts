import fs from "fs";
import path from "path";
import matter from "gray-matter";
import { expect, it, vi } from "vitest";
import { normalizeCategory } from "@/lib/blog/categories";
import { resolveSitemap } from "next/dist/build/webpack/loaders/metadata/resolve-route-data";

// Exercise sitemap.ts itself while deliberately keeping the expected corpus
// independent of articles.ts's locale helpers.  The backend is unavailable in
// this test by design: it proves the checked-in Magazine inventory, which is
// the only source capable of advertising translated MDX URLs.
vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
vi.mock("@/lib/kbli-data.server", () => ({
  getAllCodes: () => [],
  getSections: () => [],
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn(), warn: vi.fn() } }));
vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline in test")));

const ARTICLES = path.join(process.cwd(), "src/content/articles");
const BASE_URL = "https://balizero.com";
const LANGUAGES = ["id", "it", "ru", "fr"];

function eligibleFile(file: string, locale: string): boolean {
  try {
    const { data } = matter(fs.readFileSync(file, "utf8"), {});
    return (
      (data.status === undefined || data.status === "published") &&
      !data.noIndex &&
      (!data.locale || data.locale === locale)
    );
  } catch {
    return false;
  }
}

function expectedMagazineUrls() {
  const expected = new Map<string, Record<string, string>>();
  const articlePaths = new Set<string>();
  for (const folder of fs.readdirSync(ARTICLES, { withFileTypes: true })) {
    if (!folder.isDirectory()) continue;
    const dir = path.join(ARTICLES, folder.name);
    const names = new Set(fs.readdirSync(dir));
    for (const name of names) {
      if (!/^[a-z0-9][a-z0-9-]*\.mdx$/.test(name)) continue;
      const slug = name.slice(0, -4);
      const bare = `${BASE_URL}/${normalizeCategory(folder.name)}/${slug}`;
      articlePaths.add(bare);
      if (slug === "test-article" || !eligibleFile(path.join(dir, name), "en"))
        continue;
      const languages: Record<string, string> = { en: bare };
      for (const locale of LANGUAGES) {
        const filename = `${slug}.${locale}.mdx`;
        if (
          names.has(filename) &&
          eligibleFile(path.join(dir, filename), locale)
        ) {
          languages[locale] = `${bare}?lang=${locale}`;
        }
      }
      const alternates =
        Object.keys(languages).length > 1
          ? { ...languages, "x-default": bare }
          : {};
      for (const url of Object.values(languages)) expected.set(url, alternates);
    }
  }
  return { expected, articlePaths };
}

const { default: sitemap } = await import("./sitemap");

it("serializes exactly the indexable article URLs and their reciprocal alternate sets", async () => {
  const xml = resolveSitemap(await sitemap());
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  expect(doc.querySelector("parsererror")).toBeNull();
  const { expected, articlePaths } = expectedMagazineUrls();
  const seen = new Set<string>();
  const errors: string[] = [];
  const sitemapNS = "http://www.sitemaps.org/schemas/sitemap/0.9";
  const xhtmlNS = "http://www.w3.org/1999/xhtml";
  // A static NodeList avoids traversing a live collection for every row.
  for (const row of doc.querySelectorAll("url")) {
    const url =
      row.getElementsByTagNameNS(sitemapNS, "loc")[0]?.textContent || "";
    if (seen.has(url)) errors.push("duplicate location " + url);
    seen.add(url);
    const links = Array.from(row.getElementsByTagNameNS(xhtmlNS, "link"));
    if (!expected.has(url)) {
      if (articlePaths.has(url) || url.includes("?lang=") || links.length) {
        errors.push("unexpected article " + url);
      }
      continue;
    }
    const wanted = expected.get(url)!;
    const actual = links.map(
      (link) =>
        [link.getAttribute("hreflang"), link.getAttribute("href")] as const,
    );
    if (
      new Set(actual.map(([l]) => l)).size !== actual.length ||
      actual.length !== Object.keys(wanted).length ||
      actual.some(([l, href]) => !l || wanted[l] !== href)
    )
      errors.push("wrong alternates " + url);
  }
  expect(errors).toEqual([]);
  expect([...expected.keys()].filter((url) => !seen.has(url))).toEqual([]);
  expect([...expected.keys()].some((url) => url.includes("?lang="))).toBe(true);
}, 120_000);
