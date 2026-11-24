import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The /process board (process/page.tsx) rendered 7 labels at 9px: the
 * urgent/high priority badges, the payment-status pill, the expiry
 * countdown badges, the "last updated" age badge and the "passate" divider
 * label. Measured at 3e76a31b. They now sit at 11px, the floor
 * /kbli-explorer and the portal company page already hold. The 18 labels
 * that remained at 10px (measured at 1adaf23971) joined them.
 *
 * Declared limits: reads the board and its tile and history components as text. It checks un-prefixed arbitrary
 * `text-[9px]` and `text-[10px]` only. It does not render and does not
 * cover process/[id] or process/new.
 */
const SRC = [
  join(__dirname, "page.tsx"),
  join(__dirname, "../../../components/process/ProcessTile.tsx"),
  join(__dirname, "../../../components/process/GhostCard.tsx"),
]
  .map((path) => readFileSync(path, "utf8"))
  .join("\n");
const BELOW_FLOOR = /(?<![\w:-])text-\[(?:9|10)px\]/g;

describe("process board holds the 11px floor for its labels", () => {
  it("positive control: page.tsx is read and carries arbitrary px text sizes", () => {
    expect(SRC).toContain("Stage history");
    expect([...SRC.matchAll(/text-\[\d+px\]/g)].length).toBeGreaterThan(0);
  });

  it("no 9px or 10px text", () => {
    expect([...SRC.matchAll(BELOW_FLOOR)].map((m) => m[0])).toEqual([]);
  });

  it("the probe fires on the sizes it forbids, not on prefixed variants or the floor", () => {
    const probe =
      'className="text-[9px] md:text-[9px] text-[10px] md:text-[10px] text-[11px]"';
    expect([...probe.matchAll(BELOW_FLOOR)].map((m) => m[0])).toEqual([
      "text-[9px]",
      "text-[10px]",
    ]);
  });
});
