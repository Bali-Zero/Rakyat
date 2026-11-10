import { describe, expect, it } from "vitest";

import VisaOracleUnlockLayout, { metadata } from "./layout";

describe("visa-oracle/unlock layout metadata", () => {
  it("guilt: noindex and nofollow are explicit, as the parent sets them", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });

  it("canonical is the page itself, not /visa-oracle", () => {
    expect(metadata.alternates?.canonical).toBe(
      "https://balizero.com/visa-oracle/unlock",
    );
  });

  it("innocence: children still render through the layout", () => {
    expect(
      VisaOracleUnlockLayout({
        children: "unlock child" as unknown as React.ReactNode,
      }),
    ).toBe("unlock child");
  });
});
