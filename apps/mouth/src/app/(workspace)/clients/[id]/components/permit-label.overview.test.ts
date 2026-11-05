import { describe, expect, it } from "vitest";
import { cleanPermitLabel, permitDisplay } from "./permit-label";
import { permitLine } from "./header-facts";
import type { ClientDocument } from "@/lib/api/crm/crm.types";

describe("cleanPermitLabel", () => {
  it("drops parenthesised qualifiers", () => {
    expect(cleanPermitLabel("Investor KITAS (Standard)")).toBe(
      "Investor KITAS",
    );
    expect(cleanPermitLabel("Visit Visa (B211A) (Extension)")).toBe(
      "Visit Visa",
    );
  });
  it("is empty for nothing and leaves a clean label alone", () => {
    expect(cleanPermitLabel(null)).toBe("");
    expect(cleanPermitLabel(undefined)).toBe("");
    expect(cleanPermitLabel("Investor KITAS")).toBe("Investor KITAS");
  });
});

describe("permitDisplay", () => {
  it("code + label with a leading code and a qualifier", () => {
    expect(permitDisplay("E28A", "E28A — Investor KITAS (Standard)")).toBe(
      "E28A · Investor KITAS",
    );
  });
  it("no code: just the clean label", () => {
    expect(permitDisplay(undefined, "Investor KITAS (Standard)")).toBe(
      "Investor KITAS",
    );
    expect(permitDisplay("  ", "Investor KITAS")).toBe("Investor KITAS");
  });
  it("label without the code prefix is joined with a dot", () => {
    expect(permitDisplay("E28A", "Investor KITAS")).toBe(
      "E28A · Investor KITAS",
    );
  });
  it("code only when the label is empty or is the code", () => {
    expect(permitDisplay("E28A", "")).toBe("E28A");
    expect(permitDisplay("E28A", null)).toBe("E28A");
    expect(permitDisplay("E28A", "E28A")).toBe("E28A");
  });
});

describe("the header permit line uses it", () => {
  it("never shows a qualifier", () => {
    const doc = {
      id: 1,
      client_id: 1,
      document_type: "kitas",
      document_category: "immigration",
      expiry_date: "2030-01-01",
      permit_code: "E28A",
      permit_label: "E28A — Investor KITAS (Standard)",
    } as ClientDocument;
    expect(permitLine([doc])).toBe("E28A · Investor KITAS");
  });
});
