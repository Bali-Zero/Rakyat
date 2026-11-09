import { describe, expect, it } from "vitest";
import baseline from "./mdx-article-guard.fr-ru.baseline.json";
import {
  ARTICLES_ROOT,
  compileArticlesInChunks,
  formatFailureAssertionMessage,
  walkMdxFiles,
} from "./mdx-article-guard";

const pinned: string[] = baseline;

const files = walkMdxFiles(ARTICLES_ROOT).filter(
  (file) => file.endsWith(".fr.mdx") || file.endsWith(".ru.mdx"),
);

// A page whose article does not compile serves HTTP 200 and no body. These files fail today, so
// they are pinned until cured: a new failure is refused, and a cured file must leave the list.
describe("MDXContentRSC French and Russian article compilation ratchet", () => {
  it("pins the files that fail and refuses new or stale entries", async () => {
    expect(files.length).toBeGreaterThan(300);
    const failures = await compileArticlesInChunks(files, 8);
    const unexpected = failures.filter((f) => !pinned.includes(f.file));
    expect(
      unexpected,
      formatFailureAssertionMessage("French or Russian", unexpected),
    ).toEqual([]);
    const failing = new Set(failures.map((f) => f.file));
    expect(
      pinned.filter((file) => !failing.has(file)),
      "these now compile: remove them from mdx-article-guard.fr-ru.baseline.json",
    ).toEqual([]);
  }, 180000);
});
