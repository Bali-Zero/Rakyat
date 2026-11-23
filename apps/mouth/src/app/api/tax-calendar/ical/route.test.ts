import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";
import { getUpcomingTaxDeadlines } from "../deadlines";

const NOW = new Date("2026-03-10T12:00:00Z");

function nextDay(ymd: string): string {
  const d = new Date(
    Date.UTC(+ymd.slice(0, 4), +ymd.slice(4, 6) - 1, +ymd.slice(6, 8) + 1),
  );
  return d.toISOString().slice(0, 10).replace(/-/g, "");
}

describe("tax calendar iCal feed", () => {
  const originalTz = process.env.TZ;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
    if (originalTz === undefined) delete process.env.TZ;
    else process.env.TZ = originalTz;
  });

  for (const tz of ["America/Los_Angeles", "Asia/Tokyo", "UTC"]) {
    it(`writes each deadline as the same all-day date in ${tz}`, async () => {
      process.env.TZ = tz;
      const expected = getUpcomingTaxDeadlines(new Date(), 12);
      expect(expected.length).toBeGreaterThan(0);

      const body = await (await GET()).text();
      const lines = body.split("\r\n");
      const starts = lines.filter((l) => l.startsWith("DTSTART"));
      const ends = lines.filter((l) => l.startsWith("DTEND"));

      expect(starts).toEqual(
        expected.map(
          (d) => `DTSTART;VALUE=DATE:${d.date.slice(0, 10).replace(/-/g, "")}`,
        ),
      );
      expect(ends).toEqual(
        expected.map(
          (d) =>
            `DTEND;VALUE=DATE:${nextDay(d.date.slice(0, 10).replace(/-/g, ""))}`,
        ),
      );
      expect(body).not.toMatch(/T000000Z/);
    });
  }
});
