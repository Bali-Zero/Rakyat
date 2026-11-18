/**
 * Folder guard (G1 names, G3 no hit on the real files; rules in themeGuard.ts).
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { declaredInTree, guardedFiles, reportFile } from "./themeGuard";

const ACCOUNTING = resolve(__dirname, "..");
const GLOBALS = resolve(process.cwd(), "src/app/globals.css");

describe("accounting theme guard", () => {
  const declared = declaredInTree(GLOBALS);
  const files = guardedFiles(ACCOUNTING);

  it("reads the stylesheet tree", () => {
    expect(declared.size).toBeGreaterThan(50);
    expect(declared.has("bz-card")).toBe(true);
  });

  it("guards exactly the nine files that exist today", () => {
    expect(files.map((f) => f.slice(ACCOUNTING.length + 1))).toEqual([
      "_components/BankStatementUpload.tsx",
      "_components/ConfirmModal.tsx",
      "_components/LinkedIgnoredSection.tsx",
      "_components/ReconciliationInbox.tsx",
      "_components/formatStatementAmount.ts",
      "_components/useWriteLock.ts",
      "error.tsx",
      "loading.tsx",
      "page.tsx",
    ]);
  });

  it("G3: the guarded files produce no hit", () => {
    const hits = files.flatMap((f) =>
      reportFile(
        f.slice(ACCOUNTING.length + 1),
        readFileSync(f, "utf8"),
        declared,
      ),
    );
    expect(hits).toEqual([]);
  });

  it("reports hits as file:line match", () => {
    expect(
      reportFile(
        "planted.tsx",
        'a\n<div className="[&:hover]:bg-card/50 bg-[var(--not-declared-anywhere)]/20 bg-(--nope,red) bg-[var(--bz-card)]" />',
        declared,
      ),
    ).toEqual([
      "planted.tsx:2 bg-card",
      "planted.tsx:2 --not-declared-anywhere",
      "planted.tsx:2 --nope",
    ]);
  });
});
