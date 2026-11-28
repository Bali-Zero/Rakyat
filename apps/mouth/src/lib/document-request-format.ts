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

/** The part of an address before the "@", first letter in upper case. */
export function nameOf(address: string | null): string {
  const local = (address ?? "").split("@")[0];
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : "";
}
