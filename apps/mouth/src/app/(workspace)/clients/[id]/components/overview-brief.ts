import type {
  Client,
  ClientDocument,
  ClientProfile,
  Interaction,
} from "@/lib/api/crm/crm.types";
import { formatLongSpan } from "@/lib/utils/format-date";
import { pickCurrentVisa } from "./pickCurrentVisa";
import { pickCurrentPassport } from "./utils";
import {
  EXPIRY_WARNING_DAYS,
  dayNumber,
  parseCalendarDate,
  type ValidityState,
} from "./documentValidity";

export { EXPIRY_WARNING_DAYS };

/**
 * Pure derivations behind the Overview "brief" — every value comes from a
 * field the profile already carries; when the field is absent the fact says
 * so in words instead of guessing.
 */

type Practice = ClientProfile["practices"][number];

export type BriefTone = "neutral" | "warning";

const WITA_FORMAT = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Makassar",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** The business runs in Bali (WITA, UTC+8, no DST): "YYYY-MM-DD" of an instant there. */
export function witaDateKey(ms: number): string {
  return WITA_FORMAT.format(ms);
}

/**
 * Calendar date of a stored date under the backend grammar (`_as_date`): the
 * first 10 characters as written, no timezone conversion. Null for anything
 * that is not a real `YYYY-MM-DD`.
 */
function calendarKey(dateStr: string): string | null {
  return parseCalendarDate(dateStr);
}

const keyToDayNumber = dayNumber;

/**
 * Whole calendar days from the card's day (`YYYY-MM-DD`) to the date;
 * negative once past, null when either is not a date.
 */
export function daysUntil(dateStr: string | undefined | null, day: string) {
  const key = calendarKey(dateStr ?? "");
  const today = calendarKey(day);
  if (!key || !today) return null;
  return keyToDayNumber(key) - keyToDayNumber(today);
}

