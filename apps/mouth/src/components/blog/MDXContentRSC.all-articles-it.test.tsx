import { describe, expect, it } from "vitest";
import {
  ARTICLES_ROOT,
  compileArticlesInChunks,
  formatFailureAssertionMessage,
  walkMdxFiles,
} from "./mdx-article-guard";

const italianFiles = walkMdxFiles(ARTICLES_ROOT).filter((file) =>
  file.endsWith(".it.mdx"),
);

describe("MDXContentRSC Italian article compilation guard", () => {
  it("every Italian article compiles", async () => {
    expect(italianFiles.length).toBeGreaterThan(800);
    const failures = await compileArticlesInChunks(italianFiles, 8);
    expect(failures, formatFailureAssertionMessage("Italian", failures)).toEqual(
      [],
    );
  }, 180000);
});
