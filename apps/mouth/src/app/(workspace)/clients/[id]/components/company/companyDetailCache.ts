import { api } from "@/lib/api";

export type CompanyDetail = Awaited<ReturnType<typeof api.crm.getCompany>>;

// The full company detail (associates + documents), kept for the session so
// re-opening the Company tab does not fetch it again. Five minutes, as a
// backstop; every mutation made from kita drops it (the tab's own reload, and
// the client card's invalidate for changes made on other tabs).
const COMPANY_DETAIL_TTL_MS = 5 * 60_000;
const cache = new Map<number, { at: number; value: CompanyDetail }>();

/** Forget every cached company detail. */
export function clearCompanyDetailCache(): void {
  cache.clear();
}

export function fetchCompanyDetail(
  companyId: number,
  bypass: boolean,
): Promise<CompanyDetail> {
  const hit = cache.get(companyId);
  if (!bypass && hit && Date.now() - hit.at < COMPANY_DETAIL_TTL_MS) {
    return Promise.resolve(hit.value);
  }
  return api.crm.getCompany(companyId).then((value) => {
    cache.set(companyId, { at: Date.now(), value });
    return value;
  });
}
