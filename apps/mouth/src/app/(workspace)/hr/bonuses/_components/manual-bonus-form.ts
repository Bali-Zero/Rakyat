/** Pure rules of the Add bonus form, shared by the dialog and its tests. */

export const MAX_AMOUNT_IDR = 100_000_000;
export const REASON_MIN = 3;
export const REASON_MAX = 300;

export interface ManualBonusDraft {
  employeeId: string;
  amount: string;
  reason: string;
  serviceCode: string;
  awardedOn: string;
}

export type ManualBonusErrors = Partial<
  Record<"employeeId" | "amount" | "reason" | "awardedOn", string>
>;

/** Today's calendar day in WITA (the business timezone), YYYY-MM-DD. */
export function witaToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Makassar",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function validateManualBonus(d: ManualBonusDraft): ManualBonusErrors {
  const errors: ManualBonusErrors = {};
  if (!d.employeeId) errors.employeeId = "Choose who gets the bonus.";
  const amount = Number(d.amount);
  if (!d.amount.trim() || !Number.isInteger(amount) || amount <= 0) {
    errors.amount = "Enter a whole amount above 0.";
  } else if (amount > MAX_AMOUNT_IDR) {
    errors.amount = "The most one bonus can be is Rp 100.000.000.";
  }
  const reason = d.reason.trim();
  if (reason.length < REASON_MIN) {
    errors.reason = `Say why, in at least ${REASON_MIN} characters.`;
  } else if (reason.length > REASON_MAX) {
    errors.reason = `Keep it to ${REASON_MAX} characters.`;
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d.awardedOn)) {
    errors.awardedOn = "Pick a date.";
  }
  return errors;
}
