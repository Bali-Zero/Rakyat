import fs from "node:fs";
import path from "node:path";
import { renderToString } from "react-dom/server";
import matter from "gray-matter";
import { describe, expect, it } from "vitest";

import { renderMDXBody } from "../MDXContentRSC";
import { InfoCard, type InfoCardItem } from "./InfoCard";

const composerItems: InfoCardItem[] = [
  { label: "Should I Worry?", value: "Check your turnover now." },
  { label: "When", value: "December 31, 2026" },
];

describe("InfoCard", () => {
  it("renders the labelled items the article composer emits", () => {
    const html = renderToString(
      <InfoCard title="Quick Summary" items={composerItems} />,
    );

    expect(html).toContain("Quick Summary");
    expect(html).toContain("Should I Worry?");
    expect(html).toContain("Check your turnover now.");
    expect(html).toContain("December 31, 2026");
    expect(html.match(/<li/g)).toHaveLength(2);
  });

  it("renders plain string items with inline bold", () => {
    const html = renderToString(
      <InfoCard title="Visa" items={["**Purpose:** Business meetings"]} />,
    );

    expect(html).toContain("<strong");
    expect(html).toContain("Purpose:");
    expect(html).toContain("Business meetings");
    expect(html).not.toContain("**");
  });

  it("keeps rendering children when there are no items", () => {
    const html = renderToString(
      <InfoCard title="Note">Body from children</InfoCard>,
    );

    expect(html).toContain("Body from children");
    expect(html).not.toContain("<ul");
  });

  it("renders items before children when both are given", () => {
    const html = renderToString(
      <InfoCard title="Both" items={composerItems}>
        Trailing children
      </InfoCard>,
    );

    expect(html).toContain("Check your turnover now.");
    expect(html.indexOf("Check your turnover now.")).toBeLessThan(
      html.indexOf("Trailing children"),
    );
  });

  it("renders no list for an empty items array", () => {
    const html = renderToString(<InfoCard title="Empty" items={[]} />);

    expect(html).toContain("Empty");
    expect(html).not.toContain("<ul");
  });

  it("renders the Quick Summary rows of a real composed article", async () => {
    const articlePath = path.join(
      process.cwd(),
      "src/content/articles/tax-legal/indonesia-0-5-percent-tax-december-31-djp-faq.mdx",
    );
    const { content } = matter(fs.readFileSync(articlePath, "utf8"));
    const body = await renderMDXBody(content.trim());
    const html = renderToString(<>{body}</>);

    expect(html).toContain("Quick Summary");
    expect(html).toContain("Should I Worry?");
    expect(html).toContain("December 31, 2026 is the last day on 0.5%");
  });
});
