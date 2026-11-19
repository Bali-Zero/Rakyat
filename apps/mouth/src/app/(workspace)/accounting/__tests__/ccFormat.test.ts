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
    expect(ccD("not-a-date"), "D: invalid string").toBe("not-a-date");
    expect(ccD("2026-02-30"), "D: impossible date").toBe("2026-02-30");
    expect(ccD(null), "D: null").toBe("");
    expect(ccD(undefined), "D: undefined").toBe("");

    expect(ccMon("not-a-date"), "MON: invalid string").toBe("");
    expect(ccMon("2026-02-30"), "MON: impossible date").toBe("");
    expect(ccMon(null), "MON: null").toBe("");
    expect(ccMon(undefined), "MON: undefined").toBe("");

    expect(ccShort("not-a-date"), "SHORT: invalid string").toBe("not-a-date");
    expect(ccShort("2026-02-30"), "SHORT: impossible date").toBe("2026-02-30");
    expect(ccShort(null), "SHORT: null").toBe("");
    expect(ccShort(undefined), "SHORT: undefined").toBe("");

    expect(ccDayDate("not-a-date"), "DAYDATE: invalid string").toBe(
      "not-a-date",
    );
    expect(ccDayDate("2026-02-30"), "DAYDATE: impossible date").toBe(
      "2026-02-30",
    );
    expect(ccDayDate(null), "DAYDATE: null").toBe("");
    expect(ccDayDate(undefined), "DAYDATE: undefined").toBe("");

    expect(ccFull("not-a-date"), "FULL: invalid string").toBe("not-a-date");
    expect(ccFull("2026-02-30"), "FULL: impossible date").toBe("2026-02-30");
    expect(ccFull(null), "FULL: null").toBe("");
    expect(ccFull(undefined), "FULL: undefined").toBe("");

    expect(ccInv("not-a-date", "2026-11-16"), "INV: invalid string").toBe(
      "not-a-date",
    );
    expect(ccInv("2026-02-30", "2026-11-16"), "INV: impossible date").toBe(
      "2026-02-30",
    );
    expect(ccInv(null, "2026-11-16"), "INV: null").toBe("");
    expect(ccInv(undefined, "2026-11-16"), "INV: undefined").toBe("");
  });
});
