import type { MatchCandidate } from "@/lib/api/workspace/accounting.api";

export const NO_INVOICE_NUMBER = "No invoice number";

const DATE_START = /^\d{4}-\d{2}-\d{2}/;

type IdentityFields = Pick<
  MatchCandidate,
  "invoice_number" | "invoice_issued_at" | "service_name"
>;

/** A string that is not empty after trimming; anything else counts as missing. */
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** What identifies an invoice to a person. Null, absent and wrong types are all "missing". */
export function candidateIdentityParts(c: IdentityFields): {
  /** null when the invoice has no usable number */
  number: string | null;
  date: string | null;
  service: string | null;
} {
  const issued = c.invoice_issued_at as unknown;
  return {
    number: text(c.invoice_number),
    // The page shows its other dates as stored (YYYY-MM-DD): cut the stamp the same way.
    date:
      typeof issued === "string" && DATE_START.test(issued)
        ? issued.slice(0, 10)
        : null,
    service: text(c.service_name),
  };
}

/** The compact one-line form used by the candidate row. */
export function CandidateIdentity({
  candidate,
  className = "text-xs text-[var(--bz-text-2)]",
}: {
  candidate: IdentityFields;
  className?: string;
}) {
  const { number, date, service } = candidateIdentityParts(candidate);
  const line = [number ?? NO_INVOICE_NUMBER, date, service].filter(
    (p): p is string => p !== null,
  );
  return (
    <p
      className={`min-w-0 break-words [overflow-wrap:anywhere] ${className}`}
      data-testid="candidate-identity"
    >
      {line.join(" · ")}
    </p>
  );
}