/** "kitas_card" -> "Kitas card" — formatting only, never invents a value. */
export function humanize(code: string | undefined | null): string {
  if (!code) return "";
  const s = code.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Value + unit for a span of days: "138" "days", "3y 10mo" "", "Expired" "". */
export function spanValue(
  days: number,
  state?: ValidityState,
): { value: string; unit: string } {
  if (days < 0 || state === "expired") return { value: "Expired", unit: "" };
  if (days === 0) return { value: "Today", unit: "" };
  if (days === 1) return { value: "1", unit: "day" };
  if (days <= 365) return { value: String(days), unit: "days" };
  return { value: formatLongSpan(days), unit: "" };
}

const VISA_PRACTICE =
  /kitas|kitap|itas|itap|visa|stay permit|e33|c31|c312|c313|c314|e28|e31/i;

export function isVisaPractice(p: Practice): boolean {
  return VISA_PRACTICE.test(`${p.practice_type_code} ${p.practice_type_name}`);
}

export interface PermitFact {
  kind: "none" | "undated" | "dated";
  label: string;
  days: number | null;
  expiry: string | null;
  /** Issue date when the document carries a real one, else null. */
  issued?: string | null;
  /** A visa/KITAS practice is open: a renewal or a first issuance, the data cannot tell. */
  openVisaPractice: Practice | null;
}

/**
 * Share of an issue→expiry window already used, 0..1, on WITA calendar days;
 * null when either date is missing/invalid or the window is not positive.
 */
export function elapsedShare(
  issued: string | null | undefined,
  expiry: string | null | undefined,
  day: string,
): number | null {
  if (!issued || !expiry) return null;
  const i = calendarKey(issued);
  const e = calendarKey(expiry);
  if (!i || !e) return null;
  const total = keyToDayNumber(e) - keyToDayNumber(i);
  if (total <= 0) return null;
  const today = calendarKey(day);
  if (!today) return null;
  const used = keyToDayNumber(today) - keyToDayNumber(i);
  return Math.min(Math.max(used / total, 0), 1);
}

/**
 * The practice lifecycle as prod's practice_status_log walks it
 * (inquiry -> waiting_documents -> sending_invoice -> on_process -> completed).
 */
export const PRACTICE_STAGES = [
  { key: "inquiry", label: "Inquiry" },
  { key: "waiting_documents", label: "Documents" },
  { key: "sending_invoice", label: "Invoice" },
  { key: "on_process", label: "In process" },
  { key: "completed", label: "Done" },
] as const;

const STAGE_ALIASES: Record<string, string> = { in_progress: "on_process" };

/** Index of a status on PRACTICE_STAGES, or -1 when it is not on the path. */
export function stageIndex(status: string | undefined | null): number {
  if (!status) return -1;
  const key = STAGE_ALIASES[status] ?? status;
  return PRACTICE_STAGES.findIndex((s) => s.key === key);
}

export function permitFact(
  documents: ClientDocument[],
  activePractices: Practice[],
  now: number,
): PermitFact {
  const doc = pickCurrentVisa(documents);
  const openVisaPractice = activePractices.find(isVisaPractice) ?? null;
  if (!doc)
    return {
      kind: "none",
      label: "",
      days: null,
      expiry: null,
      openVisaPractice,
    };
  const label = doc.permit_label || humanize(doc.document_type);
  const days = daysUntil(doc.expiry_date, witaDateKey(now));
  if (days === null)
    return {
      kind: "undated",
      label,
      days: null,
      expiry: null,
      openVisaPractice,
    };
  return {
    kind: "dated",
    label,
    days,
    expiry: doc.expiry_date ?? null,
    issued:
      doc.issue_date && calendarKey(doc.issue_date) ? doc.issue_date : null,
    openVisaPractice,
  };
}

export interface PassportFact {
  days: number | null;
  expiry: string | null;
  /** True only when BOTH dates are known and the passport ends first. */
  endsBeforePermit: boolean;
}

export function passportFact(
  client: Client,
  documents: ClientDocument[],
  permit: PermitFact,
  now: number,
): PassportFact {
  const expiry =
    client.passport_expiry ||
    pickCurrentPassport(documents)?.expiry_date ||
    null;
  const days = daysUntil(expiry, witaDateKey(now));
  const endsBeforePermit =
    days !== null &&
    permit.days !== null &&
    days >= 0 &&
    permit.days >= 0 &&
    days < permit.days;
  return { days, expiry: days === null ? null : expiry, endsBeforePermit };
}

/**
 * What is still owed: the price minus what was already paid, on every
 * unpaid/partial practice. The old tile summed the FULL price of a partially
 * paid practice, so a 14.5M job with 7M paid showed 14.5M owed.
 */
export function outstandingAmount(practices: Practice[]): number {
  return practices
    .filter(
      (p) =>
        (p.payment_status === "unpaid" || p.payment_status === "partial") &&
        !isBalanceUnknown(p),
    )
    .reduce((sum, p) => {
      const price = Number(p.actual_price ?? p.quoted_price ?? 0);
      const paid = Number(p.paid_amount ?? 0);
      const due = price - (Number.isFinite(paid) ? paid : 0);
      return sum + (Number.isFinite(due) && due > 0 ? due : 0);
    }, 0);
}

/**
 * A partially paid practice whose record carries no `paid_amount`: the balance
 * cannot be computed, and showing the full price as owed would be a lie. Such a
 * practice adds nothing to `outstandingAmount`; the UI says "balance unknown".
 */
export function isBalanceUnknown(p: Practice): boolean {
  return (
    p.payment_status === "partial" &&
    (p.paid_amount === undefined ||
      p.paid_amount === null ||
      !Number.isFinite(Number(p.paid_amount)))
  );
}

/** Total price of the active practices (actual, else quoted), string numerics coerced. */
export function pipelineAmount(active: Practice[]): number {
  return active.reduce((sum, p) => {
    const price = Number(p.actual_price ?? p.quoted_price ?? 0);
    return sum + (Number.isFinite(price) && price > 0 ? price : 0);
  }, 0);
}

const PRIORITY_RANK: Record<string, number> = { urgent: 0, high: 1 };

/** The practice to name first: urgent/high before the rest, then the most recently touched. */
export function leadPractice(active: Practice[]): Practice | null {
  if (active.length === 0) return null;
  return active.slice().sort((a, b) => {
    const pa = PRIORITY_RANK[a.priority ?? ""] ?? 9;
    const pb = PRIORITY_RANK[b.priority ?? ""] ?? 9;
    if (pa !== pb) return pa - pb;
    return (
      (Date.parse(b.updated_at ?? "") || 0) -
      (Date.parse(a.updated_at ?? "") || 0)
    );
  })[0];
}

export function lastCompleted(completed: Practice[]): Practice | null {
  if (completed.length === 0) return null;
  const at = (p: Practice) =>
    Date.parse(p.completion_date ?? p.updated_at ?? "") || 0;
  return completed.slice().sort((a, b) => at(b) - at(a))[0];
}

export interface ContactFact {
  days: number | null;
  channel: string | null;
  summary: string | null;
}

/**
 * The more recent of (newest logged interaction, client's own last-contact
 * date) wins, and channel/summary come only from that winner — a date is
 * never paired with the other source's text.
 */
export function contactFact(
  client: Client,
  interactions: Interaction[],
  now: number,
): ContactFact {
  const at = (i: Interaction) =>
    Date.parse(i.interaction_date || i.created_at) || 0;
  const newest = interactions.length
    ? interactions.reduce((a, b) => (at(b) > at(a) ? b : a))
    : null;
  const iT = newest && at(newest) > 0 ? at(newest) : NaN;
  const cT = client.last_interaction_date
    ? Date.parse(client.last_interaction_date)
    : NaN;
  const useInteraction = !Number.isNaN(iT) && (Number.isNaN(cT) || iT >= cT);
  const t = useInteraction ? iT : cT;
  if (Number.isNaN(t)) return { days: null, channel: null, summary: null };
  return {
    days: Math.max(
      0,
      keyToDayNumber(witaDateKey(now)) - keyToDayNumber(witaDateKey(t)),
    ),
    channel: useInteraction ? humanize(newest!.interaction_type) : null,
    summary: useInteraction
      ? newest!.summary || null
      : client.last_interaction_summary || null,
  };
}

/** Full years between a birth date and today (WITA); null when unknown or unparsable. */
export function ageYears(dob: string | undefined, now: number): number | null {
  if (!dob) return null;
  const key = calendarKey(dob);
  if (!key) return null;
  const [by, bm, bd] = key.split("-").map(Number);
  const [ny, nm, nd] = witaDateKey(now).split("-").map(Number);
  let age = ny - by;
  if (nm < bm || (nm === bm && nd < bd)) age -= 1;
  return age >= 0 && age < 130 ? age : null;
}

/** "Sample Fixture Person" -> "SP"; one word -> its first letter. */
export function initials(fullName: string): string {
  const words = fullName.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "";
  const first = words[0].charAt(0);
  const last = words.length > 1 ? words[words.length - 1].charAt(0) : "";
  return (first + last).toUpperCase();
}
