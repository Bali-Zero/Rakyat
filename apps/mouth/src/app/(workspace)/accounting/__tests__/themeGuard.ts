/**
 * Theme guard for the accounting folder: two purely textual rules.
 *
 * G1 NAMES. A guarded file fails when its text contains `ROOT-NAME` (ROOT in
 *    ROOTS, NAME in FAMILY) with the character before it not in [A-Za-z0-9_-]
 *    (or start of text) and the character after it not in [A-Za-z0-9_-] (or
 *    end of text), wherever it sits: class, comment, any string.
 * G2 VARIABLES. A guarded file fails when its text references a custom
 *    property that no stylesheet reachable from globals.css declares.
 *
 * Variable names are carried WITHOUT the leading `--`.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

/**
 * The colour utility roots of Tailwind CSS 4.3.3 (51), printed by
 * scratchpad/audit-guard/roots.mjs, which asks Tailwind itself which roots
 * accept a colour. Sorted; matching tries them longest-first.
 */
export const ROOTS: readonly string[] = [
  "accent",
  "bg",
  "border",
  "border-b",
  "border-be",
  "border-bs",
  "border-e",
  "border-l",
  "border-r",
  "border-s",
  "border-t",
  "border-x",
  "border-y",
  "caret",
  "decoration",
  "divide",
  "drop-shadow",
  "fill",
  "from",
  "inset-ring",
  "inset-shadow",
  "mask-b-from",
  "mask-b-to",
  "mask-conic-from",
  "mask-conic-to",
  "mask-l-from",
  "mask-l-to",
  "mask-linear-from",
  "mask-linear-to",
  "mask-r-from",
  "mask-r-to",
  "mask-radial-from",
  "mask-radial-to",
  "mask-t-from",
  "mask-t-to",
  "mask-x-from",
  "mask-x-to",
  "mask-y-from",
  "mask-y-to",
  "outline",
  "placeholder",
  "ring",
  "ring-offset",
  "scrollbar-thumb",
  "scrollbar-track",
  "shadow",
  "stroke",
  "text",
  "text-shadow",
  "to",
  "via",
];

export const FAMILY: readonly string[] = [
  "card",
  "card-foreground",
  "popover",
  "popover-foreground",
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

const longestFirst = (xs: readonly string[]) =>
  [...xs].sort((a, b) => b.length - a.length);

const NAME_RE = new RegExp(
  `(?<![A-Za-z0-9_-])(?:${longestFirst(ROOTS).join("|")})-(?:${longestFirst(FAMILY).join("|")})(?![A-Za-z0-9_-])`,
  "g",
);

const REF_RES = [
  /var\(\s*--([A-Za-z0-9_-]+)/g,
  /[-/]\((?:[A-Za-z-]+:)?--([A-Za-z0-9_-]+)/g,
];

const lineOf = (text: string, index: number) =>
  text.slice(0, index).split("\n").length;

/** G1: every ROOT-NAME hit, in text order, with its 1-based line. */
export function nameHits(text: string): { line: number; match: string }[] {
  return [...text.matchAll(NAME_RE)].map((m) => ({
    line: lineOf(text, m.index!),
    match: m[0],
  }));
}

/** G2: every variable reference (name without `--`), in text order. */
export function variableRefs(text: string): { line: number; name: string }[] {
  const refs: { index: number; name: string }[] = [];
  for (const re of REF_RES) {
    for (const m of text.matchAll(re)) {
      refs.push({ index: m.index!, name: m[1] });
    }
  }
  return refs
    .sort((a, b) => a.index - b.index)
    .map((r) => ({ line: lineOf(text, r.index), name: r.name }));
}

/** Names declared by ONE stylesheet text (comments removed first). */
export function declaredVariables(css: string): Set<string> {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return new Set(
    [...bare.matchAll(/(?<![^\s{;])--([A-Za-z0-9_-]+)\s*:/g)].map((m) => m[1]),
  );
}

/**
 * Declared names of a stylesheet and every one reachable by an @import whose
 * specifier starts with `./` or `../` (read literally). Bare specifiers are
 * never followed. Termination comes from the visited set; the catch wraps
 * only the file read, so a missing or unreadable file is skipped.
 */
export function declaredInTree(
  entry: string,
  read: (file: string) => string = (f) => readFileSync(f, "utf8"),
): Set<string> {
  const declared = new Set<string>();
  const visited = new Set<string>();
  const walk = (file: string) => {
    if (visited.has(file)) return;
    visited.add(file);
    let css: string;
    try {
      css = read(file);
    } catch {
      return;
    }
    for (const n of declaredVariables(css)) declared.add(n);
    const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
    for (const m of bare.matchAll(
      /@import\s+(?:url\(\s*)?["'](\.{1,2}\/[^"']*)["']/g,
    )) {
      walk(resolve(dirname(file), m[1]));
    }
  };
  walk(entry);
  return declared;
}

const EXT = /\.(?:ts|tsx|js|jsx|mjs|cjs|mts|cts)$/;

/** Guarded files under `dir`, at any depth, sorted. */
export function guardedFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .sort()
    .flatMap((name) => {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) {
        return name === "__tests__" ? [] : guardedFiles(p);
      }
      if (!EXT.test(name)) return [];
      if (name.includes(".test.") || name.includes(".spec.")) return [];
      return [p];
    });
}

/** `file:line match` for G1 and `file:line --NAME` for G2. */
export function reportFile(
  file: string,
  text: string,
  declared: Set<string>,
): string[] {
  return [
    ...nameHits(text).map((h) => `${file}:${h.line} ${h.match}`),
    ...variableRefs(text)
      .filter((r) => !declared.has(r.name))
      .map((r) => `${file}:${r.line} --${r.name}`),
  ];
}
