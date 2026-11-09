import { describe, expect, it } from "vitest";
import { cleanPermitLabel, permitDisplay } from "./permit-label";

describe("cleanPermitLabel", () => {
  it("drops parenthesised qualifiers such as (Standard) and tidies spaces", () => {
    expect(cleanPermitLabel("E28A — Investor KITAS (Standard)")).toBe(
      "E28A — Investor KITAS",
    );
    expect(cleanPermitLabel("D12  (a) Pre-Investment (Multiple Entry)")).toBe(
      "D12 Pre-Investment",
    );
  });
  it("INNOCENCE: a label without qualifiers is unchanged; empty input gives ''", () => {
    expect(cleanPermitLabel("Working KITAS")).toBe("Working KITAS");
    expect(cleanPermitLabel(null)).toBe("");
    expect(cleanPermitLabel(undefined)).toBe("");
  });
});

describe("permitDisplay", () => {
  it("reads '<code> · <popular name>' and strips the code a label leads with", () => {
    expect(permitDisplay("E28A", "E28A — Investor KITAS (Standard)")).toBe(
      "E28A · Investor KITAS",
    );
    expect(permitDisplay("e28a", "E28A - Investor KITAS")).toBe(
      "E28A · Investor KITAS".replace("E28A", "e28a"),
    );
  });
  it("a label that does not lead with the code keeps it whole after the code", () => {
    expect(permitDisplay("C31", "Visit visa")).toBe("C31 · Visit visa");
  });
  it("no code gives the clean label; code only gives the code; regex characters in a code are literal", () => {
    expect(permitDisplay("", "Working KITAS (Standard)")).toBe("Working KITAS");
    expect(permitDisplay("E28A", "")).toBe("E28A");
    expect(permitDisplay("E28A", "E28A")).toBe("E28A");
    expect(permitDisplay("A.1", "A.1 — Name")).toBe("A.1 · Name");
    expect(permitDisplay("A.1", "AX1 — Name")).toBe("A.1 · AX1 — Name");
  });
});
