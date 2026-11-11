import type { Practice } from "@/lib/api/crm/crm.types";

/**
 * Merge the response of a practice save into the page state WITHOUT touching the
 * invoice company: that pair belongs to the "Invoice to company" card, and a
 * delayed response from an older save (notes, priority, payment, service) carries
 * a stale copy that must never overwrite what the card saved since.
 */
export function mergePracticeUpdate(
  current: Practice | null,
  updated: Practice,
): Practice {
  if (!current) return updated;
  const { invoice_company_id, invoice_company_name, ...rest } = updated;
  return { ...current, ...rest };
}
