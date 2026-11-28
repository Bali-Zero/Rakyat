"use client";

import React, { useState } from "react";
import { CheckCircle, CreditCard, Gift } from "lucide-react";
import { toast } from "sonner";
import { Money } from "@balizero/core";
import { formatIDR } from "@balizero/core/utils";
import * as hrApi from "@/lib/api/hr/hr";
import { ApiError } from "@/lib/api/error-handler";
import {
  bonusDayLabel,
  bonusMonthLabel,
  bonusPaydayText,
} from "@/lib/hr/bonus-run-format";
import type { BonusRun } from "@/types/hr";

const PANEL: React.CSSProperties = {
  background: "var(--bz-card)",
  borderColor: "var(--bz-border)",
};

const CHIP: Record<string, string> = {
  Open: "bg-[var(--bz-glass-rim)] text-[var(--bz-text-2)]",
  "Ready to approve": "bg-[var(--state-info)]/10 text-[var(--state-info)]",
  "Awaiting payment":
    "bg-[var(--state-warning)]/10 text-[var(--state-warning)]",
  Paid: "bg-[var(--state-success)]/10 text-[var(--state-success)]",
};

export function bonusRunChip(run: BonusRun): string {
  if (run.status === "paid") return "Paid";
  if (run.status === "approved") return "Awaiting payment";
  return run.ended ? "Ready to approve" : "Open";
}

/** Bonus months from Nov 2026, newest first. Gross amounts only: a bonus carries no tax. */
export function BonusRuns({
  runs,
  canWrite,
  onChanged,
}: {
  runs: BonusRun[];
  canWrite: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  const approve = async (run: BonusRun) => {
    const month = bonusMonthLabel(run.bonus_month);
    const pending =
      run.pending_count > 0
        ? ` ${run.pending_count} pending bonuses (${formatIDR(run.pending_total_idr)}) stay unpaid: approve or reject them first.`
        : "";
    if (
      !window.confirm(
        `Close ${month} and pay ${formatIDR(run.approved_total_idr)} gross.${pending} This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(run.bonus_month);
    try {
      await hrApi.approveBonusRun(run.bonus_month.slice(0, 7), {
        approved_total_idr: run.approved_total_idr,
        pending_count: run.pending_count,
      });
      toast.success(`${month} bonus run approved`);
    } catch (err) {
      if (err instanceof ApiError && err.code === "bonus_run_changed") {
        toast.error("The month changed: review it again");
      } else {
        toast.error(err instanceof Error ? err.message : "Failed to approve");
      }
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  const markPaid = async (run: BonusRun) => {
    const month = bonusMonthLabel(run.bonus_month);
    if (
      !window.confirm(
        `Mark the ${month} bonus run as paid: ${formatIDR(run.approved_total_idr)} gross.`,
      )
    ) {
      return;
    }
    setBusy(run.bonus_month);
    try {
      await hrApi.markBonusRunPaid(run.bonus_month.slice(0, 7));
      toast.success(`${month} bonus run marked as paid`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  return (
    <section aria-label="Bonus runs" className="space-y-2">
      <h2 className="text-lg font-semibold text-[var(--bz-text-1)]">
        Bonus runs
      </h2>
      {[...runs].reverse().map((run) => {
        const chip = bonusRunChip(run);
        return (
          <div
            key={run.bonus_month}
            className="border rounded-lg p-4 flex items-center justify-between gap-4"
            style={PANEL}
          >
            <div>
              <div className="flex items-center gap-3">
                <Gift size={18} className="text-[var(--bz-text-3)]" />
                <span className="font-medium text-[var(--bz-text-1)]">
                  {bonusMonthLabel(run.bonus_month)}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${CHIP[chip]}`}
                >
                  {chip}
                </span>
              </div>
              <div className="text-sm text-[var(--bz-text-3)] mt-1">
                <Money value={run.approved_total_idr} /> gross ·{" "}
                {run.approved_count} bonuses
              </div>
              {run.pending_count > 0 && (
                <div className="text-sm text-[var(--bz-text-3)]">
                  {run.pending_count} pending ·{" "}
                  <Money value={run.pending_total_idr} /> · not paid by this run
                </div>
              )}
              {run.held_count > 0 && (
                <div className="text-sm text-[var(--bz-text-3)]">
                  {run.held_count} held · <Money value={run.held_total_idr} /> ·
                  carried to a later run
                </div>
              )}
              <div className="text-sm text-[var(--bz-text-3)]">
                {run.status === "paid" && run.paid_at
                  ? `Paid ${bonusDayLabel(run.paid_at)}`
                  : bonusPaydayText(
                      run.expected_payday,
                      run.payday_salary_month,
                    )}
              </div>
            </div>
            {canWrite && run.status === "open" && (
              <div className="flex flex-col items-end gap-1">
                <button
                  onClick={() => void approve(run)}
                  disabled={!run.can_approve || busy !== null}
                  className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[var(--state-warning)]/10 text-[var(--state-warning)] hover:bg-[var(--state-warning)]/20 text-sm disabled:opacity-50"
                >
                  <CheckCircle size={14} />
                  Approve run
                </button>
                {!run.can_approve && (
                  <p className="text-xs text-[var(--bz-text-3)]">
                    {run.ended
                      ? "Close the earlier month first."
                      : "Opens when the month ends."}
                  </p>
                )}
              </div>
            )}
            {canWrite && run.status === "approved" && (
              <button
                onClick={() => void markPaid(run)}
                disabled={busy !== null}
                className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[var(--state-success)]/10 text-[var(--state-success)] hover:bg-[var(--state-success)]/20 text-sm disabled:opacity-50"
              >
                <CreditCard size={14} />
                Mark paid
              </button>
            )}
          </div>
        );
      })}
    </section>
  );
}
