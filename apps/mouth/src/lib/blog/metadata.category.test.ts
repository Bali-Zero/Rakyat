/**
 * The page of a category carries a text written for it.
 *
 * The category page sets its own metadata, which overrides the layout's. Its
 * table had no entry for one real category, so that page was served the
 * generic sentence made from the category's name ("Trends Insights") while a
 * title and a description written for it sat in CATEGORY_SEO, unused.
 */
import { describe, expect, it } from "vitest";
import { CATEGORY_SEO } from "@/lib/blog/category-seo";
import { generateCategoryMetadata } from "@/lib/blog/metadata";
import type { ArticleCategory } from "@/lib/blog/types";
import { generateMetadata as pageMetadata } from "@/app/(blog)/[category]/page";

const REAL = Object.keys(CATEGORY_SEO) as ArticleCategory[];
const genericTitle = (name: string) =>
  `${name.charAt(0).toUpperCase() + name.slice(1)} Insights`;
const genericDescription = (name: string) =>
  `Expert guides and insights about ${name} in Indonesia and Bali.`;

describe("every real category", () => {
  it("there are six of them", () => {
    expect(REAL.sort()).toEqual(
      ["business", "living", "property", "taxes", "trends", "visas"].sort(),
    );
  });

  it.each(REAL)("%s is not served the generic sentence", (category) => {
    const meta = generateCategoryMetadata(category);

    expect(meta.title).not.toBe(genericTitle(category));
    expect(meta.description).not.toBe(genericDescription(category));
    expect(meta.openGraph?.title).not.toBe(
      `${genericTitle(category)} | Bali Zero`,
    );
    expect(meta.twitter?.title).not.toBe(genericTitle(category));
  });

  it.each(REAL)("%s is served the same through its page", async (category) => {
    const meta = await pageMetadata({ params: Promise.resolve({ category }) });

    expect(meta).toEqual(generateCategoryMetadata(category));
    expect(meta.title).not.toBe(genericTitle(category));
  });
});

describe("the category that had no text in the page's table", () => {
  it("carries the title and the description written for it, everywhere they are shown", () => {
    const written = CATEGORY_SEO.trends;

    const meta = generateCategoryMetadata("trends");

    expect(meta.title).toBe(written.title);
    expect(meta.description).toBe(written.description);
    expect(meta.openGraph).toMatchObject({
      title: `${written.title} | Bali Zero`,
      description: written.description,
      url: "https://balizero.com/trends",
    });
    expect(meta.twitter).toMatchObject({
      title: written.title,
      description: written.description,
    });
    expect(meta.alternates?.canonical).toBe("https://balizero.com/trends");
    // The keywords stay the ones the page had: none were written for it.
    expect(meta.keywords).toEqual(["trends", "indonesia", "bali", "guide"]);
  });
});

describe("what does not change", () => {
  // The five titles the page serves today. They differ from CATEGORY_SEO's on
  // purpose or by drift; which of the two tables should win is not decided
  // here, so the page's own text stays.
  it.each([
    ["visas", "Indonesia Visa & Immigration Guides"],
    ["business", "Starting a Business in Indonesia"],
    ["taxes", "Indonesia Tax & Legal Guides"],
    ["property", "Property Investment in Bali & Indonesia"],
    ["living", "Living in Bali - Expat Lifestyle Guides"],
  ])("%s keeps the title of the page's own table", (category, title) => {
    const meta = generateCategoryMetadata(category);

    expect(meta.title).toBe(title);
    expect(meta.title).not.toBe(
      CATEGORY_SEO[category as ArticleCategory].title,
    );
    expect(meta.openGraph?.title).toBe(`${title} | Bali Zero`);
  });

  it.each(["blog", "zzz-nonsense", "digital"])(
    "a name that is no category (%s) still gets the generic sentence here",
    (name) => {
      const meta = generateCategoryMetadata(name);

      expect(meta.title).toBe(genericTitle(name));
      expect(meta.description).toBe(genericDescription(name));
    },
  );
});
