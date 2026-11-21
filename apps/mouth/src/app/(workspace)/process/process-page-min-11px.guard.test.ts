import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The /process board (process/page.tsx) rendered 7 labels at 9px: the
 * urgent/high priority badges, the payment-status pill, the expiry
 * countdown badges, the "last updated" age badge and the "passate" divider
 * label. Measured at 3e76a31b. They now sit at 11px, the floor
 * /kbli-explorer and the portal company page already hold.
 *
 * Declared limits: reads page.tsx as text and checks un-prefixed arbitrary
 * `text-[9px]` only. It does not render, does not cover the 10px labels in
 * the same file (a separate slice of the systemic text-[9|10]px task), and
 * does not cover process/[id] or process/new.
 */
const SRC = readFileSync(join(__dirname, "page.tsx"), "utf8");
const NINE_PX = /(?<![\w:-])text-\[9px\]/g;

describe("process board holds the 11px floor for its former 9px labels", () => {
  it("positive control: page.tsx is read and carries arbitrary px text sizes", () => {
    expect(SRC).toContain("passate");
    expect([...SRC.matchAll(/text-\[\d+px\]/g)].length).toBeGreaterThan(0);
  });

  it("no 9px text", () => {
    expect([...SRC.matchAll(NINE_PX)].map((m) => m[0])).toEqual([]);
  });

  it("the probe fires on the size it forbids, not on prefixed variants", () => {
    const probe = 'className="text-[9px] md:text-[9px] text-[10px]"';
    expect([...probe.matchAll(NINE_PX)].map((m) => m[0])).toEqual([
      "text-[9px]",
    ]);
  });
});
