import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(process.cwd(), "src/app/(workspace)/accounting");
const palette = readFileSync(join(root, "control-centre.css"), "utf8");
const componentNames = [
  "ReconciliationInbox.tsx",
  "ReconciliationTabs.tsx",
  "TransferList.tsx",
  "OpenedTransfer.tsx",
  "ReconciliationViews.tsx",
  "CcAmount.tsx",
  "BankStatementUpload.tsx",
];
const components = componentNames.map((name) => ({
  name,
  source: readFileSync(join(root, "_components", name), "utf8"),
}));

describe("control-centre palette boundary", () => {
  it("declares every control-centre custom property used by components", () => {
    const declared = new Set(
      Array.from(palette.matchAll(/(--cc-[\w-]+)\s*:/g), (match) => match[1]),
    );
    const used = new Set(
      components.flatMap(({ source }) =>
        Array.from(
          source.matchAll(/var\((--cc-[\w-]+)\)/g),
          (match) => match[1],
        ),
      ),
    );
    expect(used.size).toBeGreaterThan(10);
    for (const property of used)
      expect(declared.has(property), property).toBe(true);
  });

  it("has the operative-dark selector and every theme-dependent K2 property", () => {
    const dark = palette.match(
      /\[data-theme="operative-dark"\] \.cc-root\s*\{([\s\S]*?)\}/,
    )?.[1];
    expect(dark).toBeDefined();
    for (const property of [
      "--cc-card",
      "--cc-wash",
      "--cc-pill",
      "--cc-sel",
      "--cc-tx",
      "--cc-tx-2",
      "--cc-edge",
      "--cc-hair",
      "--cc-line-control",
      "--cc-act",
      "--cc-act-on",
      "--cc-confirm-ink",
      "--cc-link-ink",
      "--cc-choose-ink",
      "--cc-nomatch-ink",
    ])
      expect(dark, property).toContain(property);
  });

  it("keeps literals and unsupported colour utilities out of components", () => {
    const family =
      /(?<![\w-])(?:bg|text|border|ring|divide|fill|stroke)-(?:card|muted|muted-foreground|foreground|background|popover|primary|secondary|accent|destructive|border|input)(?![\w-])/;
    for (const { name, source } of components) {
      expect(source, name).not.toMatch(/#[\da-f]{3,8}|rgba?\(|hsla?\(/i);
      expect(source, name).not.toMatch(family);
      expect(source, name).not.toContain("dark:");
      expect(source, name).not.toMatch(/\$\{[^}]*var\(/);
      const variables = Array.from(
        source.matchAll(/var\((--[\w-]+)\)/g),
        (m) => m[1],
      );
      for (const variable of variables) {
        expect(
          variable.startsWith("--cc-") ||
            [
              "--state-danger",
              "--state-warning",
              "--bz-text-1",
              "--bz-text-2",
              "--bz-card",
            ].includes(variable),
          `${name}: ${variable}`,
        ).toBe(true);
      }
    }
  });
});
