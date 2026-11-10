import { describe, expect, it } from "vitest";

import VisaOracleUnlockLayout from "./layout";

// robots and canonical are asserted in ../canonical.guard.test.ts
describe("visa-oracle/unlock layout", () => {
  it("innocence: children still render through the layout", () => {
    expect(
      VisaOracleUnlockLayout({
        children: "unlock child" as unknown as React.ReactNode,
      }),
    ).toBe("unlock child");
  });
});
