import { describe, expect, it } from "vitest";
import {
  ccD,
  ccDayDate,
  ccFull,
  ccInv,
  ccMon,
  ccShort,
} from "../_components/ccFormat";

describe("control-centre date formats", () => {
  it("formats valid date prefixes without using the local time zone", () => {
    expect(ccD("2026-11-16T23:00:00-08:00")).toBe("16");
    expect(ccMon("2026-11-16")).toBe("Nov");
    expect(ccShort("2026-11-16")).toBe("16 Nov");
    expect(ccDayDate("2026-11-16")).toBe("Mon 16 Nov");
    expect(ccFull("2026-11-16")).toBe("Mon 16 Nov 2026");
  });

  it("uses the short invoice date only in the transfer year", () => {
    expect(ccInv("2026-11-06", "2026-11-16")).toBe("6 Nov");
    expect(ccInv("2025-11-06", "2026-11-16")).toBe("6 Nov 2025");
  });

  it("shows invalid strings raw and missing values as empty", () => {
    expect(ccD("2026-02-30")).toBe("2026-02-30");
    expect(ccMon("not-a-date")).toBe("");
    expect(ccShort("not-a-date")).toBe("not-a-date");
    expect(ccFull(null)).toBe("");
  });
});
