/** Wording of a bonus run for the HR screens. Dates arrive as plain YYYY-MM-DD, so no time zone can move them. */

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/** "2026-11-01" -> "November 2026" (long) or "Nov 2026" (short). */
export function bonusMonthLabel(iso: string, short = false): string {
  const [year, month] = iso.split("-").map(Number);
  const name = MONTHS[month - 1] ?? "";
  return `${short ? name.slice(0, 3) : name} ${year}`;
}

const WITA_OFFSET_MS = 8 * 60 * 60 * 1000; // Asia/Makassar, UTC+8 all year (no daylight saving)

/** "2026-12-25" -> "25 Dec 2026" as written. A timestamp ("2026-12-25T16:30:00Z") is a moment: its day is the
 *  Bali day it fell on (26 Dec here), never the UTC one. */
export function bonusDayLabel(iso: string): string {
  let ymd = iso.slice(0, 10);
  if (iso.length > 10) {
    const moment = Date.parse(iso);
    if (!Number.isNaN(moment)) {
      ymd = new Date(moment + WITA_OFFSET_MS).toISOString().slice(0, 10);
    }
  }
  const [year, month, day] = ymd.split("-").map(Number);
  return `${day} ${MONTHS[month - 1]?.slice(0, 3) ?? ""} ${year}`;
}

/** The expected payday: shown only, never enforced. */
export function bonusPaydayText(
  expectedPayday: string | null,
  paydaySalaryMonth: string,
): string {
  return expectedPayday
    ? `Expected ${bonusDayLabel(expectedPayday)}`
    : `7 days after the ${bonusMonthLabel(paydaySalaryMonth, true)} salary is paid`;
}
