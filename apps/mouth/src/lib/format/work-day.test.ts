import { describe, expect, it } from "vitest";
import { WORK_DAY_TIME_ZONE, firstDayOfMonth, workDayOf } from "./work-day";

// The answers below do not depend on the zone of the machine that runs them:
// the formatter names its own zone.
describe("the team's work day", () => {
  it("is a day in Bali", () => {
    expect(WORK_DAY_TIME_ZONE).toBe("Asia/Makassar");
  });

  it.each([
    ["2026-11-20T15:59:59Z", "2026-11-20", "a second before midnight in Bali"],
    ["2026-11-20T16:00:00Z", "2026-11-21", "midnight in Bali"],
    [
      "2026-11-20T22:00:00Z",
      "2026-11-21",
      "06:00 in Bali, still the 20th in UTC",
    ],
    ["2026-11-21T07:00:00Z", "2026-11-21", "15:00 in Bali"],
    ["2026-10-31T17:00:00Z", "2026-11-01", "01:00 on the first of the month"],
    ["2026-12-31T16:30:00Z", "2027-01-01", "00:30 on New Year's Day"],
    ["2028-02-28T16:00:00Z", "2028-02-29", "a leap day"],
  ])("%s is %s (%s)", (instant, day) => {
    expect(workDayOf(new Date(instant))).toBe(day);
  });

  it("starts a month on its first day", () => {
    expect(firstDayOfMonth("2026-11-21")).toBe("2026-11-01");
    expect(firstDayOfMonth("2026-11-01")).toBe("2026-11-01");
    expect(firstDayOfMonth("2027-01-01")).toBe("2027-01-01");
  });
});
