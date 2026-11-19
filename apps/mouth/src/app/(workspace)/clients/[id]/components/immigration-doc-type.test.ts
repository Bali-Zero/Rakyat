import { describe, expect, it } from "vitest";

import { isImmigrationDocType } from "./immigration-doc-type";

describe("isImmigrationDocType", () => {
  it.each([
    "kitas",
    "KITAP",
    "kitas_c317",
    "visa c1",
    "e-visa",
    "evisa",
    "e_visa",
    "visa_sponsor_letter",
    "voa",
    "e-voa",
    "imta",
    "rptka",
    "stay_permit",
    "Work Permit",
    "working permit",
    "exit permit",
    "re-entry permit",
    "reentry_permit",
    "limited stay permit",
  ])("%s is an immigration document type", (t) => {
    expect(isImmigrationDocType(t)).toBe(true);
  });

  it.each([
    "building_permit",
    "Business Permit",
    "environmental permit",
    "permit",
    "devisa transfer receipt",
    "laporan_devisa",
    "advisory letter",
    "passport",
    "npwp",
    "",
  ])("%s is not an immigration document type", (t) => {
    expect(isImmigrationDocType(t)).toBe(false);
  });

  it("handles a missing type", () => {
    expect(isImmigrationDocType(undefined)).toBe(false);
    expect(isImmigrationDocType(null)).toBe(false);
  });
});
