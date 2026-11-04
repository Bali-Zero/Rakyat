import { describe, expect, it } from "vitest";
import {
  assertInitialSsrHtml,
  extractJsonLdBlocks,
  stripExecutableScripts,
} from "./ssr-html-contract";

const VALID_SSR_FIXTURE = [
  '<!DOCTYPE html><html lang="en"><head>',
  "<title>Bali Visa Agency 2026 | Bali Zero</title>",
  '<link rel="canonical" href="https://balizero.com/services/visa" />',
  '<meta property="og:url" content="https://balizero.com/services/visa" />',
  '<script id="faq-jsonld" type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"Q1","acceptedAnswer":{"@type":"Answer","text":"A1"}}]}</script>',
  '<script type="application/ld+json">{"@context":"https://schema.org","@type":"GovernmentService","name":"Visa & Immigration"}</script>',
  "</head><body><main><h1>Visa &amp; Immigration</h1>",
  '<dl itemScope itemType="https://schema.org/GovernmentService">',
  '<dt>Service</dt><dd itemProp="serviceType">Immigration</dd>',
  '<dd itemProp="hoursAvailable">Mo-Fr 09:00-17:00</dd><dd itemProp="areaServed">Bali</dd></dl>',
  "<section><h2>Pricing</h2><p>C1 Tourism Visit: IDR 2.300.000</p></section>",
  "<section><h2>Frequently Asked Questions</h2></section></main></body></html>",
].join("\n");

describe("BZ-025: assertInitialSsrHtml initial server HTML contract", () => {
  it("passes on a valid server-rendered service HTML document", () => {
    expect(() =>
      assertInitialSsrHtml(VALID_SSR_FIXTURE, "visa", {
        requireExactOgUrl: true,
      }),
    ).not.toThrow();
  });

  it("extracts JSON-LD scripts even when extra attributes like id='faq-jsonld' are present", () => {
    expect(extractJsonLdBlocks(VALID_SSR_FIXTURE)).toHaveLength(2);
  });

  it("fails when Pricing, prices, or microdata exist only inside <script> or JSON-LD tags", () => {
    const shell = [
      '<!DOCTYPE html><html><head><link rel="canonical" href="https://balizero.com/services/visa" />',
      '<script id="faq-jsonld" type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","description":"Pricing 2.300.000 serviceType hoursAvailable areaServed","mainEntity":[{"@type":"Question","name":"Q","acceptedAnswer":{"@type":"Answer","text":"A"}}]}</script>',
      '<script type="application/ld+json">{"@context":"https://schema.org","@type":"GovernmentService"}</script></head>',
      "<body><main><h1>Visa</h1><h2>Pricing</h2><h2>Frequently Asked Questions</h2></main>",
      '<script>self.__next_f = ["2.300.000 <dl itemscope itemtype=https://schema.org/GovernmentService itemprop=serviceType>hoursAvailable areaServed</dl>"];</script></body></html>',
    ].join("\n");
    expect(stripExecutableScripts(shell)).not.toContain("2.300.000");
    expect(() => assertInitialSsrHtml(shell, "visa")).toThrow(
      /Missing server-rendered IDR price values/,
    );
  });

  it("fails when og:url is generic /services instead of page-specific /services/visa", () => {
    const genericOgHtml = VALID_SSR_FIXTURE.replace(
      'content="https://balizero.com/services/visa"',
      'content="https://balizero.com/services"',
    );
    expect(() =>
      assertInitialSsrHtml(genericOgHtml, "visa", { requireExactOgUrl: true }),
    ).toThrow(/Missing or mismatched og:url/);
  });
});
