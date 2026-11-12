/**
 * HR/Payroll API client module
 * Uses the centralized api client for auth token management.
 */

import { api } from "@/lib/api";
import type { components } from "@/lib/api/schema";
import type { CredentialedResponse } from "@/lib/api/types/api-client.types";
import type {
  AdminDashboard,
  Bonus,
  BonusHistoricalRecord,
  BonusRate,
  BonusPracticeType,
  BonusRatePayload,
  EmployeeCreatePayload,
  HREmployee,
  LeaveBalance,
  LeaveRequest,
  LeaveRequestPayload,
  LeaveType,
  ManualBonusPayload,
  PayrollPeriod,
  Payslip,
  PayslipDetail,
  PersonalDashboard,
  TeamLeaveSummaryRow,
} from "@/types/hr";

const BASE = "/api/hr";

function qs(
  params?: Record<string, string | number | null | undefined>,
): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(
    (entry): entry is [string, string | number] => entry[1] != null,
  );
  return entries.length
    ? "?" +
        new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()
    : "";
}

// Capabilities — the server's own answer; no allowlist lives in the browser.
export interface HrCapabilities {
  hr_admin: boolean;
  accounting_admin: boolean;
  has_employee_record: boolean;
  /** Whose HR picture the People console shows: everyone, a few people, or nobody. */
  people_scope: "all" | "scoped" | null;
  /** The viewer may edit HR data (read-only People for everyone else). */
  hr_edit: boolean;
  /** The viewer may edit, clear or create any person's HR card (the global admins only). */
  hr_card_write: boolean;
}
export const getCapabilities = () =>
  api.get<HrCapabilities>(`${BASE}/me/capabilities`);

// Own staff card — the member's own values in full, without the HR notes.
export interface HrContract {
  number?: string | null;
  file_id?: string | null;
}
export interface HrStaffCard {
  employee_id: number;
  full_name: string | null;
  role: string | null;
  department: string | null;
  hire_date: string | null;
  is_active: boolean;
  ptkp_status: string | null;
  base_salary_idr: number | null;
  npwp: string | null;
  bpjs_kesehatan_number: string | null;
  bpjs_ketenagakerjaan_number: string | null;
  bank_name: string | null;
  bank_account: string | null;
  bank_account_holder: string | null;
  legal_name: string | null;
  nik: string | null;
  birth_place: string | null;
  birth_date: string | null;
  gender: string | null;
  address: string | null;
  religion: string | null;
  marital_status: string | null;
  employer_entity: string | null;
  unit: string | null;
  contract: HrContract | null;
  docs: Record<string, string>;
  end_date: string | null;
}
export const getMyCard = () => api.get<HrStaffCard>(`${BASE}/me/card`);

// My HR overview — the member's own picture in one read (Pay, Time, Me). The page
// binds it to the bearer (src/lib/auth/SESSION.md S4), so it reads through
// requestWithCredential and compares `team_member_id` with the sub that was sent.
export interface HrOverviewPayslip {
  id: number;
  payroll_year: number;
  payroll_month: number;
  net_salary_idr: number;
  base_salary_idr: number;
  bonus_total_idr: number;
  allowance_total_idr: number;
  deduction_total_idr: number;
  thr_idr: number;
}
export interface HrOverviewBonus {
  id: number;
  practice_type_code: string;
  awarded_at: string;
  status: string;
  amount_idr: number;
}
export interface HrOverviewLeaveBalance {
  id: number;
  leave_type_id: number;
  code: string;
  leave_type_name: string;
  is_paid: boolean;
  balance_year: number;
  allocated_days: number;
  carried_over: number;
  used_days: number;
  pending_days: number;
}
export interface HrOverviewLeaveRequest {
  id: number;
  leave_type_name: string;
  start_date: string;
  end_date: string;
  total_days: number;
  status: string;
  requested_at: string | null;
}
export interface HrOverviewAttendance {
  /** "YYYY-MM", Bali calendar month. */
  month: string;
  days_present: number;
  hours: number | null;
  /** "HH:MM", Bali time. */
  avg_clock_in: string | null;
  /** null = the late-incident record could not be read. */
  late_incidents: number | null;
}
export interface MyHrOverview {
  /** The owner of this answer: equal to the bearer's sub. */
  team_member_id: string;
  card: HrStaffCard;
  payslips: HrOverviewPayslip[];
  bonuses: HrOverviewBonus[];
  bonus_totals: {
    month_total_idr: number;
    month_count: number;
    year_total_idr: number;
  };
  leave: {
    balances: HrOverviewLeaveBalance[];
    requests: HrOverviewLeaveRequest[];
  };
  attendance: HrOverviewAttendance[];
}
export const getMyHrOverview = (): Promise<
  CredentialedResponse<MyHrOverview>
