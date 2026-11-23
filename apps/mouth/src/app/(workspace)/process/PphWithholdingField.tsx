"use client";

import type { PphSelection } from "@/lib/api/crm/crm.types";

/**
 * "PPh withholding" on a process (DAI-168, K127). The API takes a top-level
 * `pph` and stores it in `metadata.pph`. Empty means no PPh. The type list,
 * the rate limit and the roles mirror the backend (services/invoicing/pph.py,
 * app/utils/crm_utils.py); the backend validates and authorizes every write,
 * so the role check here only decides who sees the field.
 */
export const PPH_TYPE_LABELS = {
  PPH23: "PPh 23",
  PPH21: "PPh 21",
  PPH4_2: "PPh 4(2)",
} as const;

export const PPH_MAX_RATE_PERCENT = 10;

export const PPH_SETTER_ROLES: readonly string[] = [
  "admin",
  "board member",
  "ceo",
  "founder",
  "tax consultant",
  "tax lead",
  "tax supervisor",
  "tax responsible",
];

export type PphType = keyof typeof PPH_TYPE_LABELS;

export interface PphValue {
  type: PphType | "";
  rate: string;
}

export const EMPTY_PPH: PphValue = { type: "", rate: "" };

export function canSetPph(
  isAdmin: boolean,
  role: string | null | undefined,
): boolean {
  if (isAdmin) return true;
  return PPH_SETTER_ROLES.includes((role ?? "").trim().toLowerCase());
}

const isPphType = (v: unknown): v is PphType =>
  typeof v === "string" &&
  Object.prototype.hasOwnProperty.call(PPH_TYPE_LABELS, v);

/** `practice.metadata` may arrive as JSON text, even double-encoded. */
export function pphFromMetadata(metadata: unknown): PphValue {
  let parsed = metadata;
  try {
    for (let i = 0; i < 2 && typeof parsed === "string"; i++) {
      parsed = JSON.parse(parsed);
    }
  } catch {
    return EMPTY_PPH;
  }
  const raw = (parsed as { pph?: { type?: unknown; rate_percent?: unknown } })
    ?.pph;
  const rate = Number(raw?.rate_percent);
  if (!raw || !isPphType(raw.type) || !Number.isFinite(rate) || rate <= 0) {
    return EMPTY_PPH;
  }
  // A hand-edited value the invoice ignores must not block saving other fields.
  const value: PphValue = { type: raw.type, rate: String(rate) };
  return pphError(value) ? EMPTY_PPH : value;
}

const normalizedRate = (rate: string) => rate.trim().replace(",", ".");

/** Why the draft cannot be sent, or null when it is empty or valid. */
export function pphError(value: PphValue): string | null {
  const rate = normalizedRate(value.rate);
  if (!value.type && !rate) return null;
  if (!value.type) return "Pick the PPh type, or clear the rate.";
  if (!rate) return "Enter the PPh rate, or clear the type.";
  const n = Number(rate);
  if (!/^\d+(\.\d{1,2})?$/.test(rate) || n <= 0 || n > PPH_MAX_RATE_PERCENT) {
    return `PPh rate must be above 0 and at most ${PPH_MAX_RATE_PERCENT}%, with up to 2 decimals.`;
  }
  return null;
}

/** The API value for a valid draft; null when the draft is empty or invalid. */
export function pphSelection(value: PphValue): PphSelection | null {
  if (!value.type || pphError(value)) return null;
  return {
    type: value.type,
    rate_percent: Number(normalizedRate(value.rate)),
  };
}

export function pphChanged(saved: PphValue, next: PphValue): boolean {
  const a = pphSelection(saved);
  const b = pphSelection(next);
  return a?.type !== b?.type || a?.rate_percent !== b?.rate_percent;
}

interface Props {
  value: PphValue;
  onChange: (value: PphValue) => void;
  disabled?: boolean;
}

const fieldClass =
  "w-full px-3 py-2.5 rounded-lg border border-[var(--border)] bg-[var(--background-elevated)] text-[var(--foreground)] placeholder:text-[var(--foreground-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/50";

export function PphWithholdingField({ value, onChange, disabled }: Props) {
  const error = pphError(value);
  return (
    <div className="space-y-2" data-testid="pph-withholding-field">
      <div className="grid grid-cols-1 sm:grid-cols-[2fr_1fr] gap-2">
        <select
          aria-label="PPh type"
          value={value.type}
          disabled={disabled}
          onChange={(e) => {
            const type = e.target.value;
            onChange(isPphType(type) ? { type, rate: value.rate } : EMPTY_PPH);
          }}
          className={fieldClass}
        >
          <option value="">No PPh</option>
          {(Object.keys(PPH_TYPE_LABELS) as PphType[]).map((type) => (
            <option key={type} value={type}>
              {PPH_TYPE_LABELS[type]}
            </option>
          ))}
        </select>
        <input
          type="text"
          inputMode="decimal"
          aria-label="PPh rate percent"
          value={value.rate}
          disabled={disabled}
          maxLength={5}
          onChange={(e) => onChange({ ...value, rate: e.target.value })}
          className={fieldClass}
          placeholder="Rate %"
        />
      </div>
      {error && (
        <p role="alert" className="text-xs text-[var(--state-danger)]">
          {error}
        </p>
      )}
    </div>
  );
}
