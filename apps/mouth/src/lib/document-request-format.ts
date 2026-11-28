/** Small formatters shared by the reviewers' page and the practice's Required documents card. */

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** "28 Nov", from the ISO string, without a date library. */
export function shortDate(iso: string | null): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!match) return "";
  return `${Number(match[3])} ${MONTHS[Number(match[2]) - 1] ?? ""}`;
}

/**
 * "28 Nov": the calendar day and short English month of that instant in `timeZone`
 * (the viewer's own zone when it is not given); "" when the string is not a date.
 */
export function dayMonth(iso: string | null, timeZone?: string): string {
  const date = new Date(iso ?? "");
  if (Number.isNaN(date.getTime())) return "";
  // en-US: its short months are three letters all year ("Sep", where en-GB writes "Sept"),
  // the same spelling `shortDate` gives.
  const parts = new Intl.DateTimeFormat("en-US", {
    day: "numeric",
    month: "short",
    timeZone,
  }).formatToParts(date);
  const day = parts.find((part) => part.type === "day")?.value ?? "";
  const month = parts.find((part) => part.type === "month")?.value ?? "";
  return `${day} ${month}`;
}

/** The part of an address before the "@", first letter in upper case. */
export function nameOf(address: string | null): string {
  const local = (address ?? "").split("@")[0];
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : "";
}
