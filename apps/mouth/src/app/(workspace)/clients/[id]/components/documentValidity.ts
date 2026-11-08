import { formatLongSpan } from "@/lib/utils/format-date";

/**
 * One validity model for the kita client card (DOCUMENT_VALIDITY_SPEC.md).
 * Pure: every function takes the card's day (`YYYY-MM-DD`, the `as_of` of the
 * profile payload) and never reads a clock.
 */

/** Same threshold the visa card uses for its red state: two months. */
export const EXPIRY_WARNING_DAYS = 60;

/** The Documents / Immigration / Start Renewal window. */
export const URGENT_WINDOW_DAYS = 90;

export type ValidityState =
  "valid" | "expiring" | "expired" | "undated" | "void";

export type VoidReason = "removed" | "archived" | "rejected";

export type AlertLevel = "green" | "yellow" | "red" | "expired";

/**
 * What the validity model reads from a document. Wider than `ClientDocument`
 * on purpose: the wire can send `null` for each field, and Rule L treats null
 * as live. `ClientDocument` is assignable to it.
 */
export interface DocumentValidityInput {
  deleted_at?: string | null;
  is_archived?: boolean | null;
  status?: string | null;
  issue_date?: string | null;
  expiry_date?: string | null;
}

export interface DateValidity {
  state: Exclude<ValidityState, "void">;
  expiry: string | null;
  daysLeft: number | null;
}

export interface DocumentValidity {
  live: boolean;
  state: ValidityState;
  voidReason: VoidReason | null;
  expiry: string | null;
  daysLeft: number | null;
}

// ---------------------------------------------------------------------------
// Rule D: the backend's date grammar (`_as_date`, client_attention.py:97-108)
// ---------------------------------------------------------------------------

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

