import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Kita heading font contract (owner decision 2026-11-03): IBM Plex Sans 500
 * for headings and numerals on kita, Manrope body, Fraunces untouched on the
 * client portal (my) and the website.
 *
 * The heading token keeps its historical name --font-serif; only the kita
 * theme blocks re-point it. Each detector is a pure function run against a
 * planted violation (guilt) and against the real files (innocence).
 */
const APP = join(__dirname, "..", "..");
const read = (...p: string[]) => readFileSync(join(APP, ...p), "utf8");

const globalsCss = read("globals.css");
const fontsCss = read("portal", "r19-fonts.css");

export function themeBlock(
  css: string,
  theme: string,
  product: string,
): string {
  const match = css.match(
    new RegExp(
      `\\[data-theme="${theme}"\\]\\[data-product="${product}"\\]\\s*\\{([\\s\\S]*?)\\n\\}`,
    ),
  );
  if (!match) throw new Error(`${product} ${theme} block is absent`);
  return match[1];
}

export function headingFamily(block: string): string | null {
  const m = block.match(/--font-serif:\s*"([^"]+)"/);
  return m ? m[1] : null;
}

export function plexFaces(css: string): { weight: string; display: string }[] {
  const out: { weight: string; display: string }[] = [];
  for (const m of css.matchAll(/@font-face\s*\{([^}]*)\}/g)) {
    if (!/font-family:\s*"IBM Plex Sans"/.test(m[1])) continue;
    out.push({
      weight: (m[1].match(/font-weight:\s*([^;]+);/) ?? [])[1]?.trim() ?? "",
      display: (m[1].match(/font-display:\s*([^;]+);/) ?? [])[1]?.trim() ?? "",
    });
  }
  return out;
}

export function usesOpticalAxes(src: string): boolean {
  return /fontVariationSettings|font-variation-settings/.test(src);
}

describe("kita heading token", () => {
  for (const theme of ["operative-light", "operative-dark"]) {
    it(`${theme}: kita resolves --font-serif to IBM Plex Sans`, () => {
      expect(headingFamily(themeBlock(globalsCss, theme, "kita"))).toBe(
        "IBM Plex Sans",
      );
    });
    it(`${theme}: my (portal) still resolves --font-serif to Fraunces`, () => {
      expect(headingFamily(themeBlock(globalsCss, theme, "my"))).toBe(
        "Fraunces",
      );
    });
    it(`${theme}: kita body stays Manrope`, () => {
      expect(themeBlock(globalsCss, theme, "kita")).toContain(
        '--font-sans: "Manrope"',
      );
    });
  }

  it("detector guilt: a block that still says Fraunces is caught", () => {
    expect(headingFamily('  --font-serif: "Fraunces", Georgia, serif;')).toBe(
      "Fraunces",
    );
    expect(headingFamily("  --color: red;")).toBeNull();
  });
});

describe("IBM Plex Sans faces", () => {
  it("declares exactly 400/500/600 with display swap", () => {
    expect(plexFaces(fontsCss)).toEqual([
      { weight: "400", display: "swap" },
      { weight: "500", display: "swap" },
      { weight: "600", display: "swap" },
    ]);
  });

  it("every declared file exists in public/fonts", () => {
    for (const w of ["400", "500", "600"]) {
      expect(
        existsSync(
          join(
            APP,
            "..",
            "..",
            "public",
            "fonts",
            `ibm-plex-sans-${w}-latin.woff2`,
          ),
        ),
      ).toBe(true);
    }
  });

  it("detector guilt: a 700 face or a missing swap would be caught", () => {
    const planted =
      '@font-face { font-family: "IBM Plex Sans"; font-weight: 700; }';
    expect(plexFaces(planted)).toEqual([{ weight: "700", display: "" }]);
    expect(plexFaces('@font-face { font-family: "Manrope"; }')).toEqual([]);
  });
});

describe("no Fraunces optical axes left on kita", () => {
  const kitaFiles: string[][] = [
    ["..", "components", "workspace", "r19", "tokens.ts"],
    ["..", "components", "workspace", "AppSidebar.tsx"],
    ["login", "page.tsx"],
    ["login", "error.tsx"],
    ["(workspace)", "garuda-voa", "r19.tsx"],
  ];
  for (const f of kitaFiles) {
    it(`${f.slice(-2).join("/")} has no font-variation-settings`, () => {
      expect(usesOpticalAxes(read(...f))).toBe(false);
    });
  }

  it("detector guilt: opsz on a heading is caught", () => {
    expect(usesOpticalAxes(`{ fontVariationSettings: '"opsz" 144' }`)).toBe(
      true,
    );
  });

  it("garuda-voa re-scope points at Plex, not Fraunces", () => {
    const src = read("(workspace)", "garuda-voa", "layout.tsx");
    expect(src).toContain('"--font-serif": \'"IBM Plex Sans"');
    expect(src).not.toMatch(/"--font-serif":[^\n]*Fraunces/);
  });
});
