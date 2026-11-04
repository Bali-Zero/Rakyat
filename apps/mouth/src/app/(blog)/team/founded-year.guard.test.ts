import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const SURFACES = {
  "team/page.tsx": read("./page.tsx"),
  "team/layout.tsx": read("./layout.tsx"),
};

describe("public founded year is 2020 on /team", () => {
  for (const [name, src] of Object.entries(SURFACES)) {
    it(`${name} never states 'since 2006' or 'since 2019'`, () => {
      expect(src).not.toMatch(/since\s+(2006|2019)/i);
    });

    it(`${name} states since 2020 (positive control)`, () => {
      expect(src).toMatch(/since\s+2020/i);
    });
  }

  it("the /team trust pill says Licensed since 2020", () => {
    const src = SURFACES["team/page.tsx"];
    expect(src).toMatch(/<span>Licensed since 2020<\/span>/);
  });
});
