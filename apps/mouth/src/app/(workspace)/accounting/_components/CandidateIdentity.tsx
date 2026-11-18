import type { MatchCandidate } from "@/lib/api/workspace/accounting.api";

export const NO_INVOICE_NUMBER = "No invoice number";

/** What identifies an invoice to a person: number, issue date, service. Null and absent alike. */
export function candidateIdentity(
  c: Pick<
    MatchCandidate,
    "invoice_number" | "invoice_issued_at" | "service_name"
  >,
): string[] {
  const parts = [c.invoice_number || NO_INVOICE_NUMBER];
  // The page shows its other dates as stored (YYYY-MM-DD), so the ISO stamp is cut the same way.
  if (c.invoice_issued_at) parts.push(c.invoice_issued_at.slice(0, 10));
  if (c.service_name) parts.push(c.service_name);
  return parts;
}

export function CandidateIdentity({
  candidate,
  className = "text-xs text-[var(--bz-text-2)]",
}: {
  candidate: MatchCandidate;
  className?: string;
}) {
  return (
    <p className={className} data-testid="candidate-identity">
      {candidateIdentity(candidate).join(" · ")}
    </p>
  );
}
