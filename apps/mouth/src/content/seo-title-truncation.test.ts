// =============================================================================
// SEO TITLE TRUNCATION — an English seoTitle must not end in a literal "...".
//
// WHY THIS EXISTS. Measured on main d3a1480 (2026-11-24): 326 English articles
// carried a seoTitle the content generator had cut to ~57 characters and
// finished with "..." — six of them ending "|...". `lib/blog/metadata.ts` uses
// seoTitle as-is for <title>, so Google received the dots literally, e.g.
// "Indonesia DTA Benefits 2026: How to Claim Treaty Rates |...".
//
// PARSED WITH THE APP'S OWN PARSER (gray-matter, as in articles.ts), so the
// value checked is the value the page renders.
//
// SCOPE: English files only (`<slug>.mdx`, no `.<locale>.mdx` suffix). The
// translated locales carry the same defect and are fixed in follow-up PRs; this
// guard widens to them when they are clean.
// =============================================================================

import fs from "fs";
import path from "path";
import matter from "gray-matter";
import { describe, expect, it } from "vitest";

const ARTICLES_PATH = path.join(process.cwd(), "src/content/articles");
const LOCALE_SUFFIX = /\.[a-z]{2}\.mdx$/;

function englishSeoTitles(): { rel: string; seoTitle: unknown }[] {
  const out: { rel: string; seoTitle: unknown }[] = [];
  for (const folder of fs.readdirSync(ARTICLES_PATH)) {
    const dir = path.join(ARTICLES_PATH, folder);
    if (!fs.statSync(dir).isDirectory()) continue;
    for (const file of fs.readdirSync(dir)) {
      if (!file.endsWith(".mdx") || LOCALE_SUFFIX.test(file)) continue;
      const raw = fs.readFileSync(path.join(dir, file), "utf-8");
      let seoTitle: unknown;
      try {
        seoTitle = matter(raw).data?.seoTitle;
      } catch {
        seoTitle = "<<UNPARSEABLE FRONTMATTER>>...";
      }
      out.push({ rel: `${folder}/${file}`, seoTitle });
    }
  }
  return out;
}

describe("seoTitle truncation (English articles)", () => {
  const articles = englishSeoTitles();

  it("finds a corpus to check, instead of passing on an empty read", () => {
    expect(articles.length).toBeGreaterThan(900);
    expect(
      articles.filter((a) => typeof a.seoTitle === "string").length,
    ).toBeGreaterThan(900);
  });

  it('no English seoTitle ends with a machine-cut "..."', () => {
    const offenders = articles
      .filter(
        (a) =>
          typeof a.seoTitle === "string" &&
          a.seoTitle.trimEnd().endsWith("..."),
      )
      .map((a) => `${a.rel} — ${JSON.stringify(a.seoTitle)}`);
    expect(
      offenders,
      `seoTitle reaches <title> verbatim, so Google shows the dots:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });
});
