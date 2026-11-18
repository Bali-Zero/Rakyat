"use client";

/** Collapsed list of transfers linked or ignored, each with a Reopen button. */

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
  onReopened,
}: {
  /** Bumped by the inbox after a write: the open list reloads. */
  version: number;
  onReopened: () => void;
}) {
  const { error, success } = useToast();
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<BankTransaction[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [rowError, setRowError] = useState<{
    id: number;
    message: string;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const [linked, ignored] = await Promise.all([
        accountingApi.getBankTransactions({ reconciledStatus: "manual" }),
        accountingApi.getBankTransactions({ reconciledStatus: "ignored" }),
      ]);
      setRows([...linked, ...ignored].sort((a, b) => b.id - a.id));
    } catch (err) {
      error("Failed to load linked and ignored transfers", "Please try again");
      logger.error("accounting linked/ignored load failed", {}, err as Error);
    } finally {
      setLoading(false);
    }
  }, [error]);

  useEffect(() => {
    if (open) void load();
  }, [open, version, load]);

  const reopen = async (id: number) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setRowError(null);
    try {
      await accountingApi.reopenTransaction(id);
      success("Transfer reopened");
      onReopened();
    } catch (err) {
      const detail =
        (err instanceof Error && err.message) || "Please try again";
      setRowError({ id, message: detail });
      error("Reopen failed", detail);
      logger.error(`accounting reopen failed txn=${id}`, {}, err as Error);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <section className="rounded-lg border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="w-full p-3 text-left text-sm font-medium hover:bg-muted/30"
      >
        Linked and ignored
      </button>
      {open && (
        <div className="space-y-2 border-t p-3">
          {loading ? (
            <div className="flex items-center justify-center py-3 text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : rows.length === 0 ? (
            <p className="text-center text-sm text-muted-foreground">
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
                    <p className="text-xs text-muted-foreground tabular-nums">
                      {r.txn_date} ·{" "}
                      {r.reconciled_status === "ignored" ? "Ignored" : "Linked"}{" "}
                      · <Money value={r.amount_idr} />
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
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
