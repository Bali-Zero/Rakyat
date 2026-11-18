// Guard: the funnel "pricing" doors point at /services/<funnel>, never at a
// dead /pricing. Measured 12 Nov 2026: balizero.com/pricing answered 404 and
// tax.balizero.com/pricing answered 200 "Page not found"; the public pages
// that carry the packages are /services/<funnel>. Moved here from the retired
// FunnelFeature.pricing-href test, keeping the checks that guard surviving code.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { SERVICES_DATA } from "./services_data";

describe("every funnel pricing door has a /services page behind it", () => {
  it.each([["company"], ["property"], ["visa"], ["tax"]] as const)(
    "SERVICES_DATA has the %s key",
    (key) => {
      expect(Object.keys(SERVICES_DATA)).toContain(key);
    },
  );
});

describe("no source file links to a dead /pricing", () => {
  const SRC = join(__dirname, "..");
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(tsx?|jsx?)$/.test(name) && !/\.test\./.test(name))
        files.push(p);
    }
  };
  walk(SRC);

  it("finds no href to balizero.com/pricing or bare /pricing", () => {
    expect(files.length).toBeGreaterThan(100);
    const offenders = files.filter((f) =>
      /["'`](https?:\/\/[a-z.]*balizero\.com)?\/pricing\/?["'`]/.test(
        readFileSync(f, "utf8"),
      ),
    );
    expect(offenders).toEqual([]);
  });
});
