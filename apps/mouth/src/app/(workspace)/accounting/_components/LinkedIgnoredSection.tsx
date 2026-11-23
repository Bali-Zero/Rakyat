"use client";

/** Collapsed list of transfers linked or ignored, each with a Reopen button. */

import { useAccountingMonth } from "./accountingPeriod";
import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { Money } from "@balizero/core";
import {
  accountingApi,
  type BankTransaction,
} from "@/lib/api/workspace/accounting.api";

export function LinkedIgnoredSection({
  version,
  locked,
  exclusive,
  onReopened,
}: {
  /** True while any write of the tab is pending or the confirm dialog is open. */
  locked: boolean;
  /** Shared runner: returns at once when a write is pending, else runs and clears the flag. */
  exclusive: (fn: () => Promise<unknown>) => Promise<unknown>;
  /** Bumped by the inbox after a write: the open list reloads. */
  version: number;
  onReopened: () => void;
}) {
  const { error, success } = useToast();
  const [open, setOpen] = useState(false);
  const month = useAccountingMonth();
  const [rows, setRows] = useState<BankTransaction[]>([]);
  // Status of the LATEST STARTED refresh; with no rows it picks loading / empty / failure.
  const [loadStatus, setLoadStatus] = useState<"loading" | "ok" | "failed">(
    "loading",
  );
  const [rowError, setRowError] = useState<{
    id: number;
    message: string;
  } | null>(null);

  // Token of the latest refresh: an older one answering late never replaces the list.
  const loadReq = useRef(0);

  const load = useCallback(async () => {
    const token = ++loadReq.current;
    setLoadStatus("loading");
    try {
      const [linked, ignored] = await Promise.all([
        accountingApi.getBankTransactions({
          reconciledStatus: "manual",
          month,
        }),
        accountingApi.getBankTransactions({
          reconciledStatus: "ignored",
          month,
        }),
      ]);
      if (loadReq.current !== token) return;
      setRows([...linked, ...ignored].sort((a, b) => b.id - a.id));
      setLoadStatus("ok");
    } catch (err) {
      if (loadReq.current !== token) return;
      setLoadStatus("failed");
      error("Failed to load linked and ignored transfers", "Please try again");
      logger.error("accounting linked/ignored load failed", {}, err as Error);
    }
  }, [error, month]);

  useEffect(() => {
    if (open) void load();
  }, [open, version, load]);

  const reopen = (id: number) =>
    exclusive(async () => {
      setRowError(null);
      try {
        await accountingApi.reopenTransaction(id);
        success("Transfer reopened");
        // the reopened row leaves at once; the refresh then replaces the list
        setRows((cur) => cur.filter((r) => r.id !== id));
        // invalidate every refresh in flight and start the new one right here
        loadReq.current += 1;
        void load();
        onReopened();
      } catch (err) {
        const detail =
          (err instanceof Error && err.message) || "Please try again";
        setRowError({ id, message: detail });
        error("Reopen failed", detail);
        logger.error(`accounting reopen failed txn=${id}`, {}, err as Error);
      }
    });

  return (
    <section className="rounded-lg border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="w-full p-3 text-left text-sm font-medium hover:bg-[var(--bz-card-hover)]/30"
      >
        Linked and ignored
      </button>
      {open && (
        <div className="space-y-2 border-t p-3">
          {rows.length === 0 && loadStatus === "loading" ? (
            <div className="flex items-center justify-center py-3 text-[var(--bz-text-2)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : rows.length === 0 && loadStatus === "failed" ? (
            <div className="text-center text-sm text-[var(--bz-text-2)]">
              <p>Could not load this list.</p>
              <Button
                className="mt-2"
                size="sm"
                variant="outline"
                onClick={() => void load()}
              >
                Try again
              </Button>
            </div>
          ) : rows.length === 0 ? (
            <p className="text-center text-sm text-[var(--bz-text-2)]">
              Nothing linked or ignored yet.
            </p>
          ) : (
            rows.map((r) => (
              <div key={r.id} className="rounded-md border p-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {r.description ?? "(no memo)"}
                    </p>
                    <p className="text-xs text-[var(--bz-text-2)] tabular-nums">
                      {r.txn_date} ·{" "}
                      {r.reconciled_status === "ignored" ? "Ignored" : "Linked"}{" "}
                      · <Money value={r.amount_idr} />
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => reopen(r.id)}
                  >
                    Reopen
                  </Button>
                </div>
                {rowError?.id === r.id && (
                  <p
                    role="alert"
                    className="mt-1 text-sm text-[var(--state-danger)]"
                  >
                    {rowError.message}
                  </p>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </section>
  );
}
