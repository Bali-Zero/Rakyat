"use client";

import { useState } from "react";
import { Loader2, CheckCircle2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { Money } from "@balizero/core";
import {
  accountingApi,
  type BankTransaction,
  type MatchCandidate,
} from "@/lib/api/workspace/accounting.api";

// ── Confirm modal ────────────────────────────────────────────────────────────

export function ConfirmModal({
  txn,
  candidate,
  onClose,
  onConfirmed,
}: {
  txn: Pick<BankTransaction, "id" | "amount_idr" | "description" | "txn_date">;
  candidate: MatchCandidate;
  onClose: () => void;
  onConfirmed: () => void;
}) {
  const { success, error } = useToast();
  const [busy, setBusy] = useState(false);
  const delta = txn.amount_idr - candidate.invoice_amount_idr;
  // A transfer net of PPh23 withholding (delta < 0 but flagged likely_withheld)
  // still SETTLES the invoice — the withheld tax is recorded separately, not a
  // shortfall. Only a genuine underpayment (short, NOT withholding) is 'partial'.
  const newStatus: "paid" | "partial" =
    delta >= -1000 || candidate.likely_withheld ? "paid" : "partial";

  const handleConfirm = async () => {
    setBusy(true);
    try {
      await accountingApi.confirm({
        bank_txn_id: txn.id,
        practice_id: candidate.practice_id,
        invoice_id: candidate.invoice_id,
        amount_applied_idr: txn.amount_idr,
        new_status: newStatus,
        payment_method: "bank_transfer",
        payment_reference: txn.description ?? undefined,
        movement_date: txn.txn_date,
        pph_withheld_idr: candidate.likely_withheld ? -delta : 0,
      });
      success(
        "Payment confirmed",
        `${candidate.client_name} marked ${newStatus}`,
      );
      onConfirmed();
    } catch (err) {
      error("Confirm failed", "Please try again");
      logger.error(
        `accounting confirm failed txn=${txn.id} inv=${candidate.invoice_id}`,
        {},
        err as Error,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-xl border bg-card p-6 shadow-xl">
        <div className="flex items-start justify-between">
          <h3 className="text-lg font-semibold">Confirm payment match</h3>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <dl className="mt-4 space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Transfer</dt>
            <dd className="font-medium tabular-nums">
              <Money value={txn.amount_idr} />
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Client</dt>
            <dd className="font-medium">{candidate.client_name}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Invoice</dt>
            <dd className="tabular-nums">
              <Money value={candidate.invoice_amount_idr} />
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Delta</dt>
            <dd
              className={`tabular-nums ${delta < 0 ? "text-[var(--state-warning)]" : "text-[var(--state-success)]"}`}
            >
              {delta >= 0 ? "+" : ""}
              <Money value={delta} />
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">New status</dt>
            <dd className="font-semibold uppercase">{newStatus}</dd>
          </div>
          {candidate.likely_withheld && (
            <p className="rounded-md bg-[color-mix(in_srgb,var(--state-warning)_10%,transparent)] p-2 text-xs text-[var(--state-warning)]">
              Looks net of PPh23 withholding — the delta is recorded as withheld
              tax.
            </p>
          )}
        </dl>

        <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
          {candidate.reasons.map((reason, i) => (
            <li key={i}>• {reason}</li>
          ))}
        </ul>

        <div className="mt-6 flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={handleConfirm} disabled={busy}>
            {busy ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <CheckCircle2 className="mr-2 h-4 w-4" />
            )}
            Confirm {newStatus}
          </Button>
        </div>
      </div>
    </div>
  );
}
