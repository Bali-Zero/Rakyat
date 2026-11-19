import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(process.cwd(), "src/app/(workspace)/accounting");
const palette = readFileSync(join(root, "control-centre.css"), "utf8");
const darkPaletteMatch = palette.match(
  /\[data-theme="operative-dark"\] \.cc-root\s*\{([\s\S]*?)\}/,
);
const lightPalette = darkPaletteMatch
  ? palette.replace(darkPaletteMatch[0], "")
  : palette;
const componentNames = [
  "ReconciliationInbox.tsx",
  "ReconciliationTabs.tsx",
  "TransferList.tsx",
  "OpenedTransfer.tsx",
  "ReconciliationViews.tsx",
  "CcAmount.tsx",
  "BankStatementUpload.tsx",
  "CcGlyph.tsx",
  "CcArt.tsx",
];
const components = componentNames.map((name) => ({
  name,
  source: readFileSync(join(root, "_components", name), "utf8"),
}));
const colourFiles = [
  ...components,
  { name: "page.tsx", source: readFileSync(join(root, "page.tsx"), "utf8") },
];
const lightRootMatch = palette.match(/^\.cc-root\s*\{([\s\S]*?)\}/m);
const k1Palette = [
  ["--cc-card", "#fffcf7", "#1a1a1f"],
  ["--cc-wash", "#f3efe7", "#222127"],
  ["--cc-pill", "#efe9de", "rgba(247, 244, 238, 0.1)"],
  ["--cc-sel", "#f4efe6", "rgba(247, 244, 238, 0.06)"],
  ["--cc-tx", "#1d2c3b", "#f7f4ee"],
  ["--cc-tx-2", "#58626b", "rgba(247, 244, 238, 0.66)"],
  ["--cc-edge", "#ddd6ca", "rgba(247, 244, 238, 0.14)"],
  ["--cc-hair", "#ebe5da", "rgba(247, 244, 238, 0.08)"],
  ["--cc-line-control", "#857f73", "rgba(247, 244, 238, 0.46)"],
  ["--cc-act", "#253e33", "#3f7a5c"],
  ["--cc-act-on", "#f7f4ee", "#ffffff"],
  ["--cc-on", "#ffffff", "#ffffff"],
  ["--cc-confirm", "#2a7a49", "#2a7a49"],
  ["--cc-confirm-ink", "#226a3e", "#74cf98"],
  ["--cc-confirm-mark", "#2a7a49", "#74cf98"],
  ["--cc-link", "#1f66b0", "#1f66b0"],
  ["--cc-link-ink", "#1b5c9f", "#8fbcf0"],
  ["--cc-link-mark", "#1f66b0", "#8fbcf0"],
  ["--cc-choose", "#9e6206", "#9e6206"],
  ["--cc-choose-ink", "#8a5a12", "#e9bb52"],
  ["--cc-choose-mark", "#9e6206", "#e9bb52"],
  ["--cc-nomatch", "#b0432f", "#b0432f"],
  ["--cc-nomatch-ink", "#ad3b29", "#f39079"],
  ["--cc-nomatch-mark", "#b0432f", "#f39079"],
  ["--cc-art-tile", "#dcebe0", "#151d18"],
  ["--cc-art-sky", "#f3ecdf", "#24232a"],
  ["--cc-art-sun", "#f0d79f", "#c7a65e"],
  ["--cc-art-hill1", "#a9d2b6", "#2f5d46"],
  ["--cc-art-hill3", "#253e33", "#17291f"],
  ["--cc-art-sheet", "#fffcf7", "#2c2b32"],
  ["--cc-art-sheet-line", "#d9d1c3", "#4a4852"],
  ["--cc-art-sheet-head", "#253e33", "#8f8c8b"],
] as const;

function declaredProperties(source: string): Set<string> {
  return new Set(
    Array.from(source.matchAll(/^\s*(--cc-[\w-]+)\s*:/gm), (match) => match[1]),
  );
}

function declarationValues(source: string): Record<string, string> {
  return Object.fromEntries(
    Array.from(
      source.matchAll(/^\s*(--cc-[\w-]+)\s*:\s*([^;]+);/gm),
      (match) => [match[1], match[2].trim()],
    ),
  );
}

