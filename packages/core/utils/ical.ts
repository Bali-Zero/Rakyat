interface IcalEventBase {
  uid: string;
  summary: string;
  description?: string;
}

export interface IcalTimedEvent extends IcalEventBase {
  start: Date;
  end: Date;
  allDayDate?: undefined;
}

/** An all-day event on a calendar date written as `YYYY-MM-DD`. The day is
 * used as given: no instant is converted to a zone. */
export interface IcalAllDayEvent extends IcalEventBase {
  allDayDate: string;
}

export type IcalEvent = IcalTimedEvent | IcalAllDayEvent;

export interface IcalOptions {
  prodId: string;
}

function icalDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    d.getUTCFullYear().toString() +
    pad(d.getUTCMonth() + 1) +
    pad(d.getUTCDate()) +
    "T" +
    pad(d.getUTCHours()) +
    pad(d.getUTCMinutes()) +
    pad(d.getUTCSeconds()) +
    "Z"
  );
}

function icalDay(ymd: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd);
  if (!m) throw new Error(`Invalid all-day date: ${ymd}`);
  return `${m[1]}${m[2]}${m[3]}`;
}

function icalNextDay(ymd: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd);
  if (!m) throw new Error(`Invalid all-day date: ${ymd}`);
  const next = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + 1));
  return icalDay(next.toISOString());
}

function escape(s: string): string {
  return s.replace(/[\\,;]/g, (c) => `\\${c}`).replace(/\n/g, "\\n");
}

export function toIcalString(events: IcalEvent[], opts: IcalOptions): string {
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    `PRODID:${opts.prodId}`,
    "CALSCALE:GREGORIAN",
  ];
  for (const e of events) {
    lines.push("BEGIN:VEVENT");
    lines.push(`UID:${e.uid}`);
    lines.push(`DTSTAMP:${icalDate(new Date())}`);
    if (e.allDayDate !== undefined) {
      lines.push(`DTSTART;VALUE=DATE:${icalDay(e.allDayDate)}`);
      lines.push(`DTEND;VALUE=DATE:${icalNextDay(e.allDayDate)}`);
    } else {
      lines.push(`DTSTART:${icalDate(e.start)}`);
      lines.push(`DTEND:${icalDate(e.end)}`);
    }
    lines.push(`SUMMARY:${escape(e.summary)}`);
    if (e.description) lines.push(`DESCRIPTION:${escape(e.description)}`);
    lines.push("END:VEVENT");
  }
  lines.push("END:VCALENDAR");
  return lines.join("\r\n");
}