> => api.requestWithCredential<MyHrOverview>(`${BASE}/me/overview`);

// People console (read side) — the roster and one person's overview. Both answers
// name the bearer they were read for (`viewer_team_member_id`) and the page binds them
// to the session (src/lib/auth/SESSION.md S4), so both read through requestWithCredential.
export type PeopleRoster = components["schemas"]["PeopleRoster"];
export type PersonRow = components["schemas"]["PersonRow"];
export type PeoplePulse = components["schemas"]["PeoplePulse"];
export type PersonOverview = Omit<
  components["schemas"]["PersonOverview"],
  "person"
> & { person: MyHrOverview };
export const getPeople = (): Promise<CredentialedResponse<PeopleRoster>> =>
  api.requestWithCredential<PeopleRoster>(`${BASE}/people`);
export const getPersonOverview = (
  teamMemberId: string,
): Promise<CredentialedResponse<PersonOverview>> =>
  api.requestWithCredential<PersonOverview>(
    `${BASE}/people/${encodeURIComponent(teamMemberId)}/overview`,
  );

// People console, card edit — STAFF_CARD.md C11. Only the global admins may call it; the
// answer is the person's refreshed overview, bound to the session like the read.
export type CardEditBody = components["schemas"]["CardEditBody"];
/** One entry of a 422: a closed field key (or ""), a code and a fixed message; never a value. */
export interface CardEditFieldError {
  field: string;
  code: string;
  message: string;
}
export const editPersonCard = (
  teamMemberId: string,
  body: CardEditBody,
): Promise<CredentialedResponse<PersonOverview>> =>
  api.requestWithCredential<PersonOverview>(
    `${BASE}/people/${encodeURIComponent(teamMemberId)}/card`,
    { method: "PATCH", body: JSON.stringify(body) },
  );

// Dashboard
export const getDashboard = () => api.get<AdminDashboard>(`${BASE}/dashboard`);
export const getMyDashboard = () =>
  api.get<PersonalDashboard>(`${BASE}/my-dashboard`);

// Employees
export const listEmployees = () =>
  api.get<{ employees: HREmployee[]; count: number }>(`${BASE}/employees`);
export const getEmployee = (id: number) =>
  api.get<HREmployee>(`${BASE}/employees/${id}`);
export const upsertEmployee = (data: EmployeeCreatePayload) =>
  api.post<{ success: boolean; employee: HREmployee }>(
    `${BASE}/employees`,
    data,
  );

// Bonus Rates
export const listBonusRates = () =>
  api.get<{ rates: BonusRate[]; count: number }>(`${BASE}/bonus-rates`);
export const upsertBonusRate = (data: BonusRatePayload) =>
  api.post<{ success: boolean; rate: BonusRate }>(`${BASE}/bonus-rates`, data);

/** HR-admin only. Active practice types, tax included, grouped server-side. */
export const listBonusPracticeTypes = () =>
  api.get<{ practice_types: BonusPracticeType[]; count: number }>(
    `${BASE}/bonus-practice-types`,
  );

// Bonuses
export const listBonuses = (
  params?: Record<string, string | number | null | undefined>,
) =>
  api.get<{ bonuses: Bonus[]; count: number }>(`${BASE}/bonuses${qs(params)}`);
