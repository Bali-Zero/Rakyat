import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The /kbli/[code] page is a long stack of peer cards under one <h1>. Every
// card title must be an <h2>, so the outline has no skipped level
// (h1 → h3) and no card is nested under the card before it.
const page = readFileSync(
  join(process.cwd(), "src/app/kbli/[code]/page.tsx"),
  "utf8",
);

const tagFor = (title: string): string | null => {
  const escaped = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = page.match(
    new RegExp(`<(\\w+)\\b[^>]*>\\s*${escaped}\\s*</\\1>`),
  );
  return match ? match[1] : null;
};

describe("/kbli/[code] card titles are peer <h2> headings", () => {
  it.each([
    "What You Need",
    "Who This Is For",
    "You&apos;ll Also Need",
    "Complementary Codes",
  ])("%s is an <h2>", (title) => {
    expect(tagFor(title)).toBe("h2");
  });

  it("has no <h3> card title left on the page", () => {
    expect(page).not.toMatch(/<h3\b/);
  });

  it("keeps the page <h1> and the existing section <h2>s (positive control)", () => {
    expect(page).toMatch(/<h1\b/);
    expect(tagFor("Bali Intelligence")).toBe("h2");
    expect(tagFor("Related Codes")).toBe("h2");
  });
});
