"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  Banknote,
  Calculator,
  CheckCircle,
  ChevronRight,
  CreditCard,
} from "lucide-react";
import { toast } from "sonner";
import * as hrApi from "@/lib/api/hr/hr";
import { workDayOf } from "@/lib/format/work-day";
import { useHrCapabilities } from "@/lib/hr/use-hr-capabilities";
import type { PayrollPeriod, Payslip } from "@/types/hr";
import { Money } from "@balizero/core";

/** Dashboard panel recipe — mirrors the day/dark-aware Kita surfaces. */
const PANEL: React.CSSProperties = {
  background: "var(--bz-card)",
  borderColor: "var(--bz-border)",
};

// Payroll period statuses, hue-preserving: draft -> neutral, calculated ->
// info, approved -> warning, paid -> success (same map as payroll/[slipId]).
const statusColors: Record<string, string> = {
  draft: "bg-[var(--bz-glass-rim)] text-[var(--bz-text-2)]",
  calculated: "bg-[var(--state-info)]/10 text-[var(--state-info)]",
  approved: "bg-[var(--state-warning)]/10 text-[var(--state-warning)]",
  paid: "bg-[var(--state-success)]/10 text-[var(--state-success)]",
};

/**
 * The month payroll is run for: the civil month in Bali, whatever zone the
 * browser is in. The server cuts the month's bonuses in Asia/Makassar too.
 */
function payrollMonthNow(): { month: number; year: number } {
  const [year, month] = workDayOf().split("-").map(Number);
  return { month, year };
}

// The server locks a period once it is approved or paid: a calculation of
// that month is refused, so the page does not offer one.
const CLOSED_STATUSES: ReadonlySet<string> = new Set(["approved", "paid"]);

