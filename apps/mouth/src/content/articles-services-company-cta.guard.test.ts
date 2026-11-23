import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * DK-16: two ranking articles about PT PMA did not link to the service page
 * that sells it. Each English article now carries one InfoCard that links to
 * /services/company, with no price and no figure in the card.
 *
 * Declared limits: reads the MDX as text, English files only. Translations
 * follow through the automatic translation flow.
 */
const ARTICLES = [
  "immigration/e25b-director-kitas-guide.mdx",
  "property/property-via-pt-pma-indonesia.mdx",
];
const CARD = /<InfoCard\b[^>]*>[\s\S]*?<\/InfoCard>/g;
const LINK = "](/services/company)";

const cardsWithLink = (src: string) =>
  [...src.matchAll(CARD)].map((m) => m[0]).filter((c) => c.includes(LINK));

describe.each(ARTICLES)("%s links to /services/company", (file) => {
  const src = readFileSync(join(__dirname, "articles", file), "utf8");

  it("positive control: the article is read and talks about PT PMA", () => {
    expect(src).toContain("PT PMA");
  });

  it("has exactly one link, inside one InfoCard", () => {
    expect(src.split(LINK).length - 1).toBe(1);
    expect(cardsWithLink(src)).toHaveLength(1);
  });

  it("the card carries no price or figure", () => {
    const [card] = cardsWithLink(src);
    expect(card).toBeDefined();
    // "E25B" is the visa's name, the only digits a card may carry.
    expect(card?.replace(/\bE25B\b/g, "")).not.toMatch(/\d|IDR|USD|Rp/);
  });
});
