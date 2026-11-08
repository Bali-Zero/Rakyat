import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const files = vi.hoisted(() => ({}) as Record<string, string>);
const find = (p: unknown) =>
  Object.keys(files).find((name) => String(p).endsWith(`articles/${name}`));
const folders = () => new Set(Object.keys(files).map((n) => n.split("/")[0]));
const exists = (p: unknown) =>
  String(p).endsWith("src/content/articles") ||
  [...folders()].some((f) => String(p).endsWith(`src/content/articles/${f}`)) ||
  find(p) !== undefined;
const list = (p: unknown) => {
  const folder = [...folders()].find((f) =>
    String(p).endsWith(`articles/${f}`),
  );
  return folder
    ? Object.keys(files)
        .filter((n) => n.startsWith(folder + "/"))
        .map((n) => n.slice(folder.length + 1))
    : [];
};
const read = (p: unknown) => {
  const name = find(p);
  if (!name) throw new Error("ENOENT " + String(p));
  return files[name];
};
vi.mock("fs", () => {
  const api = {
    existsSync: exists,
    readFileSync: read,
    readdirSync: list,
    statSync: () => ({ isDirectory: () => true }),
  };
  return { default: api, ...api };
});
vi.mock("next/cache", () => ({ unstable_cache: <T,>(fn: T) => fn }));
const layout = vi.hoisted(
  () =>
    ({
      insight_1: "pin-draft",
      insight_2: "pin-live",
      insight_3: "pin-review",
    }) as Record<string, string>,
);
vi.mock("@/content/homepage-layout.json", () => ({ default: layout }));

const { HomeJournal } = await import("./HomeJournal");

function article(slug: string, day: number, status = "") {
  files[`business/${slug}.mdx`] =
    `---\ntitle: "Story ${slug}"\ncategory: business\npublishedAt: "2026-10-${String(day).padStart(2, "0")}"\n${status}---\n\nBody.\n`;
}

function backendRows(...rows: [string, string][]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        success: true,
        total: rows.length,
        data: rows.map(([slug, category], i) => ({
          id: "news-" + i,
          title: "Backend copy " + slug,
          slug,
          summary: "Summary.",
          content: "Body.",
          source: "scraper",
          source_url: null,
          category,
          priority: "normal",
          status: "approved",
          image_url: null,
          view_count: 0,
          published_at: "2026-10-01T00:00:00Z",
          created_at: "2026-10-01T00:00:00Z",
          ai_summary: null,
          ai_tags: null,
        })),
      }),
    }),
  );
}

afterEach(() => {
  cleanup();
  for (const name of Object.keys(files)) delete files[name];
  vi.unstubAllGlobals();
  layout.insight_3 = "pin-review";
});

describe("HomeJournal Insight row over the real reader", () => {
  it("shows a published pin and never a draft or review pin", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    for (let i = 0; i < 7; i++) article("feed-" + i, 20 - i);
    article("pin-live", 3);
    article("pin-draft", 2, "status: draft\n");
    article("pin-review", 1, "status: review\n");
    render(await HomeJournal());
    const row = screen.getByRole("region", { name: "Insight" });
    expect(row.querySelectorAll("a[href='/business/pin-live']")).toHaveLength(
      1,
    );
    expect(document.querySelectorAll("a[href*='pin-draft']")).toHaveLength(0);
    expect(document.querySelectorAll("a[href*='pin-review']")).toHaveLength(0);
  });

  it("never shows a noIndex pin, even when the backend has an approved row for it", async () => {
    backendRows(["pin-noindex", "business"]);
    layout.insight_3 = "pin-noindex";
    for (let i = 0; i < 7; i++) article("feed-" + i, 20 - i);
    article("pin-live", 3);
    article("pin-noindex", 2, "noIndex: true\n");
    render(await HomeJournal());
    const row = screen.getByRole("region", { name: "Insight" });
    expect(row.querySelectorAll("a[href='/business/pin-live']")).toHaveLength(
      1,
    );
    expect(document.querySelectorAll("a[href*='pin-noindex']")).toHaveLength(0);
  });

  it("never shows a draft pin through a backend row in another category", async () => {
    backendRows(["pin-draft", "lifestyle"]);
    for (let i = 0; i < 7; i++) article("feed-" + i, 20 - i);
    article("pin-live", 3);
    article("pin-draft", 2, "status: draft\n");
    render(await HomeJournal());
    const row = screen.getByRole("region", { name: "Insight" });
    expect(row.querySelectorAll("a[href='/business/pin-live']")).toHaveLength(
      1,
    );
    expect(document.querySelectorAll("a[href*='pin-draft']")).toHaveLength(0);
  });

  it("never shows a pin that exists only on the backend or in two MDX folders", async () => {
    backendRows(["pin-draft", "business"]);
    layout.insight_3 = "pin-dup";
    for (let i = 0; i < 7; i++) article("feed-" + i, 20 - i);
    article("pin-live", 3);
    article("pin-dup", 2);
    files["lifestyle/pin-dup.mdx"] = files["business/pin-dup.mdx"];
    render(await HomeJournal());
    const row = screen.getByRole("region", { name: "Insight" });
    expect(
      [...row.querySelectorAll("a")].map((a) => a.getAttribute("href")),
    ).toEqual(["/business/pin-live"]);
  });

  it("renders the home when a pin's title is not text, without that pin", async () => {
    backendRows(["pin-object", "business"]);
    layout.insight_3 = "pin-object";
    for (let i = 0; i < 7; i++) article("feed-" + i, 20 - i);
    article("pin-live", 3);
    files["business/pin-object.mdx"] =
      '---\ntitle:\n  text: Broken\ncategory: business\npublishedAt: "2026-10-02"\n---\n\nBody.\n';
    render(await HomeJournal());
    const row = screen.getByRole("region", { name: "Insight" });
    expect(
      [...row.querySelectorAll("a")].map((a) => a.getAttribute("href")),
    ).toEqual(["/business/pin-live"]);
  });
});
