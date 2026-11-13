/**
 * Practice State Machine — Frontend mirror of the backend authority.
 *
 * Source of truth:
 *   apps/backend-rag/backend/services/crm/practice_state_machine.py
 *
 * The backend ALWAYS validates transitions (defense-in-depth). This module
 * only filters the UI dropdown so users do not see illegal options. If the
 * two ever diverge, the backend rejects the request and the user sees the
 * existing error toast — data stays consistent. The snapshot test in
 * `state-machine.test.ts` pins the table; if the backend changes, that
 * test fails and the discrepancy is caught in CI.
 */

export type PracticeStatus =
  | "inquiry"
  | "waiting_documents"
  | "sending_invoice"
  | "on_process"
  | "completed"
  | "cancelled";

/**
 * Allowed forward transitions: from → set of valid `to` states.
 * Mirrors `VALID_TRANSITIONS` in the backend module.
 */
export const VALID_TRANSITIONS: Record<
  PracticeStatus,
  readonly PracticeStatus[]
> = {
  inquiry: ["waiting_documents", "cancelled"],
  waiting_documents: ["sending_invoice", "inquiry", "cancelled"],
  sending_invoice: ["on_process", "waiting_documents", "cancelled"],
  on_process: ["completed", "sending_invoice", "cancelled"],
  completed: ["cancelled"],
  cancelled: ["inquiry"],
} as const;

/**
 * Transitions that require an admin role on the user object.
 * Mirrors `ADMIN_ONLY_TRANSITIONS` in the backend module.
 */
export const ADMIN_ONLY_TRANSITIONS: ReadonlyArray<
  readonly [PracticeStatus, PracticeStatus]
> = [
  ["completed", "cancelled"],
  ["cancelled", "inquiry"],
] as const;

/** Human-readable labels for each state. */
export const STATUS_LABELS: Record<PracticeStatus, string> = {
  inquiry: "Inquiry",
  waiting_documents: "Waiting Documents",
  sending_invoice: "Sending Invoice",
  on_process: "On Process",
  completed: "Completed",
  cancelled: "Cancelled",
};

const ALL_STATES = new Set<string>(Object.keys(VALID_TRANSITIONS));

/**
 * Placeholder practice type for an inquiry opened WITHOUT a service.
 * Mirrors `OPEN_INQUIRY_TYPE_CODE` / `STATES_WITHOUT_SERVICE` in the backend
 * module: the backend refuses to move such a practice past the inquiry stage
 * until a real service is chosen (migration 311, 2026-09-11).
 */
export const OPEN_INQUIRY_TYPE_CODE = "open_inquiry";

const STATES_WITHOUT_SERVICE = new Set<string>(["inquiry", "cancelled"]);

/** True when moving to `target` requires a real service to be chosen first. */
export function needsServiceBefore(
  target: PracticeStatus | string,
  practiceTypeCode: string | null | undefined,
): boolean {
  return (
    practiceTypeCode === OPEN_INQUIRY_TYPE_CODE &&
    !STATES_WITHOUT_SERVICE.has(target)
  );
}

/**
 * Quoted Price gate (owner decision 2026-11-13). Mirrors
 * `validate_quoted_price_for_invoice` in the backend module: a practice cannot move INTO
 * Sending Invoice without a Quoted Price (null or 0). The backend refuses with a 422 whose
 * detail is `QUOTED_PRICE_REQUIRED_DETAIL`; this module only lets the UI say so in plain
 * words BEFORE the round-trip, and map the refusal if it still comes back.
 */
export const QUOTED_PRICE_REQUIRED_DETAIL =
  "Quoted Price is required before Sending Invoice";
export const QUOTED_PRICE_REQUIRED_TEXT_ID =
  "Isi Quoted Price dulu sebelum Sending Invoice";

/** True when `value` is a number (or numeric string) greater than zero. */
export function hasQuotedPrice(value: unknown): boolean {
  if (value === null || value === undefined || typeof value === "boolean") {
    return false;
  }
  if (typeof value === "string" && value.trim() === "") return false;
  const n = Number(value);
  return Number.isFinite(n) && n > 0;
}

/**
 * True when moving a practice from `current` to `target` would enter Sending Invoice
 * with no Quoted Price. A practice already in Sending Invoice is never blocked
 * (only entering the status is gated).
 */
const LEGACY_SENDING_INVOICE = new Set<string>([
  "quotation_sent",
  "payment_pending",
  "waiting_payment",
]);

/** Mirrors the backend's `normalize_state` for the one state this gate cares about. */
function isSendingInvoice(state: string | null | undefined): boolean {
  return (
    !!state &&
    (state === "sending_invoice" || LEGACY_SENDING_INVOICE.has(state))
  );
}

export function needsQuotedPriceBefore(
  target: PracticeStatus | string,
  quotedPrice: unknown,
  current?: string | null,
): boolean {
  return (
    isSendingInvoice(target) &&
    !isSendingInvoice(current) &&
    !hasQuotedPrice(quotedPrice)
  );
}

/** True when `error` is the backend's Quoted Price refusal (422 + the fixed detail). */
export function isQuotedPriceRefusal(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const { detail, message } = error as { detail?: unknown; message?: unknown };
  return [detail, message].some(
    (text) =>
      typeof text === "string" && text.includes(QUOTED_PRICE_REQUIRED_DETAIL),
  );
}

/**
 * Return the list of states the user can transition INTO from `current`,
 * including `current` itself (so the dropdown can keep its existing
 * selection without flagging it as illegal).
 *
 * Admin-only transitions are filtered out when `isAdmin` is false.
 *
 * Unknown / legacy states pass through unchanged: only `current` is
 * returned, so the user can keep the value but cannot move to anything
 * else without first hitting a known state via the backend's normalize
 * step.
 */
export function getAllowedNextStatuses(
  current: PracticeStatus | string,
  isAdmin: boolean = false,
): PracticeStatus[] {
  if (!ALL_STATES.has(current)) {
    return [current as PracticeStatus];
  }

  const next: PracticeStatus[] = [current as PracticeStatus];
  for (const candidate of VALID_TRANSITIONS[current as PracticeStatus]) {
    if (
      !isAdmin &&
      ADMIN_ONLY_TRANSITIONS.some(
        ([from, to]) => from === current && to === candidate,
      )
    ) {
      continue;
    }
    next.push(candidate);
  }
  return next;
}