const isLeap = (y: number): boolean =>
  (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/**
 * A value is a date when it is a string whose first 10 characters are an ISO
 * `YYYY-MM-DD` real calendar date. It is used as written, with no timezone
 * conversion, whatever follows it. Anything else is null.
 */
export function parseCalendarDate(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const m = ISO_DAY.exec(value.slice(0, 10));
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  if (y < 1 || mo < 1 || mo > 12 || d < 1) return null;
  const max = mo === 2 && isLeap(y) ? 29 : DAYS_IN_MONTH[mo - 1];
  return d <= max ? m[0] : null;
}

/**
 * Whole days since 1970-01-01 of a `YYYY-MM-DD` key. Pure calendar
 * arithmetic: no `Date`, so years 0..99 are not remapped to the 1900s.
 */
export function dayNumber(key: string): number {
  const [y, m, d] = key.split("-").map(Number);
  const yy = m <= 2 ? y - 1 : y;
  const era = Math.floor(yy / 400);
  const yoe = yy - era * 400;
  const mp = (m + 9) % 12;
  const doy = Math.floor((153 * mp + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** Whole calendar days from `day` to the date; null when either is no date. */
export function daysBetween(value: unknown, day: string): number | null {
  const key = parseCalendarDate(value);
  const today = parseCalendarDate(day);
  if (!key || !today) return null;
  return dayNumber(key) - dayNumber(today);
}

const stateOf = (daysLeft: number | null): DateValidity["state"] => {
  if (daysLeft === null) return "undated";
  if (daysLeft <= 0) return "expired";
  if (daysLeft <= EXPIRY_WARNING_DAYS) return "expiring";
  return "valid";
};

/** The state table applied to a bare value. It has no liveness. */
export function dateValidity(value: unknown, day: string): DateValidity {
  const daysLeft = daysBetween(value, day);
  return {
    state: stateOf(daysLeft),
    expiry: daysLeft === null ? null : parseCalendarDate(value),
    daysLeft,
  };
}

// ---------------------------------------------------------------------------
// Rule L: liveness, identical to the backend `_usable` (client_attention.py:214-219)
// ---------------------------------------------------------------------------

export type LivenessFields = Pick<
  DocumentValidityInput,
  "deleted_at" | "is_archived" | "status"
>;

/**
 * Why a document is void, or null when it is held. The first test that fires
 * gives the reason. No trimming; `archived`, `expired`, `pending`, NULL and
 * unknown statuses are all live.
 */
export function voidReasonOf(doc: LivenessFields): VoidReason | null {
  if (doc.deleted_at !== null && doc.deleted_at !== undefined) return "removed";
  if (doc.is_archived) return "archived";
  const status = String(doc.status ?? "").toLowerCase();
  if (status === "rejected") return "rejected";
  if (status === "deleted") return "removed";
  return null;
}

/** May be current, filed, on file, counted, renewed. */
export const isLive = (doc: LivenessFields): boolean =>
  voidReasonOf(doc) === null;

export function documentValidity(
  doc: DocumentValidityInput,
  day: string,
): DocumentValidity {
  const reason = voidReasonOf(doc);
  const dated = dateValidity(doc.expiry_date, day);
  return {
    live: reason === null,
    state: reason === null ? dated.state : "void",
    voidReason: reason,
    expiry: dated.expiry,
    daysLeft: dated.daysLeft,
  };
}

export const VOID_LABEL: Record<VoidReason, string> = {
  removed: "Removed",
  archived: "Archived",
  rejected: "Rejected",
};

// ---------------------------------------------------------------------------
// Wording, urgency, enums
// ---------------------------------------------------------------------------

/**
 * The one wording ladder. `short` is the documents form ("Expired 3d ago",
 * "⏰ 62d left"); `long` is the permit-card form ("62 days left"). The
 * classification is the same in both: day 0 is expired.
 */
export function expiryWording(
  daysLeft: number,
  form: "short" | "long" = "short",
): string {
  if (daysLeft < 0) {
    return form === "short"
      ? `Expired ${-daysLeft}d ago`
      : `Expired ${-daysLeft} ${daysLeft === -1 ? "day" : "days"} ago`;
  }
  if (daysLeft === 0) return "Expired today";
  if (daysLeft <= 365) {
    return form === "short"
      ? `⏰ ${daysLeft}d left`
      : `${daysLeft} ${daysLeft === 1 ? "day" : "days"} left`;
  }
  return `${formatLongSpan(daysLeft)} left`;
}

const ENUM_URGENT: ReadonlySet<string> = new Set(["red", "yellow", "expired"]);

/** `alert_color` / `visa_alert` / `passport_alert` that adds urgency. */
export const isEnumUrgent = (alert: string | null | undefined): boolean =>
  alert != null && ENUM_URGENT.has(alert);

/**
 * The urgency formula, one for every document surface:
 * `live && (expired || expiring || daysLeft <= window || enumUrgent)`.
 */
export function isUrgent(
  v: Pick<DocumentValidity, "live" | "state" | "daysLeft">,
  window: number,
  alert?: string | null,
): boolean {
  return (
    v.live &&
    (v.state === "expired" ||
      v.state === "expiring" ||
      (v.daysLeft !== null && v.daysLeft <= window) ||
      isEnumUrgent(alert))
  );
}

const ALERT_RANK: Record<AlertLevel, number> = {
  green: 0,
  yellow: 1,
  red: 2,
  expired: 3,
};

const levelOfState = (state: ValidityState): AlertLevel | null => {
  if (state === "expired") return "expired";
  if (state === "expiring") return "red";
  if (state === "valid") return "green";
  return null;
};

/**
 * The worse of an enum and the model's answer, in the order expired, red,
 * yellow, green (model expiring = red, valid = green). An enum may add
 * urgency or turn a label worse, never better.
 */
export function worseLevel(
  alert: AlertLevel | null | undefined,
  state: ValidityState,
): AlertLevel | null {
  const model = levelOfState(state);
  if (!alert) return model;
  if (!model) return alert;
  return ALERT_RANK[alert] > ALERT_RANK[model] ? alert : model;
}

// ---------------------------------------------------------------------------
// The date formatter
// ---------------------------------------------------------------------------

/**
 * "14 Mar 2027" of a date under the grammar, the same calendar day in every
 * browser timezone. A null date renders as "—".
 */
export function formatCalendarDate(value: unknown): string {
  const key = parseCalendarDate(value);
  if (!key) return "—";
  const [y, m, d] = key.split("-").map(Number);
  const at = new Date(0);
  at.setUTCFullYear(y, m - 1, d);
  return at.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/**
 * The card's `formatDate`: a date-only value goes through
 * `formatCalendarDate`; an instant (upload time, removal time) keeps the
 * browser's local formatting. Before mount it is the SSR placeholder.
 */
export function formatCardDate(dateStr: string, mounted: boolean): string {
  if (!dateStr) return "";
  if (!mounted) return "...";
  if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr) && parseCalendarDate(dateStr))
    return formatCalendarDate(dateStr);
  return new Date(dateStr).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
