"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { accountingApi } from "@/lib/api/workspace/accounting.api";
import { logger } from "@/lib/logger";

export type BankDateState =
  | { status: "loading" | "empty" | "failed"; date: null }
  | { status: "ok"; date: string };

export function useBankDate() {
  const [state, setState] = useState<BankDateState>({
    status: "loading",
    date: null,
  });
  const request = useRef(0);

  const load = useCallback(async () => {
    const token = ++request.current;
    setState({ status: "loading", date: null });
    try {
      const rows = await accountingApi.getBankTransactions({ limit: 1 });
      if (request.current !== token) return;
      if (!Array.isArray(rows)) {
        setState({ status: "failed", date: null });
        logger.error(
          "accounting bank date load failed",
          {},
          new Error("bank-transactions body malformed"),
        );
        return;
      }
      setState(
        rows.length > 0
          ? { status: "ok", date: rows[0].txn_date }
          : { status: "empty", date: null },
      );
    } catch (err) {
      if (request.current !== token) return;
      setState({ status: "failed", date: null });
      logger.error("accounting bank date load failed", {}, err as Error);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { ...state, reload: load };
}
