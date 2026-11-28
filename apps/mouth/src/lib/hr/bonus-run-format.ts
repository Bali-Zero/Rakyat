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

/** "2026-12-25" -> "25 Dec 2026"; also reads a full timestamp's date part. */
export function bonusDayLabel(iso: string): string {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
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
