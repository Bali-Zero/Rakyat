import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(__dirname, "home.css"), "utf-8");

describe("home.css after the 2026-11-01 web audit", () => {
  it("never hides the account link by position", () => {
    // The 680 px rule `nav a:last-child { display: none }` targeted the
    // MyBALIZERO link; the open-menu cascade overrode it, so it only misled
    // readers of the file. A link must be hidden by what it is, not where it sits.
    expect(CSS).not.toMatch(/nav a:last-child\s*\{\s*display:\s*none/);
  });

  it("balances headings and avoids orphans in prose", () => {
    expect(CSS).toMatch(/:is\(h1, h2, h3, h4\)\s*\{\s*text-wrap:\s*balance/);
    expect(CSS).toMatch(/:is\(p, li, blockquote\)\s*\{\s*text-wrap:\s*pretty/);
  });

  it("does not use content-visibility on the home sections", () => {
    // #9117 showed Chromium paints content-visibility:auto sections blank in
    // beyond-viewport captures (post-deploy QA screenshots, Lighthouse report).
    expect(CSS).not.toMatch(/content-visibility:\s*auto/);
  });
});
