import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ unstable_cache: <T>(fn: T) => fn }));
// Every case below must be answered from the local corpus: a request that reached the
// backend would be a different bug, so fetch throws and is asserted unused.
const fetchSpy = vi.fn(() => {
  throw new Error("fetch must not be called");
});
vi.stubGlobal("fetch", fetchSpy);

const { getArticleByLocale } = await import("@/lib/blog/articles");

// Two real corpus articles that both carry an .id.mdx.
const CAT = "immigration";
const A = "bali-homestays-fight-for-more-time-as-licensing-deadline-looms";
const B =
  "bali-gets-direct-links-to-avalon-and-sunshine-coast-as-australia-routes-expand";
const DIR = path.join(process.cwd(), "src/content/articles", CAT);
const titleOf = (file: string) =>
  String(matter(fs.readFileSync(path.join(DIR, file), "utf8")).data.title);

const A_EN = titleOf(`${A}.mdx`);
const A_ID = titleOf(`${A}.id.mdx`);
const B_EN = titleOf(`${B}.mdx`);
const B_ID = titleOf(`${B}.id.mdx`);

describe("getArticleByLocale: the locale is allowlisted before it reaches a path", () => {
  it("fixtures are four distinct real titles", () => {
    expect(new Set([A_EN, A_ID, B_EN, B_ID]).size).toBe(4);
  });

  // Guilt: a locale that would traverse to B gives A's English (what an unknown language
  // gets today), never B. English rather than null: one behaviour for every value outside
  // the allowlist, and the page already renders an English article.
  const hostile = [
    `/../${B}.id`,
    `../${B}.id`,
    `id/../${B}.id`,
    "id/../../x",
    "id\\..\\x",
    "ID",
    "id ",
    "id\0",
  ];
  it.each(hostile)("%j never returns another article", async (loc) => {
    const r = await getArticleByLocale(CAT, A, loc);
    expect(r?.title).toBe(A_EN);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("id returns the Indonesian translation", async () => {
    expect((await getArticleByLocale(CAT, A, "id"))?.title).toBe(A_ID);
  });
  it("en and undefined return English", async () => {
    expect((await getArticleByLocale(CAT, A, "en"))?.title).toBe(A_EN);
    expect(
      (await getArticleByLocale(CAT, A, undefined as unknown as string))?.title,
    ).toBe(A_EN);
  });
  it("an unknown well-formed locale (de) returns English", async () => {
    expect((await getArticleByLocale(CAT, A, "de"))?.title).toBe(A_EN);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
