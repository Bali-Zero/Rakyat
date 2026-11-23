import { createContext, useContext } from "react";

export const NOVEMBER = { start: "2026-11-01", end: "2026-11-30" } as const;
export const AccountingMonth = createContext<string | undefined>(undefined);

export function useAccountingMonth() {
  return useContext(AccountingMonth);
}
