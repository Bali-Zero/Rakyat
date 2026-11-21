// Guard: the toast stack's z-index token must exist. toast.tsx positions the
// stack with z-[var(--z-toast)], but no stylesheet defined --z-toast (measured
// 12 Nov 2026), so the declaration was invalid at computed time and the stack
// fell back to z-index: auto, under every z-50 dialog and z-40 sticky bar.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const read = (p: string) => readFileSync(path.resolve(__dirname, p), "utf8");

describe("--z-toast token", () => {
  it("toast.tsx still reads the token", () => {
    expect(read("./toast.tsx")).toContain("z-[var(--z-toast)]");
  });

  it("is defined in globals.css :root above the dialog layer (z-50)", () => {
    const css = read("../../app/globals.css");
    const m = /:root\s*\{[^}]*--z-toast:\s*(\d+)\s*;/.exec(css);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBeGreaterThan(50);
  });
});
