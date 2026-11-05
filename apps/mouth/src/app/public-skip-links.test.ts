import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every public surface renders a skip link AND the target it points to.
 * Before 2026-11-01 the link was styled in two stylesheets and rendered on
 * none of the public pages (only workspace and portal had one).
 */
const SURFACES: Record<string, string[]> = {
  "R19 home": ["components/r19/home/Entry.tsx", "app/(marketing)/page.tsx"],
  "blog / Rumah Putih": ["app/(blog)/layout.tsx"],
  "KBLI navigator": ["app/kbli/layout.tsx"],
};

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf-8");

describe("public skip links", () => {
  for (const [name, files] of Object.entries(SURFACES)) {
    it(`${name} has a skip link and its target`, () => {
      const src = files.map(read).join("\n");
      expect(src).toMatch(/href="#main-content"/);
      expect(src).toMatch(/id="main-content"/);
    });
  }
});

describe("light surfaces set color-scheme on the root", () => {
  it("globals.css lifts color-scheme: light to html for the R19 home and articles", () => {
    const css = read("app/globals.css");
    const dark = css.indexOf("html {\n  color-scheme: dark;");
    const light = css.search(
      /html:has\(\[data-r19-home\], \.rumah-putih\)\s*\{\s*color-scheme:\s*light;/,
    );
    expect(dark).toBeGreaterThan(-1);
    expect(light).toBeGreaterThan(dark);
  });
});
