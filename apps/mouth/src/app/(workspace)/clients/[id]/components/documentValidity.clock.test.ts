/**
 * Obligation 4, static half: one clock rule.
 *
 * In the document and practice validity modules the browser clock is
 * forbidden: no `Date.now()`, no `new Date()` with no argument, no
 * `86400000`. The only readers of the clock on the validity path are the
 * WitaClock module and `overview-brief.ts`, which this test does not scan.
 *
 * Every other read of the clock in the folder is outside the spec. The ones
 * that sit inside a scanned file are named below, with their reason, so a NEW
 * read of the clock in a covered module still fails.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = __dirname;

/** The document and practice validity modules (spec obligation 4). */
const COVERED = [
  "DocumentsTab.tsx",
  "ImmigrationTab.tsx",
  "FamilyTab.tsx",
  "VisaCard.tsx",
  "PassportCard.tsx",
  "ValidityTrack.tsx",
  "OverviewTab.tsx",
  "ProcessTab.tsx",
  "utils.ts",
  "attention.ts",
  "pickCurrentVisa.ts",
  "tax-deadlines.ts",
  "TaxTab.tsx",
  // The new validity modules obey the same rule.
  "documentValidity.ts",
  "familyValidity.ts",
  "RemovedSignpost.tsx",
];

/**
 * Reads of the clock that are NOT document dates, inside a covered file.
 * The key is the trimmed source line; the value is why it stays.
 */
const EXCLUDED: Record<string, Record<string, string>> = {
  "FamilyTab.tsx": {
    "(Date.now() - new Date(dateOfBirth).getTime()) / (365.25 * 86400000),":
      "FamilyTab.tsx age from a date of birth, not a document date",
  },
  "ProcessTab.tsx": {
    "(Date.now() - new Date(practice.updated_at!).getTime()) /":
      "ProcessTab.tsx days since updated_at, not an expiry",
    "86400000,": "the divisor of the days-since-updated_at line above",
  },
  "utils.ts": {
    "const today = new Date();":
      "utils.ts isBirthdayToday, a birthday and not a document date",
  },
};

const FORBIDDEN = [/Date\.now\(/, /new Date\(\s*\)/, /86400000/];

/** Source with block comments and line comments blanked, line numbers kept. */
function code(source: string): string[] {
  const noBlocks = source.replace(/\/\*[\s\S]*?\*\//g, (m) =>
    m.replace(/[^\n]/g, " "),
  );
  return noBlocks.split("\n").map((line) => line.replace(/(^|\s)\/\/.*$/, ""));
}

/** Lines of `source` that read the clock, as [lineNumber, trimmedText]. */
export function clockReads(source: string): Array<[number, string]> {
  const reads: Array<[number, string]> = [];
  code(source).forEach((line, i) => {
    if (FORBIDDEN.some((re) => re.test(line))) reads.push([i + 1, line.trim()]);
  });
  return reads;
}

describe("one clock rule: no browser-clock read in the validity modules", () => {
  it.each(COVERED)("%s reads no clock except the named exclusions", (file) => {
    const source = readFileSync(join(DIR, file), "utf8");
    const allowed = EXCLUDED[file] ?? {};
    const unexplained = clockReads(source).filter(([, text]) => !allowed[text]);
    expect(unexplained).toEqual([]);
  });

  it("every named exclusion still exists, so a stale one cannot hide a new read", () => {
    for (const [file, entries] of Object.entries(EXCLUDED)) {
      const reads = clockReads(readFileSync(join(DIR, file), "utf8")).map(
        ([, text]) => text,
      );
      for (const text of Object.keys(entries)) {
        expect(reads).toContain(text);
      }
    }
  });

  it("the scanner itself: it flags a clock read and ignores a comment", () => {
    expect(clockReads("const a = Date.now();")).toHaveLength(1);
    expect(clockReads("const a = new Date();")).toHaveLength(1);
    expect(clockReads("const d = x / 86400000;")).toHaveLength(1);
    expect(clockReads("// Date.now() in a comment")).toHaveLength(0);
    expect(clockReads("/* new Date() in a block\n86400000 */")).toHaveLength(0);
    expect(clockReads("const d = new Date(value);")).toHaveLength(0);
  });

  it("the WitaClock module is where the browser clock is read, and only that and overview-brief", () => {
    expect(
      clockReads(readFileSync(join(DIR, "witaClock.tsx"), "utf8")).length,
    ).toBeGreaterThan(0);
  });
});
