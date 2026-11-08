import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ArticleWithFAQJsonLd } from "./EnhancedJsonLd";

const EVIL = "</script><script>alert(1)</script>";

// The FAQ answer is written by an LLM translator: it must not be able to close the
// JSON-LD container, whatever it contains.
describe("EnhancedJsonLd escapes script content at the sink", () => {
  const html = renderToStaticMarkup(
    <ArticleWithFAQJsonLd
      title="t"
      description="d"
      slug="s"
      category="visas"
      publishedAt="2026-01-01"
      author={{ name: "a" }}
      faq={[{ question: `q ${EVIL}`, answer: EVIL }]}
    />,
  );

  it("renders exactly one closing </script> and one opening <script", () => {
    expect(html.match(/<\/script>/gi)).toHaveLength(1);
    expect(html.match(/<script/gi)).toHaveLength(1);
    expect(html).not.toContain("alert(1)</script>");
  });

  it("the embedded JSON parses back to the input", () => {
    const body = html.replace(/^<script[^>]*>/, "").replace(/<\/script>$/, "");
    const graph = JSON.parse(body)["@graph"];
    const faq = graph.find(
      (n: { "@type": string }) => n["@type"] === "FAQPage",
    );
    expect(faq.mainEntity[0].acceptedAnswer.text).toBe(EVIL);
    expect(faq.mainEntity[0].name).toBe(`q ${EVIL}`);
  });
});
