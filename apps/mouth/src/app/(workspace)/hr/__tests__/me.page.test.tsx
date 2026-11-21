import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { api } from "@/lib/api";
import type { MyHrOverview } from "@/lib/api/hr/hr";
import { tokenFor } from "@/lib/auth/session-test-utils";
import MyHrPage from "../me/page";

// A pending fetch the test resolves by hand; `auth` records the bearer the
// request really carried.
interface Pending {
  auth: string | undefined;
  resolve: (body: unknown, status?: number) => void;
}
let pending: Pending[];

function mockTransport() {
  pending = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => {
    const headers = (init as { headers: Record<string, string> }).headers;
    return new Promise<Response>((resolve) => {
      pending.push({
        auth: headers["Authorization"],
        resolve: (body, status = 200) =>
          resolve(
            new Response(JSON.stringify(body), {
              status,
              headers: { "Content-Type": "application/json" },
            }),
          ),
      });
    });
  });
}

const WITA_DAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Makassar",
});
const THIS_MONTH = WITA_DAY.format(new Date()).slice(0, 7);

// Synthetic data only: no real person, zero-filled numbers.
function overview(owner: string, name = "Made Contoh"): MyHrOverview {
  return {
    team_member_id: owner,
    card: {
      employee_id: 1,
      full_name: name,
      role: "Staff",
      department: "Ops",
      hire_date: "2024-01-15",
      is_active: true,
      ptkp_status: "TK/0",
      base_salary_idr: 1000,
      npwp: null,
      bpjs_kesehatan_number: null,
      bpjs_ketenagakerjaan_number: null,
      bank_name: "Contoh Bank",
      bank_account: "000000000",
      bank_account_holder: "CONTOH",
      legal_name: name,
      nik: null,
      birth_place: null,
      birth_date: null,
      gender: null,
      address: null,
      religion: null,
      marital_status: null,
      employer_entity: "PT Contoh",
      unit: "Unit Contoh",
      contract: { number: "C-000" },
      documents: {
        ktp: { present: false, file_id: null },
        kk: { present: false, file_id: null },
        npwp: { present: false, file_id: null },
        bpjs_kes: { present: false, file_id: null },
        bpjs_tk: { present: false, file_id: null },
        contract: { present: false, file_id: null },
      },
      end_date: null,
    },
    payslips: [
      {
        id: 31,
        payroll_year: 2026,
        payroll_month: 10,
        net_salary_idr: 1111,
        base_salary_idr: 1000,
        bonus_total_idr: 200,
        allowance_total_idr: 0,
        deduction_total_idr: 89,
        thr_idr: 0,
      },
      {
        id: 30,
        payroll_year: 2026,
        payroll_month: 9,
        net_salary_idr: 1000,
        base_salary_idr: 1000,
        bonus_total_idr: 0,
        allowance_total_idr: 0,
        deduction_total_idr: 0,
        thr_idr: 0,
      },
    ],
    bonuses: [
      {
        id: 5,
        practice_type_code: "VISA",
        awarded_at: new Date().toISOString(),
        status: "approved",
        amount_idr: 500,
      },
    ],
    bonus_totals: { month_total_idr: 500, month_count: 1, year_total_idr: 500 },
    leave: {
      balances: [
        {
          id: 1,
          leave_type_id: 1,
          code: "annual",
          leave_type_name: "Annual leave",
          is_paid: true,
          balance_year: 2026,
          allocated_days: 12,
          carried_over: 0,
          used_days: 2,
          pending_days: 1,
        },
      ],
      requests: [
        {
          id: 9,
          leave_type_name: "Annual leave",
          start_date: "2026-12-01",
          end_date: "2026-12-02",
          total_days: 2,
          status: "pending",
          requested_at: null,
        },
      ],
    },
    attendance: [
      {
        month: THIS_MONTH,
        days_present: 4,
        hours: 32,
        avg_clock_in: "09:10",
        late_incidents: 1,
      },
    ],
  };
}