export const approveBonus = (id: number) =>
  api.post<{ success: boolean; bonus: Bonus }>(
    `${BASE}/bonuses/${id}/approve`,
    {},
  );
/** HR-admin only. Starts pending; 409 when that payroll month is already locked. */
export const createManualBonus = (data: ManualBonusPayload) =>
  api.post<{ success: boolean; bonus: Bonus }>(`${BASE}/bonuses`, data);
export const rejectBonus = (id: number, note?: string) =>
  api.post<{ success: boolean; bonus: Bonus }>(
    `${BASE}/bonuses/${id}/reject`,
    note ? { note } : {},
  );
/** Only a pending MANUAL bonus can be deleted (409 otherwise). */
export const deleteBonus = (id: number) =>
  api.delete<{ success: boolean }>(`${BASE}/bonuses/${id}`);
/** HR-admin only — 403 for team members. Callers must tolerate the rejection. */
export const listBonusHistorical = (year?: number) =>
  api.get<{ records: BonusHistoricalRecord[]; count: number }>(
    `${BASE}/bonuses/historical${qs({ year })}`,
  );

// Payroll
export const calculatePayroll = (month: number, year: number) =>
  api.post<{ success: boolean; period_id: number; employee_count: number }>(
    `${BASE}/payroll/calculate`,
    { month, year },
  );
export const listPayrollPeriods = () =>
  api.get<{ periods: PayrollPeriod[]; count: number }>(
    `${BASE}/payroll/periods`,
  );
export const listPayslips = (
  params?: Record<string, string | number | null | undefined>,
) =>
  api.get<{ payslips: Payslip[]; count: number }>(
    `${BASE}/payslips${qs(params)}`,
  );
export const getPayslipDetail = (id: number) =>
  api.get<PayslipDetail>(`${BASE}/payslips/${id}`);
export const approvePayroll = (periodId: number) =>
  api.post<{ success: boolean; period: PayrollPeriod }>(
    `${BASE}/payroll/${periodId}/approve`,
    {},
  );
export const markPayrollPaid = (periodId: number) =>
  api.post<{ success: boolean; period: PayrollPeriod }>(
    `${BASE}/payroll/${periodId}/mark-paid`,
    {},
  );

// Leave Types — backend /leave/types returns active rows only,
// projected to the columns the dropdown needs.
export type LeaveTypeOption = Pick<
  LeaveType,
  "id" | "code" | "name" | "default_days" | "is_paid" | "requires_document"
>;

export const getLeaveTypes = () =>
  api.get<{ leave_types: LeaveTypeOption[] }>(`${BASE}/leave/types`);

// Leave
export const requestLeave = (data: LeaveRequestPayload) =>
  api.post<{ success: boolean; request: LeaveRequest }>(
    `${BASE}/leave/request`,
    data,
  );
export const listLeaveRequests = (
  params?: Record<string, string | number | null | undefined>,
) =>
  api.get<{ requests: LeaveRequest[]; count: number }>(
    `${BASE}/leave/requests${qs(params)}`,
  );
export const getLeaveBalance = (
  params?: Record<string, string | number | null | undefined>,
) =>
  api.get<{ balances: LeaveBalance[] }>(`${BASE}/leave/balance${qs(params)}`);
export const getTeamLeaveSummary = (
  params?: Record<string, string | number | null | undefined>,
) =>
  api.get<{ summary: TeamLeaveSummaryRow[] }>(
    `${BASE}/leave/team-summary${qs(params)}`,
  );
export const approveLeave = (id: number) =>
  api.post<{ success: boolean; request: LeaveRequest }>(
    `${BASE}/leave/${id}/approve`,
    {},
  );
export const rejectLeave = (id: number, reason?: string) =>
  api.post<{ success: boolean; request: LeaveRequest }>(
    `${BASE}/leave/${id}/reject`,
    { reason },
  );
