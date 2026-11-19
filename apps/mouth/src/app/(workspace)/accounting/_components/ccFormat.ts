const DATE_START = /^(\d{4})-(\d{2})-(\d{2})/;
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
] as const;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

type DateParts = { year: number; month: number; day: number };

function parts(value: string | null | undefined): DateParts | null {
  if (!value) return null;
  const match = DATE_START.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  )
    return null;
  return { year, month, day };
}

function raw(value: string | null | undefined): string {
  return value ?? "";
}

export function ccD(value: string | null | undefined): string {
  const p = parts(value);
  return p ? String(p.day) : raw(value);
}

export function ccMon(value: string | null | undefined): string {
  const p = parts(value);
  return p ? MONTHS[p.month - 1] : "";
}

export function ccShort(value: string | null | undefined): string {
  const p = parts(value);
  return p ? `${p.day} ${MONTHS[p.month - 1]}` : raw(value);
}

export function ccDayDate(value: string | null | undefined): string {
  const p = parts(value);
  if (!p) return raw(value);
  const weekday =
    DAYS[new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay()];
  return `${weekday} ${p.day} ${MONTHS[p.month - 1]}`;
}

export function ccFull(value: string | null | undefined): string {
  const p = parts(value);
  return p ? `${ccDayDate(value)} ${p.year}` : raw(value);
}

export function ccInv(
  value: string | null | undefined,
  transferDate: string | null | undefined,
): string {
  const p = parts(value);
  if (!p) return raw(value);
  const transfer = parts(transferDate);
  return transfer?.year === p.year
    ? ccShort(value)
    : `${ccShort(value)} ${p.year}`;
}

export function ccHasDate(value: string | null | undefined): boolean {
  return parts(value) !== null;
}
