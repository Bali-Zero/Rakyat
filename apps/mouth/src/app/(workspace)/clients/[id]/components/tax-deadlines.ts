// SPT annual-return deadlines for the kita Tax tab — pure date logic.
//
// Owner rule (2026-11-03): the personal SPT closes 31 March and the company
// SPT closes 30 April, both for fiscal year Y-1 where Y is the current year.
// A backend `attention` feed with the same rules is built in parallel (kinds
// `spt_personal` / `spt_company`, states expired|overdue|urgent|open|upcoming|
// done); the state names below are the same on purpose so the two can be
// reconciled. Calendar days are WITA (UTC+8) days, and `now` is injectable.

export type SptKind = "spt_personal" | "spt_company";

/**
 * The states the SPT rule produces (owner's rule, same as the backend engine):
 * `upcoming` 30..11 days before the deadline, `urgent` the last 10 days
 * (deadline day included), `overdue` only for the 30 days after it, `done`
 * when the fiscal year's filing is on file, `none` everywhere else (quiet).
 */
export type SptState = "upcoming" | "urgent" | "overdue" | "done" | "none";

/** Every state the backend attention feed can carry (kinds other than SPT use all). */
export type AttentionState =
  "expired" | "overdue" | "urgent" | "open" | "upcoming" | "done";

export const SPT_DEADLINES: Record<SptKind, { month: number; day: number }> = {
  spt_personal: { month: 3, day: 31 },
  spt_company: { month: 4, day: 30 },
};

/** Document types that count as a filed SPT of each kind. */
export const SPT_DOCUMENT_TYPES: Record<SptKind, readonly string[]> = {
  spt_personal: ["spt_personal"],
  spt_company: ["spt", "spt_company"],
};

export const COUNTDOWN_DAYS = 30;
export const URGENT_DAYS = 10;
export const OVERDUE_DAYS = 30;

const WITA_OFFSET_MS = 8 * 3_600_000;
const DAY_MS = 86_400_000;
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

/** The WITA calendar date of an instant, as UTC-midnight milliseconds. */
function witaDay(now: Date): { year: number; ms: number } {
  const shifted = new Date(now.getTime() + WITA_OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    ms: Date.UTC(
      shifted.getUTCFullYear(),
      shifted.getUTCMonth(),
      shifted.getUTCDate(),
    ),
  };
}

