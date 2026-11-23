import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

const root = mkdtempSync(join(tmpdir(), "homepage-layout-guard-"));

function writeArticle(folder: string, slug: string, frontmatter: string) {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${slug}.mdx`),
    `---\n${frontmatter}\n---\n\nSynthetic article body.`,
  );
}

function writeUnreadableArticle(folder: string, slug: string) {
  const dir = join(root, "src/content/articles", folder);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${slug}.mdx`), "---\ntitle: [\n---\n\nBody.");
}

const published = (title: string) =>
  `title: "${title}"\ncategory: immigration\npublishedAt: "2026-03-01"\nauthor: "Synthetic Author"`;

let validateHomepageLayoutFromDisk: typeof import("./articles").validateHomepageLayoutFromDisk;

beforeAll(async () => {
  writeArticle("immigration", "published-hero", published("Published hero"));
  writeArticle("immigration", "valid-insight", published("Valid insight"));
  writeArticle(
    "immigration",
    "draft-hero",
    `${published("Draft hero")}\nstatus: draft`,
  );
  writeArticle(
    "tax",
    "hidden-latest",
    `${published("Hidden latest")}\nnoIndex: true`,
  );
  writeUnreadableArticle("business", "unreadable-hero");
  writeArticle(
    "immigration",
    "duplicate-insight",
    published("First duplicate"),
  );
  writeArticle("tax", "duplicate-insight", published("Second duplicate"));
  writeArticle(
    "immigration",
    "mixed-hero",
    `${published("Draft copy")}\nstatus: draft`,
  );
  writeArticle("tax", "mixed-hero", published("Published copy"));
  vi.spyOn(process, "cwd").mockReturnValue(root);
  vi.resetModules();
  ({ validateHomepageLayoutFromDisk } = await import("./articles"));
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline test")));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

afterAll(() => {
  vi.restoreAllMocks();
  rmSync(root, { recursive: true, force: true });
});

async function expectMissing(layout: Record<string, unknown>, key: string) {
  try {
    await validateHomepageLayoutFromDisk(layout);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    expect(message).toContain(`- ${key}:`);
    expect(
      [...message.matchAll(/^  - ([^:]+):/gm)].map((match) => match[1]),
    ).toEqual([key]);
    return;
  }
  throw new Error("Expected the homepage layout guard to reject the layout.");
}

describe("validateHomepageLayoutFromDisk", () => {
  it("flags a draft hero article", async () => {
    await expectMissing({ hero_main: "draft-hero" }, "hero_main");
  });

  it("flags a noIndex latest article", async () => {
    await expectMissing({ latest_1: "hidden-latest" }, "latest_1");
  });

  it("flags an unreadable hero article", async () => {
    await expectMissing({ hero_main: "unreadable-hero" }, "hero_main");
  });

  it("flags a hero article with no file", async () => {
    await expectMissing({ hero_main: "missing-hero" }, "hero_main");
  });

  it("flags an insight article found in two folders", async () => {
    await expectMissing({ insight_1: "duplicate-insight" }, "insight_1");
  });

  it("accepts a published hero and a renderable insight pin", async () => {
    await expect(
      validateHomepageLayoutFromDisk({
        hero_main: "published-hero",
        insight_1: "valid-insight",
      }),
    ).resolves.toMatchObject({ ok: true, checked: 2 });
  });

  it("accepts a hero with a published file when another copy is draft", async () => {
    await expect(
      validateHomepageLayoutFromDisk({ hero_main: "mixed-hero" }),
    ).resolves.toMatchObject({ ok: true, checked: 1 });
  });

  it("counts every curated key in a layout of valid slugs", async () => {
    await expect(
      validateHomepageLayoutFromDisk({
        hero_main: "published-hero",
        latest_1: "mixed-hero",
        insight_1: "valid-insight",
        _comment: "ignored",
      }),
    ).resolves.toMatchObject({ ok: true, checked: 3 });
  });
});
