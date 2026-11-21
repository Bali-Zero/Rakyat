import { describe, expect, it } from "vitest";
import {
  validateManualBonus,
  witaToday,
  type ManualBonusDraft,
} from "./manual-bonus-form";

const OK: ManualBonusDraft = {
  employeeId: "7",
  amount: "150000",
  reason: "Weekend filing",
  serviceCode: "",
  awardedOn: "2026-11-10",
};

describe("validateManualBonus", () => {
  it("accepts a complete draft and the boundaries", () => {
    expect(validateManualBonus(OK)).toEqual({});
    expect(
      validateManualBonus({ ...OK, amount: "100000000", reason: "abc" }),
    ).toEqual({});
  });

  it.each([
    ["no person", { employeeId: "" }, "employeeId"],
    ["empty amount", { amount: "" }, "amount"],
    ["zero amount", { amount: "0" }, "amount"],
    ["negative amount", { amount: "-5" }, "amount"],
    ["decimal amount", { amount: "10.5" }, "amount"],
    ["amount over the cap", { amount: "100000001" }, "amount"],
    ["short reason", { reason: "ab" }, "reason"],
    ["blank reason", { reason: "     " }, "reason"],
    ["long reason", { reason: "x".repeat(301) }, "reason"],
    ["no date", { awardedOn: "" }, "awardedOn"],
  ])("rejects %s", (_name, patch, field) => {
    expect(Object.keys(validateManualBonus({ ...OK, ...patch }))).toEqual([
      field,
    ]);
  });
});

describe("witaToday", () => {
  it("is the WITA calendar day, not the UTC one", () => {
    // 17:00 UTC on the 10th is already 01:00 on the 11th in Bali.
    expect(witaToday(new Date("2026-11-10T17:00:00Z"))).toBe("2026-11-11");
    expect(witaToday(new Date("2026-11-10T15:59:00Z"))).toBe("2026-11-10");
  });
});