export default function PayrollPage() {
  const [periods, setPeriods] = useState<PayrollPeriod[]>([]);
  const [payslips, setPayslips] = useState<Payslip[]>([]);
  const { capabilities, loaded } = useHrCapabilities();
  const isAdmin = capabilities.hr_admin;
  // Calculate, approve and mark-paid belong to the owner and the accountant only.
  const canRunPayroll = capabilities.hr_payroll_write === true;
  const [loading, setLoading] = useState(true);
  // A list that could not be read is not an empty list: the empty state
  // invites a calculation, and nothing is known of the month it would be for.
  const [loadFailed, setLoadFailed] = useState(false);
  const [calculating, setCalculating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadFailed(false);
    try {
      if (isAdmin) {
        const data = await hrApi.listPayrollPeriods();
        setPeriods(data.periods || []);
      } else {
        const data = await hrApi.listPayslips();
        setPayslips(data.payslips || []);
      }
    } catch {
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    if (!loaded) return;
    void load();
  }, [loaded, load]);

  const handleCalculate = async () => {
    const { month, year } = payrollMonthNow();
    setCalculating(true);
    try {
      await hrApi.calculatePayroll(month, year);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Failed to calculate payroll",
      );
      setCalculating(false);
      return;
    }
    // Calculated. Reading the list again is another request: if it fails,
    // that is a list that could not be read, not a calculation that failed.
    toast.success("Payroll calculated");
    try {
      const data = await hrApi.listPayrollPeriods();
      setPeriods(data.periods || []);
    } catch {
      setLoadFailed(true);
    } finally {
      setCalculating(false);
    }
  };

  const handleApprove = async (id: number) => {
    try {
      await hrApi.approvePayroll(id);
      setPeriods((prev) =>
        prev.map((p) => (p.id === id ? { ...p, status: "approved" } : p)),
      );
      toast.success("Payroll approved");
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Failed to approve payroll",
      );
    }
  };

  const handleMarkPaid = async (id: number) => {
    try {
      await hrApi.markPayrollPaid(id);
      setPeriods((prev) =>
        prev.map((p) => (p.id === id ? { ...p, status: "paid" } : p)),
      );
      toast.success("Payroll marked as paid");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed");
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">Payroll</h1>
        <div className="animate-pulse space-y-3">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-20 bg-[var(--bz-glass-rim)] rounded-lg" />
          ))}
        </div>
      </div>
    );
  }

  if (loadFailed) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
          {isAdmin ? "Payroll Periods" : "My Payslips"}
        </h1>
        <div
          role="alert"
          className="border rounded-xl p-8 text-center text-[var(--bz-text-2)] space-y-3"
          style={PANEL}
        >
          <p>
            {isAdmin
              ? "The payroll periods could not be loaded."
              : "Your payslips could not be loaded."}
          </p>
          <button
            onClick={() => void load()}
            className="px-4 py-2 rounded-lg bg-[var(--bz-accent)]/10 text-[var(--bz-accent)] hover:bg-[var(--bz-accent)]/20 text-sm transition-colors"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (isAdmin) {
    const { month, year } = payrollMonthNow();
    const thisMonth = periods.find(
      (p) => p.payroll_month === month && p.payroll_year === year,
    );
    const closedAs =
      thisMonth && CLOSED_STATUSES.has(thisMonth.status)
        ? thisMonth.status
        : null;
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
            Payroll Periods
          </h1>
          {canRunPayroll && (
            <div className="flex flex-col items-end gap-1">
              <button
                onClick={handleCalculate}
                disabled={calculating || closedAs !== null}
                aria-describedby={closedAs ? "payroll-month-closed" : undefined}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-[var(--bz-accent)]/10 text-[var(--bz-accent)] hover:bg-[var(--bz-accent)]/20 text-sm transition-colors disabled:opacity-50"
              >
                <Calculator size={16} />
                {calculating ? "Calculating..." : "Calculate Current Month"}
              </button>
              {closedAs && (
                <p
                  id="payroll-month-closed"
                  className="text-xs text-[var(--bz-text-3)]"
                >
                  {String(month).padStart(2, "0")}/{year} is already {closedAs}.
                </p>
              )}
            </div>
          )}
        </div>

        {periods.length === 0 ? (
          <div
            className="border rounded-xl p-8 text-center text-[var(--bz-text-3)]"
            style={PANEL}
          >
            No payroll periods yet.
            {canRunPayroll && ' Click "Calculate Current Month" to start.'}
          </div>
        ) : (
          <div className="space-y-2">
            {periods.map((period) => (
              <div
                key={period.id}
                className="border rounded-lg p-4 flex items-center justify-between"
                style={PANEL}
              >
                <div>
                  <div className="flex items-center gap-3">
                    <Banknote size={18} className="text-[var(--bz-text-3)]" />
                    <span className="font-medium text-[var(--bz-text-1)]">
                      {String(period.payroll_month).padStart(2, "0")}/
                      {period.payroll_year}
                    </span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full ${statusColors[period.status] || ""}`}
                    >
                      {period.status}
                    </span>
                  </div>
                  <div className="text-sm text-[var(--bz-text-3)] mt-1">
                    {period.payslip_count} payslips · Total:{" "}
                    <Money value={period.total_net || 0} />
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {canRunPayroll && period.status === "calculated" && (
                    <button
                      onClick={() => handleApprove(period.id)}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[var(--state-warning)]/10 text-[var(--state-warning)] hover:bg-[var(--state-warning)]/20 text-sm"
                    >
                      <CheckCircle size={14} />
                      Approve
                    </button>
                  )}
                  {canRunPayroll && period.status === "approved" && (
                    <button
                      onClick={() => handleMarkPaid(period.id)}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[var(--state-success)]/10 text-[var(--state-success)] hover:bg-[var(--state-success)]/20 text-sm"
                    >
                      <CreditCard size={14} />
                      Mark Paid
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
        My Payslips
      </h1>
      {payslips.length === 0 ? (
        <div
          className="border rounded-xl p-8 text-center text-[var(--bz-text-3)]"
          style={PANEL}
        >
          No payslips available yet.
        </div>
      ) : (
        <div className="space-y-2">
          {payslips.map((slip) => (
            <Link
              key={slip.id}
              href={`/hr/payroll/${slip.id}`}
              className="border rounded-lg p-4 flex items-center justify-between group hover:bg-[var(--bz-glass-rim)] transition-colors"
              style={PANEL}
            >
              <div>
                <span className="font-medium text-[var(--bz-text-1)]">
                  {String(slip.payroll_month).padStart(2, "0")}/
                  {slip.payroll_year}
                </span>
                <div className="text-sm text-[var(--bz-text-3)] mt-1">
                  Base: <Money value={slip.base_salary_idr} /> + Bonus:{" "}
                  <Money value={slip.bonus_total_idr} /> - Deductions:{" "}
                  <Money value={slip.deduction_total_idr} />
                </div>
              </div>
              <div className="flex items-center gap-3">
                <div className="text-right">
                  <div className="text-lg font-semibold text-[var(--state-success)]">
                    <Money value={slip.net_salary_idr} />
                  </div>
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full ${statusColors[slip.period_status] || ""}`}
                  >
                    {slip.period_status}
                  </span>
                </div>
                <ChevronRight
                  size={16}
                  className="text-[var(--bz-text-3)] group-hover:text-[var(--bz-text-2)] transition-colors"
                />
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
