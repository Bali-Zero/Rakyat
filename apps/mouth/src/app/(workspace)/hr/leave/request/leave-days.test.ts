import { afterEach, describe, expect, it, vi } from "vitest";
import { countWorkingDays } from "./leave-days";

// The test runner cannot move the process time zone after start, so the
// clock-change case is pinned by construction: the count must never read the
// browser's local clock.
const LOCAL_CLOCK_READERS = [
  "getDay",
  "getDate",
  "getMonth",
  "getFullYear",
  "getHours",
  "getTimezoneOffset",
  "setDate",
  "setHours",
] as const;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("countWorkingDays", () => {
  it("counts the days around the autumn clock change from calendar dates", () => {
    expect(countWorkingDays("2026-10-23", "2026-10-26")).toBe(2);
    expect(countWorkingDays("2026-03-27", "2026-03-30")).toBe(2);
  });

  it("never reads the local clock, so the browser's zone cannot move the count", () => {
    for (const method of LOCAL_CLOCK_READERS) {
      vi.spyOn(Date.prototype, method).mockImplementation(() => {
        throw new Error(`local clock read: ${method}`);
      });
    }

    expect(countWorkingDays("2026-10-19", "2026-11-06")).toBe(15);
  });

  it("excludes weekends from complete and weekend-only ranges", () => {
    expect(countWorkingDays("2026-11-23", "2026-11-29")).toBe(5);
    expect(countWorkingDays("2026-11-28", "2026-11-29")).toBe(0);
  });

  it("counts inclusive weekday ranges", () => {
    expect(countWorkingDays("2026-11-23", "2026-11-23")).toBe(1);
    expect(countWorkingDays("2026-11-23", "2026-11-27")).toBe(5);
    expect(countWorkingDays("2026-11-23", "2026-12-04")).toBe(10);
    expect(countWorkingDays("2026-12-28", "2027-01-05")).toBe(7);
  });

  it.each([
    ["end before start", "2026-11-24", "2026-11-23"],
    ["empty start", "", "2026-11-23"],
    ["empty end", "2026-11-23", ""],
    ["malformed date", "2026-02-30", "2026-03-02"],
  ])("returns zero for %s", (_name, start, end) => {
    expect(countWorkingDays(start, end)).toBe(0);
  });
});
