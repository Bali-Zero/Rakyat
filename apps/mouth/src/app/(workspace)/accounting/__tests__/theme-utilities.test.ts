/**
 * Folder guard: the accounting sources may only use colours this app's theme
 * defines. The shadcn-style names below are declared nowhere, so a utility
 * built on them paints nothing; a var(--x) with no declaration paints nothing
 * either.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, it, expect } from "vitest";

const ACCOUNTING = resolve(__dirname, "..");
const GLOBALS = resolve(process.cwd(), "src/app/globals.css");

const UNDEFINED_COLOURS = [
  "card",
  "popover",
  "background",
  "foreground",
  "muted",
  "muted-foreground",
  "primary",
  "primary-foreground",
  "secondary",
  "secondary-foreground",
  "destructive",
  "destructive-foreground",
  "input",
  "ring",
];
const UTILITY = new RegExp(
  `^(?:[\\w-]+:)*(?:bg|text|border|border-[trblxy]|ring|divide|outline|fill|stroke|from|to|via|placeholder|decoration|caret|accent|shadow)-(${UNDEFINED_COLOURS.join("|")})(?:/[\\d.]+)?$`,
);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      return name === "__tests__" ? [] : sources(p);
    }
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [p] : [];
  });
}

function cssTree(file: string, seen = new Set<string>()): string {
  if (seen.has(file)) return "";
  seen.add(file);
  let text: string;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    return "";
  }
  const imports = [...text.matchAll(/@import\s+["'](\.[^"']+)["']/g)].map((m) =>
    resolve(dirname(file), m[1]),
  );
  return text + "\n" + imports.map((f) => cssTree(f, seen)).join("\n");
}

describe("accounting theme utilities", () => {
  const files = sources(ACCOUNTING);

  it("finds the accounting sources", () => {
    expect(files.length).toBeGreaterThan(4);
  });

  it("uses no colour utility the theme does not define", () => {
    const offenders: string[] = [];
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      for (const tok of text.split(/[\s"'`{}()<>=,;]+/)) {
        if (UTILITY.test(tok)) {
          offenders.push(`${f.replace(ACCOUNTING, "")}: ${tok}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("references only CSS variables that are declared", () => {
    const css = cssTree(GLOBALS);
    const declared = new Set(
      [...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]),
    );
    expect(declared.size).toBeGreaterThan(50);
    const offenders: string[] = [];
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      for (const m of text.matchAll(/var\((--[\w-]+)/g)) {
        if (!declared.has(m[1])) {
          offenders.push(`${f.replace(ACCOUNTING, "")}: ${m[1]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
