import { existsSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { portalNavigation } from "@/types/navigation";

/**
 * Every portal page used to share the browser tab title "Client Portal |
 * Bali Zero", so a client with several tabs open could not tell them apart.
 * Each sidebar page now names itself; the expected title is read from the
 * sidebar, so a renamed menu item and its tab title cannot drift apart.
 */
const ROOT = path.resolve(__dirname);

const pages = portalNavigation
  .flatMap((section) => section.items)
  .filter((item) => item.href !== "/portal")
  .map((item) => ({
    title: item.title,
    segment: item.href.replace(/^\/portal\//, ""),
  }));

describe("portal route tab titles", () => {
  it("covers the sidebar pages (positive control)", () => {
    expect(pages.map((p) => p.segment)).toEqual([
      "process",
      "vault",
      "messages",
      "companies",
      "visa",
      "taxes",
      "lkpm",
      "billing",
      "profile",
      "family",
      "settings",
    ]);
  });

  it.each(pages)(
    "$segment names its own tab ($title)",
    async ({ segment, title }) => {
      const layout = path.join(ROOT, segment, "layout.tsx");
      expect(existsSync(layout), `${segment}/layout.tsx is missing`).toBe(true);
      const mod = (await import(`./${segment}/layout.tsx`)) as {
        metadata?: { title?: unknown };
        default?: (props: { children: string }) => unknown;
      };
      expect(mod.metadata?.title).toBe(title);
      expect(mod.default?.({ children: "child" })).toBe("child");
    },
  );
});