export function formatDayMonth(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** "31 Mar 2027" — a full date, year included. */
export function formatFullDate(ms: number): string {
  return `${formatDayMonth(ms)} ${new Date(ms).getUTCFullYear()}`;
}

export interface SptStatus {
  kind: SptKind;
  /**
   * The fiscal year the line is about: the current WITA year - 1, or the NEXT
   * one (current year) once this year's window has closed unfiled.
   */
  fiscalYear: number;
  /** True when the line points at the next filing (window closed unfiled). */
  isNext: boolean;
  /** The past fiscal year left unfiled after its window, else null. */
  missingYear: number | null;
  state: SptState;
  /** Calendar days to the deadline (negative once passed). */
  daysToDeadline: number;
  /** Deadline of the fiscal year in `fiscalYear`, "31 Mar" style. */
  deadlineLabel: string;
  /** First day of the countdown with its year, "1 Mar 2027" style. */
  opensLabel: string;
  /** Operator-facing line; always carries a date. */
  label: string;
}

/** The one wording for a state, shared by the helper and the attention path. */
export function sptLabel(
  state: SptState | AttentionState,
  days: number,
  deadlineLabel: string,
  opensLabel: string,
): string {
  const plural = (n: number) => `${n} day${n === 1 ? "" : "s"}`;
  switch (state) {
    case "done":
      return "Filed";
    case "overdue":
      return `Overdue by ${plural(Math.abs(days))} · ${deadlineLabel}`;
    case "urgent":
      return days === 0
        ? `Closes ${deadlineLabel} · today`
        : `Closes ${deadlineLabel} · ${plural(days)}`;
    case "open":
    case "upcoming":
      return `Due in ${plural(days)} · ${deadlineLabel}`;
    default:
      return `Next filing opens ${opensLabel}`;
  }
}

/** "2026-03-31" (or a full ISO timestamp) -> UTC-midnight ms of that date. */
export function isoDayMs(iso: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : NaN;
}

export function sptStatus(
  kind: SptKind,
  filedFiscalYears: ReadonlySet<number>,
  now: Date = new Date(),
): SptStatus {
  const today = witaDay(now);
  const fiscalYear = today.year - 1;
  const rule = SPT_DEADLINES[kind];
  const deadlineMs = Date.UTC(today.year, rule.month - 1, rule.day);
  const days = Math.round((deadlineMs - today.ms) / DAY_MS);
  let state: SptState;
  if (filedFiscalYears.has(fiscalYear)) state = "done";
  else if (days > COUNTDOWN_DAYS) state = "none";
  else if (days > URGENT_DAYS) state = "upcoming";
  else if (days >= 0) state = "urgent";
  else if (days >= -OVERDUE_DAYS) state = "overdue";
  else state = "none";

  // Window closed with nothing filed: point at the next filing (next year's
  // deadline), and keep the unfiled past year for the history list.
  const isNext = state === "none" && days < 0;
  const shownDeadlineMs = isNext
    ? Date.UTC(today.year + 1, rule.month - 1, rule.day)
    : deadlineMs;
  const deadlineLabel = formatDayMonth(shownDeadlineMs);
  const opensLabel = formatFullDate(shownDeadlineMs - COUNTDOWN_DAYS * DAY_MS);
  const label = isNext
    ? `opens ${opensLabel}`
    : sptLabel(state, days, deadlineLabel, opensLabel);
  return {
    kind,
    fiscalYear: isNext ? today.year : fiscalYear,
    isNext,
    missingYear: isNext ? fiscalYear : null,
    state,
    daysToDeadline: isNext
      ? Math.round((shownDeadlineMs - today.ms) / DAY_MS)
      : days,
    deadlineLabel,
    opensLabel,
    label,
  };
}

// ---------------------------------------------------------------------------
// Filed documents
// ---------------------------------------------------------------------------

export interface SptDocumentLike {
  document_type: string;
  fiscal_year?: number | null;
  file_name?: string;
  file_url?: string;
  google_drive_file_url?: string;
  company_id?: number | null;
  is_archived?: boolean;
  deleted_at?: string | null;
}

/**
 * The fiscal year a filing document belongs to: the explicit `fiscal_year`
 * when the API returns it, else a 20xx year written in the file name. A
 * document whose year cannot be read is never counted as a filing.
 */
export function documentFiscalYear(doc: SptDocumentLike): number | null {
  if (typeof doc.fiscal_year === "number" && Number.isFinite(doc.fiscal_year))
    return doc.fiscal_year;
  const m = /(?<!\d)(20\d{2})(?!\d)/.exec(doc.file_name ?? "");
  return m ? Number(m[1]) : null;
}

export interface SptFiling {
  fiscalYear: number;
  url: string | null;
  fileName: string | null;
}

/** One filing per fiscal year (newest first) for the given kind. */
export function sptFilings(
  docs: readonly SptDocumentLike[],
  kind: SptKind,
): SptFiling[] {
  const types = SPT_DOCUMENT_TYPES[kind];
  const byYear = new Map<number, SptFiling>();
  for (const d of docs) {
    if (!types.includes(d.document_type) || d.is_archived || d.deleted_at)
      continue;
    const fiscalYear = documentFiscalYear(d);
    if (fiscalYear == null) continue;
    const url = d.google_drive_file_url || d.file_url || null;
    const prev = byYear.get(fiscalYear);
    if (!prev || (!prev.url && url))
      byYear.set(fiscalYear, {
        fiscalYear,
        url,
        fileName: d.file_name ?? null,
      });
  }
  return [...byYear.values()].sort((a, b) => b.fiscalYear - a.fiscalYear);
}
