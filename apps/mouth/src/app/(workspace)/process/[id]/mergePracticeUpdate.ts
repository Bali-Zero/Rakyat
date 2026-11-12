import type { Practice } from "@/lib/api/crm/crm.types";

const SERVICE_FIELDS = [
  "practice_type_id",
  "practice_type_code",
  "practice_type_name",
] as const;

/**
 * Merge the response of a practice save into the page state.
 *
 * Two groups of fields have a single owner and are never taken from an unrelated
 * save's response, because a delayed older response carries a stale copy:
 * the invoice company pair (owned by the "Invoice to company" card) and the
 * service (owned by the service picker, which passes ``service: true``).
 */
export function mergePracticeUpdate(
  current: Practice | null,
  updated: Practice,
  options: { service?: boolean } = {},
): Practice {
  if (!current) return updated;
  const { invoice_company_id, invoice_company_name, ...rest } = updated;
  void invoice_company_id;
  void invoice_company_name;
  const merged: Practice = { ...current, ...rest };
  if (!options.service) {
    for (const field of SERVICE_FIELDS) {
      (merged as unknown as Record<string, unknown>)[field] = (
        current as unknown as Record<string, unknown>
      )[field];
    }
  }
  return merged;
}
