import { beforeEach, describe, expect, it, vi } from "vitest";

// gray-matter caches parsed files by content string when called without options: broken YAML
// throws the first time and silently yields empty frontmatter the second time, which renders an
// article as "Untitled". The reader relies on the throw to skip corrupted articles.

const existsSyncMock = vi.fn();
const readFileSyncMock = vi.fn();
const readdirSyncMock = vi.fn();
const statSyncMock = vi.fn();

vi.mock("next/cache", () => ({
  unstable_cache: <T>(fn: T) => fn,
}));

vi.mock("fs", () => ({
  default: {
    existsSync: (...args: unknown[]) =>
      (existsSyncMock as (...a: unknown[]) => unknown)(...args),
    readFileSync: (...args: unknown[]) =>
      (readFileSyncMock as (...a: unknown[]) => unknown)(...args),
    readdirSync: (...args: unknown[]) =>
      (readdirSyncMock as (...a: unknown[]) => unknown)(...args),
    statSync: (...args: unknown[]) =>
      (statSyncMock as (...a: unknown[]) => unknown)(...args),
  },
  existsSync: (...args: unknown[]) =>
    (existsSyncMock as (...a: unknown[]) => unknown)(...args),
  readFileSync: (...args: unknown[]) =>
    (readFileSyncMock as (...a: unknown[]) => unknown)(...args),
  readdirSync: (...args: unknown[]) =>
    (readdirSyncMock as (...a: unknown[]) => unknown)(...args),
  statSync: (...args: unknown[]) =>
    (statSyncMock as (...a: unknown[]) => unknown)(...args),
}));

const { getArticleBySlug } = await import("./articles");

const BROKEN = `---\ntitle: "Broken\n  : : [unclosed\ncategory: business\n---\n\nBody text.\n`;
const VALID = `---\ntitle: "Valid Guide"\ncategory: business\npublishedAt: "2026-02-20"\n---\n\nBody text.\n`;

beforeEach(() => {
  existsSyncMock.mockReset();
  readFileSyncMock.mockReset();
  readdirSyncMock.mockReset();
  statSyncMock.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) }),
  );
});

describe("article frontmatter parsing without the gray-matter content cache", () => {
  it("skips an article with broken frontmatter on every read, not only the first", async () => {
    existsSyncMock.mockImplementation((p: unknown) =>
      String(p).endsWith("business/broken-guide.mdx"),
    );
    readFileSyncMock.mockReturnValue(BROKEN);

    const first = await getArticleBySlug("business", "broken-guide");
    const second = await getArticleBySlug("business", "broken-guide");
    const third = await getArticleBySlug("business", "broken-guide");

    expect(first).toBeNull();
    expect(second).toBeNull();
    expect(third).toBeNull();
  });

  it("still parses a valid article, repeatedly", async () => {
    existsSyncMock.mockImplementation((p: unknown) =>
      String(p).endsWith("business/valid-guide.mdx"),
    );
    readFileSyncMock.mockReturnValue(VALID);

    const first = await getArticleBySlug("business", "valid-guide");
    const second = await getArticleBySlug("business", "valid-guide");

    expect(first?.title).toBe("Valid Guide");
    expect(second?.title).toBe("Valid Guide");
  });
});
