"use client";

import React, { useEffect, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { Gift, Banknote, Calendar, Users } from "lucide-react";
import * as hrApi from "@/lib/api/hr/hr";
import { useHrCapabilities } from "@/lib/hr/use-hr-capabilities";
import type { AdminDashboard, PersonalDashboard } from "@/types/hr";
import { Money } from "@balizero/core";

/** Dashboard panel recipe — follows the active Kita surface theme. */
const PANEL: React.CSSProperties = {
  background: "var(--bz-card)",
  borderColor: "var(--bz-border)",
  boxShadow: "var(--bz-shadow-card)",
};

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
  color,
}: {
  icon: LucideIcon;
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  color: string;
}) {
  return (
    <div className="border rounded-xl p-5" style={PANEL}>
      <div className="flex items-center gap-3 mb-3">
        <div className={`p-2 rounded-lg ${color}`}>
          <Icon size={20} />
        </div>
        <span className="text-sm text-[var(--bz-text-2)]">{label}</span>
      </div>
      <div className="text-2xl font-bold text-[var(--bz-text-1)]">{value}</div>
      {sub && <div className="text-xs text-[var(--bz-text-3)] mt-1">{sub}</div>}
    </div>
  );
}

function AdminDashboardView({ data }: { data: AdminDashboard }) {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
        HR Dashboard
      </h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          icon={Users}
          label="Active Employees"
          value={String(data.employee_count || 0)}
          color="bg-[var(--state-info)]/10 text-[var(--state-info)]"
        />
        <StatCard
          icon={Gift}
          label="Pending Bonuses"
          value={String(data.pending_bonuses?.count || 0)}
          sub={
            data.pending_bonuses?.total ? (
              <Money value={data.pending_bonuses.total} />
            ) : undefined
          }
          color="bg-[var(--state-warning)]/10 text-[var(--state-warning)]"
        />
        <StatCard
          icon={Calendar}
          label="Leave Requests"
          value={String(data.pending_leave_requests || 0)}
          sub="pending approval"
          color="bg-[var(--bz-neon-purple)]/10 text-[var(--bz-neon-purple)]"
        />
        <StatCard
          icon={Banknote}
          label="Current Period"
          value={data.current_period?.status || "No period"}
          sub={
            data.current_period
              ? `${data.current_period.payroll_month}/${data.current_period.payroll_year}`
              : "Not calculated yet"
          }
          color="bg-[var(--state-success)]/10 text-[var(--state-success)]"
        />
      </div>
    </div>
  );
}

function PersonalDashboardView({ data }: { data: PersonalDashboard }) {
  const annualBalance = data.leave_balances?.find((b) => b.code === "annual");
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
        My HR Dashboard
      </h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <StatCard
          icon={Gift}
          label="This Month Bonuses"
          value={String(data.month_bonuses?.count || 0)}
          sub={
            data.month_bonuses?.total ? (
              <Money value={data.month_bonuses.total} />
            ) : (
              "No bonuses yet"
            )
          }
          color="bg-[var(--state-warning)]/10 text-[var(--state-warning)]"
        />
        <StatCard
          icon={Banknote}
          label="Latest Payslip"
          value={
            data.latest_payslip ? (
              <Money value={data.latest_payslip.net_salary_idr} />
            ) : (
              "N/A"
            )
          }
          sub={
            data.latest_payslip
              ? `${data.latest_payslip.payroll_month}/${data.latest_payslip.payroll_year}`
              : "No payslip yet"
          }
          color="bg-[var(--state-success)]/10 text-[var(--state-success)]"
        />
        <StatCard
          icon={Calendar}
          label="Leave Balance"
          value={
            annualBalance
              ? `${annualBalance.allocated_days - annualBalance.used_days - annualBalance.pending_days} days`
              : "N/A"
          }
          sub="annual leave remaining"
          color="bg-[var(--bz-neon-purple)]/10 text-[var(--bz-neon-purple)]"
        />
      </div>
    </div>
  );
}

export default function HRDashboardPage() {
  const [adminData, setAdminData] = useState<AdminDashboard | null>(null);
  const [personalData, setPersonalData] = useState<PersonalDashboard | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const { capabilities, loaded } = useHrCapabilities();

  useEffect(() => {
    if (!loaded) return;
    async function load() {
      try {
        // Everyone with an employee record gets their own card; an HR admin
        // gets the team overview above it.
        const [admin, personal] = await Promise.all([
          capabilities.hr_admin ? hrApi.getDashboard() : null,
          capabilities.has_employee_record ? hrApi.getMyDashboard() : null,
        ]);
        setAdminData(admin);
        setPersonalData(personal);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load dashboard",
        );
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [loaded, capabilities.hr_admin, capabilities.has_employee_record]);

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
          HR Dashboard
        </h1>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div
              key={i}
              className="border rounded-xl p-5 animate-pulse"
              style={PANEL}
            >
              <div className="h-4 bg-[var(--bz-glass-rim)] rounded w-24 mb-4" />
              <div className="h-8 bg-[var(--bz-glass-rim)] rounded w-32" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-[var(--state-danger)]/10 border border-[var(--state-danger)]/30 rounded-xl p-6 text-[var(--state-danger)]">
        <h2 className="font-semibold mb-2">Error</h2>
        <p>{error}</p>
      </div>
    );
  }

  if (!adminData && !personalData) {
    return (
      <div
        className="border rounded-xl p-6 text-[var(--bz-text-2)]"
        style={PANEL}
      >
        No HR record is linked to your account yet. Ask HR to create it.
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {adminData && <AdminDashboardView data={adminData} />}
      {personalData && <PersonalDashboardView data={personalData} />}
    </div>
  );
}
