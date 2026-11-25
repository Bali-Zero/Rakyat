import { describe, it, expect } from "vitest";
import { toIcalString, type IcalEvent } from "./ical";

describe("toIcalString", () => {
  it("emits a valid VCALENDAR with VEVENT entries", () => {
    const events: IcalEvent[] = [
      {
        uid: "deadline-pph-2026-05-15",
        summary: "PPh 25 — Maggio",
        start: new Date("2026-05-15T00:00:00Z"),
        end: new Date("2026-05-15T23:59:00Z"),
        description: "Pagamento PPh 25 mensile",
      },
    ];
    const out = toIcalString(events, { prodId: "BaliZero//TaxCalendar" });
    expect(out).toContain("BEGIN:VCALENDAR");
    expect(out).toContain("PRODID:BaliZero//TaxCalendar");
    expect(out).toContain("BEGIN:VEVENT");
    expect(out).toContain("UID:deadline-pph-2026-05-15");
    expect(out).toContain("SUMMARY:PPh 25 — Maggio");
    expect(out).toContain("DTSTART:20260515T000000Z");
    expect(out).toContain("END:VCALENDAR");
  });
});

describe("toIcalString all-day events", () => {
  const opts = { prodId: "BaliZero//Test" };

  it("writes an all-day event on the given date and the next day, with no time", () => {
    const out = toIcalString(
      [{ uid: "a", summary: "Due", allDayDate: "2026-05-31" }],
      opts,
    );
    expect(out).toContain("DTSTART;VALUE=DATE:20260531");
    expect(out).toContain("DTEND;VALUE=DATE:20260601");
    expect(out).not.toMatch(/DT(START|END):/);
  });

  it("rolls over month and year ends and leap days", () => {
    const out = toIcalString(
      [
        { uid: "a", summary: "x", allDayDate: "2026-12-31" },
        { uid: "b", summary: "y", allDayDate: "2028-02-28" },
      ],
      opts,
    );
    expect(out).toContain("DTEND;VALUE=DATE:20270101");
    expect(out).toContain("DTEND;VALUE=DATE:20280229");
  });

  it("is independent of the process time zone", () => {
    const original = process.env.TZ;
    try {
      for (const tz of ["America/Los_Angeles", "Asia/Tokyo"]) {
        process.env.TZ = tz;
        const out = toIcalString(
          [{ uid: "a", summary: "x", allDayDate: "2026-05-15" }],
          opts,
        );
        expect(out).toContain("DTSTART;VALUE=DATE:20260515");
        expect(out).toContain("DTEND;VALUE=DATE:20260516");
      }
    } finally {
      if (original === undefined) delete process.env.TZ;
      else process.env.TZ = original;
    }
  });

  it("keeps timed events unchanged next to all-day ones", () => {
    const out = toIcalString(
      [
        {
          uid: "t",
          summary: "Timed",
          start: new Date("2026-05-15T09:30:00Z"),
          end: new Date("2026-05-15T10:30:00Z"),
        },
        { uid: "d", summary: "Day", allDayDate: "2026-05-15" },
      ],
      opts,
    );
    expect(out).toContain("DTSTART:20260515T093000Z");
    expect(out).toContain("DTEND:20260515T103000Z");
    expect(out).toContain("DTSTART;VALUE=DATE:20260515");
  });
});
