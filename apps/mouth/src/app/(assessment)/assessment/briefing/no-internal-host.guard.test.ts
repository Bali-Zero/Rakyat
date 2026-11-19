import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * /assessment/briefing is served without login (noindex only), so anything in
 * its HTML is public. The architecture diagram used to name the internal
 * workspace host; it now uses a generic label instead.
 */
const source = readFileSync(path.join(__dirname, "page.tsx"), "utf8");

describe("assessment briefing — no internal host names", () => {
  it("reads the real page source (positive control)", () => {
    expect(source).toContain("Frontend Next.js");
  });

  it("does not print the internal workspace hostname", () => {
    expect(source).not.toMatch(/kita\.balizero\.com/i);
  });

  it("labels that box entry generically", () => {
    expect(source).toContain("- internal workspace");
  });
});
