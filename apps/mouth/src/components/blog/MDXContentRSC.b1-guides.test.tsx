import path from "node:path";
import { expect, it } from "vitest";
import {
  ARTICLES_ROOT,
  compileArticlesInChunks,
  formatFailureAssertionMessage,
} from "./mdx-article-guard";

const files = [
  "e-voa-electronic-visa-on-arrival-guide",
  "tourist-visa-extension-indonesia-guide",
].flatMap((slug) =>
  ["", ".id", ".it", ".fr", ".ru"].map((locale) =>
    path.join(ARTICLES_ROOT, "immigration", `${slug}${locale}.mdx`),
  ),
);
it("compiles both B1 guides in all five locales using production MDX options", async () => {
  expect(files).toHaveLength(10);
  const failures = await compileArticlesInChunks(files, 5);
  expect(
    failures,
    formatFailureAssertionMessage("B1 guides", failures),
  ).toEqual([]);
}, 120000);
