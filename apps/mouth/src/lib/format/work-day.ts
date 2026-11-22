/**
 * The team's work days are civil days in Bali (WITA). The backend files a
 * timesheet row under the Asia/Makassar date and reads "today" there, so a day
 * sent to it has to be that same day, whatever zone the browser is in. It is
 * never taken from `toISOString()`, which is the UTC day: in Bali that is
 * yesterday until 08:00, and the UTC day of a local midnight is the day before.
 */
export const WORK_DAY_TIME_ZONE = "Asia/Makassar";

const workDayFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: WORK_DAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** The work day (`YYYY-MM-DD`) an instant falls on; now, when none is given. */
export function workDayOf(instant: Date = new Date()): string {
  const parts = workDayFormatter.formatToParts(instant);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** The first day (`YYYY-MM-DD`) of the month a work day belongs to. */
export function firstDayOfMonth(workDay: string): string {
  return `${workDay.slice(0, 7)}-01`;
}
