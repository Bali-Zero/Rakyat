// @vitest-environment node
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { serialize } from "next-mdx-remote/serialize";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";
import baseline from "./article-mdx-compile.baseline.json";

// An article that does not compile serves its page with no body, HTTP 200: nothing else notices.
// The translator writes such files (`<25%` for `&lt;25%`, `title:` for `title=`), so this ratchet
// pins the ones that already fail and refuses a new one. Curing a pinned file means removing it here.
const ARTICLES = join(dirname(fileURLToPath(import.meta.url)), "../content/articles");
const FRONTMATTER = /^---\s*\n[\s\S]*?\n---\s*\n/;

function articleFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return articleFiles(path);
    return entry.name.endsWith(".mdx") ? [path] : [];
  });
}

async function compileError(source: string): Promise<string | null> {
  try {
    await serialize(source.replace(FRONTMATTER, ""), {
      mdxOptions: { remarkPlugins: [remarkGfm] },
    });
    return null;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const reason = message.split("\n").find((line) => line.trim() && !line.startsWith("[next-mdx-remote]"));
    return (reason ?? message).slice(0, 120);
  }
}

describe("article MDX compile ratchet", () => {
  it("rejects the shapes the translator has written", async () => {
    for (const body of [
      "| Layer | TKDN <25% |\n| --- | --- |\n",
      '<Checklist\n  title: "x"\n/>\n',
      "<InfoCard>\n\ntext\n</InfoCAard>\n",
    ]) {
      expect(await compileError(body), body).not.toBeNull();
    }
  });

  it("accepts the same content written correctly", async () => {
    for (const body of [
      "| Layer | TKDN &lt;25% |\n| --- | --- |\n",
      '<Checklist\n  title="x"\n/>\n',
      "<InfoCard>\n\ntext\n</InfoCard>\n",
    ]) {
      expect(await compileError(body), body).toBeNull();
    }
  });

  it("pins every article that fails to compile and refuses new or stale entries", async () => {
    const failing: Record<string, string> = {};
    for (const file of articleFiles(ARTICLES)) {
      const error = await compileError(readFileSync(file, "utf8"));
      if (error) failing[relative(ARTICLES, file)] = error;
    }
    const names = Object.keys(failing).sort();
    const unexpected = names.filter((name) => !baseline.includes(name));
    expect(
      unexpected.map((name) => `${name}: ${failing[name]}`),
      "new articles that do not compile serve an empty page: fix the MDX",
    ).toEqual([]);
    expect(
      baseline.filter((name) => !names.includes(name)),
      "these now compile: remove them from article-mdx-compile.baseline.json",
    ).toEqual([]);
  }, 300_000);
});
