import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * DK-16: ranking KITAS articles did not link to the service page that sells
 * it. /services/kitas does not exist yet, so each English article now
 * carries one InfoCard that links to /services/visa, with no price and no
 * figure in the card. Same pattern as articles-services-company-cta.
 *
 * Declared limits: reads the MDX as text, English files only. Translations
 * follow through the automatic translation flow.
 */
const ARTICLES = [
  "immigration/kitas-extension-renewal-guide.mdx",
  "immigration/kitas-renewal-denied-common-reasons.mdx",
  "immigration/indonesia-visa-timeline-comparison.mdx",
  "immigration/e25b-director-kitas-guide.mdx",
];
const CARD = /<InfoCard\b[^>]*>[\s\S]*?<\/InfoCard>/g;
const LINK = "](/services/visa)";

const cardsWithLink = (src: string) =>
  [...src.matchAll(CARD)].map((m) => m[0]).filter((c) => c.includes(LINK));

describe.each(ARTICLES)("%s links to /services/visa", (file) => {
  const src = readFileSync(join(__dirname, "articles", file), "utf8");

  it("positive control: the article is read and talks about KITAS", () => {
    expect(src).toContain("KITAS");
  });

  it("has exactly one link, inside one InfoCard", () => {
    expect(src.split(LINK).length - 1).toBe(1);
    expect(cardsWithLink(src)).toHaveLength(1);
  });

  it("the card carries no price or figure", () => {
    const [card] = cardsWithLink(src);
    expect(card).toBeDefined();
    expect(card).not.toMatch(/\d|IDR|USD|Rp/);
  });
});
