import { describe, expect, it } from "vitest";
import { coretaxStatus, formatNpwp, isSixteenDigits } from "./coretax";

describe("formatNpwp", () => {
  it("groups 16 bare digits 4-4-4-4", () => {
    expect(formatNpwp("1234567890123456")).toBe("1234 5678 9012 3456");
  });
  it("leaves anything else exactly as stored", () => {
    expect(formatNpwp("11.222.333.4-555.000")).toBe("11.222.333.4-555.000");
    expect(formatNpwp("123456789012345")).toBe("123456789012345");
    expect(formatNpwp("12345678901234567")).toBe("12345678901234567");
    expect(formatNpwp("SYNTHETIC-TAX-0001")).toBe("SYNTHETIC-TAX-0001");
  });
  it("isSixteenDigits is exact", () => {
    expect(isSixteenDigits("1234567890123456")).toBe(true);
    expect(isSixteenDigits("123456789012345")).toBe(false);
    expect(isSixteenDigits(undefined)).toBe(false);
  });
});

describe("coretaxStatus", () => {
  it("nothing on file", () => {
    expect(coretaxStatus([{ document_type: "passport" }])).toEqual({
      coretaxAccess: { onFile: false, date: null },
      efin: { onFile: false, date: null },
    });
  });
  it("reads each document type independently, newest date wins", () => {
    const s = coretaxStatus([
      { document_type: "coretax_access", created_at: "2026-01-05" },
      { document_type: "coretax_access", issue_date: "2026-02-10" },
      { document_type: "efin", created_at: "2026-03-01" },
    ]);
    expect(s.coretaxAccess).toEqual({ onFile: true, date: "2026-02-10" });
    expect(s.efin).toEqual({ onFile: true, date: "2026-03-01" });
    expect(
      coretaxStatus([{ document_type: "efin" }]).coretaxAccess.onFile,
    ).toBe(false);
  });
  it("archived or client-removed documents do not count", () => {
    const s = coretaxStatus([
      { document_type: "coretax_access", is_archived: true },
      { document_type: "efin", deleted_at: "2026-01-01" },
    ]);
    expect(s.coretaxAccess.onFile).toBe(false);
    expect(s.efin.onFile).toBe(false);
  });
});