const EMPTY = (owner: string): MyHrOverview => ({
  ...overview(owner),
  payslips: [],
  bonuses: [],
  bonus_totals: { month_total_idr: 0, month_count: 0, year_total_idr: 0 },
  leave: { balances: [], requests: [] },
  attendance: [],
});

const settle = () => act(async () => {});
const storageEvent = (key: string) =>
  window.dispatchEvent(new StorageEvent("storage", { key }));
const shown = (name: string) => screen.queryByText(new RegExp(name));

async function loaded(sub: string, body: MyHrOverview) {
  localStorage.setItem("auth_token", tokenFor(sub));
  render(<MyHrPage />);
  await waitFor(() => expect(pending).toHaveLength(1));
  await act(async () => pending[0].resolve(body));
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
  mockTransport();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("My HR page (/hr/me), off-payroll viewer", () => {
  it("shows no Pay panel, payslip, bonus or first-payslip nag, but keeps Time and Me", async () => {
    const off: MyHrOverview = {
      ...EMPTY("A"),
      card: { ...overview("A").card, on_payroll: false, base_salary_idr: null },
    };
    await loaded("A", off);
    expect(screen.queryByRole("button", { name: /^Pay/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Time/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Me/ })).toBeInTheDocument();
    expect(shown("payslip")).toBeNull();
    expect(shown("bonus")).toBeNull();
    expect(shown("Take-home")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain(
      "Made",
    );
  });

  it("an on-payroll viewer still gets the Pay panel", async () => {
    await loaded("A", overview("A"));
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeInTheDocument();
  });
});

describe("My HR page (/hr/me)", () => {
  it("shows the member's portrait in the header when the card carries one", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<MyHrPage />);
    await waitFor(() => expect(pending).toHaveLength(1));
    const body = overview("A");
    body.card.avatar_url = "/static/team/contoh.jpg";
    await act(async () => pending[0].resolve(body));
    expect(screen.getByAltText("Made Contoh")).toHaveAttribute(
      "src",
      "/static/team/contoh.jpg",
    );
  });

  it("shows initials, and no image, when the card has no avatar", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<MyHrPage />);
    await waitFor(() => expect(pending).toHaveLength(1));
    const body = overview("A");
    body.card.avatar_url = null;
    await act(async () => pending[0].resolve(body));
    expect(screen.queryByAltText("Made Contoh")).toBeNull();
    expect(screen.getByRole("img", { name: "Made Contoh" })).toHaveTextContent(
      "MC",
    );
  });

  it("reads the overview with the bearer and renders Pay, Time and Me", async () => {
    const token = tokenFor("A");
    localStorage.setItem("auth_token", token);
    render(<MyHrPage />);
    await waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].auth).toBe(`Bearer ${token}`);
    await act(async () => pending[0].resolve(overview("A")));

    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain(
      "Made",
    );
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Time/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Me/ })).toBeInTheDocument();
    // Pay panel: take-home of the latest payslip, bonus chip from the server totals.
    expect(screen.getAllByText(/1\.111/).length).toBeGreaterThan(0);
    expect(screen.getByText(/bonuses this month/)).toBeInTheDocument();
    // Detail below: payslip rows link to the existing detail page.
    const link = screen.getByRole("link", { name: /October 2026/ });
    expect(link).toHaveAttribute("href", "/hr/payroll/31");
    expect(
      screen.getByText(/Base Rp\s1\.000 · bonuses Rp\s200 · deductions Rp\s89/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "You can see this, and so can the people authorised to manage HR at Bali Zero.",
      ),
    ).toBeInTheDocument();
    expect(document.querySelector("[data-sentry-block]")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Time/ }));
    expect(screen.getByText(/Request leave/).closest("a")).toHaveAttribute(
      "href",
      "/hr/leave/request",
    );
    expect(screen.getByText(/9 days left/)).toBeInTheDocument();
    expect(screen.getAllByText(/3 on time · 1 late/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(screen.getByText("About you")).toBeInTheDocument();
  });

  it("A -> B in another tab clears A at once and shows B once B's response is accepted", async () => {
    await loaded("A", overview("A", "Made Contoh"));
    expect(shown("Made")).not.toBeNull();

    const tokenB = tokenFor("B");
    localStorage.setItem("auth_token", tokenB);
    act(() => storageEvent("auth_token"));
    expect(shown("Made")).toBeNull(); // A's data is gone before B's arrives
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].auth).toBe(`Bearer ${tokenB}`);
    await act(async () => pending[1].resolve(overview("B", "Ketut Contoh")));
    expect(shown("Ketut")).not.toBeNull();
    expect(shown("Made")).toBeNull();
  });

  it("logout clears the page and a late response is dropped", async () => {
    await loaded("A", overview("A"));
    // A reload in flight when the user signs out.
    act(() => storageEvent("auth_token")); // re-binds: fetch #2
    await waitFor(() => expect(pending).toHaveLength(2));
    act(() => api.clearToken());
    expect(shown("Made")).toBeNull();
    await act(async () => pending[1].resolve(overview("A")));
    expect(shown("Made")).toBeNull();
    expect(pending).toHaveLength(2); // nothing refetched without an identity
  });

  it("a response whose team_member_id differs from the sent sub is not shown", async () => {
    await loaded("A", overview("B", "Ketut Contoh"));
    expect(shown("Ketut")).toBeNull();
    expect(shown("Contoh")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(/session changed/i);
  });

  it("a response whose request went out cookie-only (no bearer) is not shown", async () => {
    const real = api.requestWithCredential.bind(api);
    vi.spyOn(api, "requestWithCredential").mockImplementation((e, o, t) => {
      localStorage.removeItem("auth_token"); // another tab logged out, no event yet
      return real(e, o, t);
    });
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<MyHrPage />);
    await waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].auth).toBeUndefined();
    await act(async () => pending[0].resolve(overview("A")));
    expect(shown("Made")).toBeNull();
    expect(shown("Contoh")).toBeNull();
  });

  it("with no bearer at all nothing is requested and nothing is shown", async () => {
    render(<MyHrPage />);
    await settle();
    await settle();
    expect(pending).toHaveLength(0);
    expect(shown("Made")).toBeNull();
  });

  it("empty states: no payslip, no leave balance, no attendance, no bonus", async () => {
    await loaded("A", EMPTY("A"));
    expect(
      screen.getByText(/Your first payslip is on its way/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Your first payslip will show up here after payday."),
    ).toBeInTheDocument();
    expect(screen.getByText("No bonus yet this month")).toBeInTheDocument();
    expect(screen.getByText(/No bonuses yet this year/)).toBeInTheDocument();
    expect(screen.getByText("No leave balance yet")).toBeInTheDocument();
    expect(screen.getByText("No clock-in this month yet")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Time/ }));
    expect(
      screen.getByText(/You have not asked for any leave yet/),
    ).toBeInTheDocument();
    expect(screen.getByText("No clock-in recorded yet.")).toBeInTheDocument();
  });

  it("an unreadable late record never claims 'never late'", async () => {
    const body = overview("A");
    body.attendance = [{ ...body.attendance[0], late_incidents: null }];
    await loaded("A", body);
    expect(screen.queryByText(/never late/)).toBeNull();
    expect(screen.getByText(/Usually in at 09:10/)).toBeInTheDocument();
  });

  it("a member without an HR record is told so, not shown an error", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<MyHrPage />);
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () => pending[0].resolve({ detail: "no record" }, 404));
    expect(screen.getByText(/No HR record is linked/)).toBeInTheDocument();
  });

  it("shows a legacy paper as exactly 'On file — not yet in DOKUMEN KARYAWAN', no link, no value", async () => {
    const base = overview("A");
    const legacy: MyHrOverview = {
      ...base,
      card: {
        ...base.card,
        documents: {
          ...base.card.documents,
          ktp: { present: true, file_id: null },
        },
      },
    };
    await loaded("A", legacy);
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(
      screen.getByText("ID card (KTP) · On file — not yet in DOKUMEN KARYAWAN"),
    ).toBeInTheDocument();
    expect(document.querySelector('a[href*="drive.google.com"]')).toBeNull();
  });
});
