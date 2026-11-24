import { createContext, useContext } from "react";

export const NOVEMBER = { start: "2026-11-01", end: "2026-11-30" } as const;
export const OCTOBER = { start: "2026-10-01", end: "2026-10-31" } as const;
export const AccountingMonth = createContext<string | undefined>(undefined);

export function useAccountingMonth() {
  return useContext(AccountingMonth);
}
