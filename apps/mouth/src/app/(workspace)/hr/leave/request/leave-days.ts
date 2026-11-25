function parseCalendarDate(value: string): Date | null {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!parts) return null;

  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const date = new Date(Date.UTC(year, month - 1, day));

  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null;
  }

  return date;
}

export function countWorkingDays(start: string, end: string): number {
  const startDate = parseCalendarDate(start);
  const endDate = parseCalendarDate(end);
  if (!startDate || !endDate || endDate < startDate) return 0;

  let total = 0;
  const date = new Date(startDate);
  while (date <= endDate) {
    const day = date.getUTCDay();
    if (day !== 0 && day !== 6) total += 1;
    date.setUTCDate(date.getUTCDate() + 1);
  }

  return total;
}
