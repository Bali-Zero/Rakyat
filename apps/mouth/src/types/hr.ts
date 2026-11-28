/**
 * HR/Payroll TypeScript types.
 * Maps to backend Pydantic models and DB schema (migration_068).
 */

// ─── Enums ────────────────────────────────────────────────────────────

export type BonusStatus =
  "pending" | "approved" | "rejected" | "paid" | "reversed";
export type PayrollStatus = "draft" | "calculated" | "approved" | "paid";
export type LeaveStatus = "pending" | "approved" | "rejected" | "cancelled";
export type PTKPStatus =
  "TK/0" | "TK/1" | "TK/2" | "TK/3" | "K/0" | "K/1" | "K/2" | "K/3";

// ─── Employees ────────────────────────────────────────────────────────

export interface HREmployee {
  id: number;
  team_member_id: string;
  full_name: string;
  email: string;
  role: string;
  /** Org-chart department chip: tax | setup | office (server-derived). */
  department?: string;
  hire_date: string;
  base_salary_idr: number;
  bank_name: string | null;
  bank_account: string | null;
  bank_account_holder: string | null;
  npwp: string | null;
  ptkp_status: PTKPStatus;
  bpjs_kesehatan_number: string | null;
  bpjs_ketenagakerjaan_number: string | null;
  is_active: boolean;
  /** false = the card says not on payroll (server-derived; absent means true). */
  on_payroll?: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface EmployeeCreatePayload {
  team_member_id: string;
  hire_date: string;
  base_salary_idr: number;
  bank_name?: string;
  bank_account?: string;
  bank_account_holder?: string;
  npwp?: string;
  ptkp_status?: PTKPStatus;
  bpjs_kesehatan_number?: string;
  bpjs_ketenagakerjaan_number?: string;
  notes?: string;
}

// ─── Bonus Rates ──────────────────────────────────────────────────────

export interface BonusRate {
  id: number;
  practice_type_code: string;
  practice_type_name: string | null;
  amount_idr: number;
  effective_from: string;
  effective_to: string | null;
  is_active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface BonusRatePayload {
  practice_type_code: string;
  amount_idr: number;
  effective_from?: string;
  effective_to?: string;
  is_active?: boolean;
  notes?: string;
}

// ─── Bonuses ──────────────────────────────────────────────────────────

/** practice = created by completing a practice; manual = entered by an HR admin. */
export type BonusSource = "practice" | "manual";

export interface Bonus {
  id: number;
  /** null for a manual bonus. */
  practice_id: number | null;
  employee_id: number;
  payroll_period_id: number | null;
  /** The bonus run (first of the month) that pays this row; null before the run or for older rows. */
  bonus_run_id?: string | null;
  bonus_rate_id: number | null;
  practice_type_code: string;
  amount_idr: number;
  status: BonusStatus;
  awarded_at: string;
  awarded_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  employee_name: string;
  employee_email: string;
  /** Org-chart department chip: tax | setup | office. */
  employee_department?: string;
  practice_status: string | null;
  client_name: string | null;
  notes: string | null;
  source: BonusSource;
  /** Why a manual bonus was given. */
  reason?: string | null;
  /** Who entered a manual bonus (HR-admin view only). */
  created_by?: string | null;
}

/** An active practice type offered in the rate and manual-bonus dropdowns. */
export type PracticeGroupId = "visa" | "company" | "tax" | "other";
export interface BonusPracticeType {
  code: string;
  name: string;
  category: string | null;
  group: PracticeGroupId;
}

export interface ManualBonusPayload {
  employee_id: number;
  amount_idr: number;
  reason: string;
  practice_type_code?: string | null;
  /** YYYY-MM-DD, WITA calendar day; the server defaults to today. */
  awarded_on?: string;
}

/**
 * Pre-system bonus recap imported from PDF (migration 099).
 *
 * A SEPARATE source from `Bonus`/`hr_bonus_ledger`: the two overlap on some
 * months with different totals. Show side by side for reconciliation —
 * never add the two together.
 */
export interface BonusHistoricalRecord {
  id: number;
  employee_name: string;
  employee_id: number | null;
  bonus_month: number;
  bonus_year: number;
  total_amount_idr: number;
  task_count: number;
  source_pdf: string;
  accounting_total_data: number | null;
  accounting_not_paid: number | null;
  accounting_paid: number | null;
  imported_at: string;
  notes: string | null;
}

// ─── Payroll ──────────────────────────────────────────────────────────

export interface PayrollPeriod {
  id: number;
  payroll_month: number;
  payroll_year: number;
  period_start: string;
  period_end: string;
  status: PayrollStatus;
  locked_at: string | null;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  paid_at: string | null;
  notes: string | null;
  payslip_count: number;
  total_net: number;
}

/** One bonus month from Nov 2026: gross amounts only (bonuses carry no tax). Oldest first from the API. */
export interface BonusRun {
  /** First of the month, YYYY-MM-DD. */
  bonus_month: string;
  status: "open" | "approved" | "paid";
  ended: boolean;
  can_approve: boolean;
  approved_at: string | null;
  approved_by: string | null;
  paid_at: string | null;
  approved_total_idr: number;
  approved_count: number;
  pending_count: number;
  pending_total_idr: number;
  held_count: number;
  held_total_idr: number;
  /** YYYY-MM-DD, or null while the salary it waits on is unpaid. */
  expected_payday: string | null;
  /** First of the salary month the payday waits on, YYYY-MM-DD. */
  payday_salary_month: string;
}

export interface Payslip {
  id: number;
  payroll_period_id: number;
  employee_id: number;
  base_salary_idr: number;
  bonus_total_idr: number;
  allowance_total_idr: number;
  deduction_total_idr: number;
  thr_idr: number;
  net_salary_idr: number;
  employee_name: string;
  employee_email: string;
  payroll_month: number;
  payroll_year: number;
  period_status: PayrollStatus;
  notes: string | null;
}

export interface PayslipDetail extends Payslip {
  full_name: string;
  email: string;
  deductions: Deduction[];
}

export interface Deduction {
  id: number;
  payslip_id: number;
  deduction_type: string;
  label: string;
  amount_idr: number;
  is_employer: boolean;
}

// ─── Leave ────────────────────────────────────────────────────────────

export interface LeaveType {
  id: number;
  code: string;
  name: string;
  description: string | null;
  default_days: number;
  is_paid: boolean;
  requires_document: boolean;
  is_active: boolean;
}

export interface LeaveBalance {
  id: number;
  employee_id: number;
  leave_type_id: number;
  leave_type_name: string;
  code: string;
  is_paid: boolean;
  balance_year: number;
  allocated_days: number;
  carried_over: number;
  used_days: number;
  pending_days: number;
}

export interface LeaveRequest {
  id: number;
  employee_id: number;
  leave_type_id: number;
  leave_type_name: string;
  start_date: string;
  end_date: string;
  total_days: number;
  reason: string | null;
  status: LeaveStatus;
  requested_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  rejection_reason: string | null;
  employee_name: string;
  employee_email: string;
  /** Server-computed: the caller owns this request. Absent on older responses = treat as own. */
  is_mine?: boolean;
  /** Server-computed: the caller may approve/reject this row (admin or the requester's approver). */
  can_review?: boolean;
}

export interface TeamLeaveSummaryRow {
  employee_name: string;
  email: string;
  employee_id: number;
  leave_type: string;
  leave_type_name: string;
  allocated_days: number;
  used_days: number;
  pending_days: number;
  carried_over: number;
  remaining_days: number;
}

export interface LeaveRequestPayload {
  leave_type_id: number;
  start_date: string;
  end_date: string;
  total_days: number;
  reason?: string;
}

// ─── Dashboard ────────────────────────────────────────────────────────

export interface AdminDashboard {
  employee_count: number;
  pending_bonuses: { count: number; total: number };
  pending_leave_requests: number;
  current_period: {
    id: number;
    status: PayrollStatus;
    payroll_month: number;
    payroll_year: number;
  } | null;
  month: number;
  year: number;
}

export interface PersonalDashboard {
  /** false = the card says not on payroll: no payslip and no bonus totals are sent. */
  on_payroll: boolean;
  month_bonuses?: { count: number; total: number };
  latest_payslip?: {
    net_salary_idr: number;
    payroll_month: number;
    payroll_year: number;
  } | null;
  leave_balances: LeaveBalance[];
}

// ─── API Response Wrappers ────────────────────────────────────────────

export interface ListResponse<T> {
  count: number;
  [key: string]: T[] | number;
}

export interface MutationResponse<T = unknown> {
  success: boolean;
  [key: string]: T | boolean;
}
