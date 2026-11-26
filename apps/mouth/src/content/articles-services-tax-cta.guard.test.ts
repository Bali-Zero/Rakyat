import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * DK-16: ranking tax articles did not link to the service page that sells the
 * work. Each English article here carries one InfoCard that links to
 * /services/tax, with no price and no figure in the card. Same pattern as
 * articles-services-visa-cta and articles-services-company-cta.
 *
 * Declared limits: reads the MDX as text, English files only. Translations
 * follow through the automatic translation flow. tax/vat-ppn-guide is not
 * here: its .id/.it twins are pinned to the English body by source_sha256
 * (tax-accounting-prices.truth.test.ts), so a card there moves the twins too.
 */
const ARTICLES: [file: string, topic: string][] = [
  ["tax/double-tax-agreement-claiming-benefits.mdx", "SKD"],
  ["property/rental-income-tax.mdx", "PPh Final"],
];
const CARD = /<InfoCard\b[^>]*>[\s\S]*?<\/InfoCard>/g;
const LINK = "](/services/tax)";

const cardsWithLink = (src: string) =>
  [...src.matchAll(CARD)].map((m) => m[0]).filter((c) => c.includes(LINK));

describe.each(ARTICLES)("%s links to /services/tax", (file, topic) => {
  const src = readFileSync(join(__dirname, "articles", file), "utf8");

  it(`positive control: the article is read and talks about ${topic}`, () => {
    expect(src).toContain(topic);
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