function referencedVariables(source: string): string[] {
  return [
    ...Array.from(
      source.matchAll(/var\(\s*(--[\w-]+)(?:\s*,[^)]*)?\)/g),
      (match) => match[1],
    ),
    ...Array.from(
      source.matchAll(/\b[a-z][\w-]*-\((?:[\w-]+:)?(--[\w-]+)\)/g),
      (match) => match[1],
    ),
  ];
}

function lucideImportClauses(source: string): string[] {
  return Array.from(
    source.matchAll(/import\s+([^;]+?)\s+from\s+["']lucide-react["']\s*;?/g),
    (match) => match[1].trim(),
  );
}

describe("control-centre palette boundary", () => {
  it("85: declares every K1 palette value in light and dark", () => {
    expect(lightRootMatch).not.toBeNull();
    expect(darkPaletteMatch).not.toBeNull();
    const light = declarationValues(lightRootMatch?.[1] ?? "");
    const dark = {
      ...light,
      ...declarationValues(darkPaletteMatch?.[1] ?? ""),
    };
    const expectedVariables = k1Palette.map(([variable]) => variable).sort();
    expect(Object.keys(light).sort()).toEqual(expectedVariables);
    expect(Object.keys(dark).sort()).toEqual(expectedVariables);

    for (const [variable, lightValue, darkValue] of k1Palette) {
      expect(light[variable], `${variable} light`).toBe(lightValue);
      expect(dark[variable], `${variable} dark`).toBe(darkValue);
    }
  });

  it("gives every non-button border inside Done today the hairline", () => {
    expect(palette).toMatch(
      /\.cc-done :is\(\.border, \.border-t\):not\(button\)\s*\{\s*border-color: var\(--cc-hair\);\s*\}/,
    );
  });

  it("declares every control-centre custom property used by components", () => {
    const declared = declaredProperties(lightPalette);
    const used = new Set(
      components
        .flatMap(({ source }) => referencedVariables(source))
        .filter((property) => property.startsWith("--cc-")),
    );
    expect(used.size).toBeGreaterThan(10);
    for (const property of used)
      expect(declared.has(property), property).toBe(true);
  });

  it("has the operative-dark selector and every theme-dependent K2 property", () => {
    const dark = darkPaletteMatch?.[1];
    expect(dark).toBeDefined();
    const darkDeclarations = declaredProperties(dark ?? "");
    const artProperties = Array.from(declaredProperties(palette)).filter(
      (property) => property.startsWith("--cc-art-"),
    );
    expect(artProperties.length).toBeGreaterThan(0);
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
      ...artProperties,
    ])
      expect(darkDeclarations.has(property), property).toBe(true);
  });

  it("keeps literals and unsupported colour utilities out of components", () => {
    const family =
      /(?<![\w-])(?:bg|text|border|ring|divide|fill|stroke)-(?:card|muted|muted-foreground|foreground|background|popover|primary|secondary|accent|destructive|border|input)(?![\w-])/;
    const namedColour =
      /(?<![a-z-])[a-z][a-z-]*-(?:white|black|(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3})(?:\/(?:[a-z\d.%_-]+|\[[^\]\s]+\]))?(?![\w/-])/;
    const literalColour =
      /#[\da-f]{3,8}|\b(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb|color)\(|-\[(?:white|black|red|blue|green|yellow|orange|purple|pink|gray|grey|silver|gold|brown|cyan|magenta|navy|teal|maroon|olive|lime|aqua|fuchsia)\]/i;
    for (const { name, source } of colourFiles) {
      expect(source, name).not.toMatch(literalColour);
      expect(source, name).not.toMatch(family);
      expect(source, name).not.toMatch(namedColour);
    }
    for (const { name, source } of components) {
      expect(source, name).not.toContain("dark:");
      expect(source, name).not.toMatch(/\$\{[^}]*var\(/);
      const variables = referencedVariables(source);
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

  it("keeps lucide out of glyphs and limits other control-centre imports to Loader2", () => {
    const glyph = components.find(({ name }) => name === "CcGlyph.tsx");
    expect(glyph?.source).not.toMatch(
      /(?:from\s+|import\s*)["']lucide-react["']/,
    );
    for (const { name, source } of components) {
      if (name === "CcGlyph.tsx") continue;
      for (const clause of lucideImportClauses(source))
        expect(clause, name).toMatch(/^\{\s*Loader2\s*,?\s*\}$/);
    }
  });
});
