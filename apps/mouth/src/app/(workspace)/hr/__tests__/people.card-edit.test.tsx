import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const nav = vi.hoisted(() => ({
  replace: vi.fn(),
  person: null as string | null,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace }),
  useSearchParams: () => ({
    get: (k: string) => (k === "person" ? nav.person : null),
  }),
}));

const caps = (write: boolean, scope: "all" | "scoped" = "all") => ({
  capabilities: {
    hr_admin: scope === "all",
    accounting_admin: false,
    has_employee_record: true,
    people_scope: scope,
    hr_edit: scope === "all",
    hr_card_write: write,
  },
  loaded: true,
  failed: false,
});
const capsState = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock("@/lib/hr/use-hr-capabilities", () => ({
  useHrCapabilities: () => capsState.value,
}));

import type {
  MyHrOverview,
  PeopleRoster,
  PersonOverview,
} from "@/lib/api/hr/hr";
import { tokenFor } from "@/lib/auth/session-test-utils";
import PeoplePage from "../page";

interface Pending {
  url: string;
  method: string;
  body: string | undefined;
  resolve: (body: unknown, status?: number) => void;
}
let pending: Pending[];
function mockTransport() {
  pending = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((url, init) => {
    const i = init as { method?: string; body?: string };
    return new Promise<Response>((resolve) => {
      pending.push({
        url: String(url),
        method: i.method ?? "GET",
        body: i.body,
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
const call = (suffix: string, method = "GET") =>
  pending.filter((p) => p.url.endsWith(suffix) && p.method === method);

// Synthetic data only.
const ID_A = "11111111-1111-4111-8111-111111111111";
const ID_B = "22222222-2222-4222-8222-222222222222";
const ID_N = "33333333-3333-4333-8333-333333333333";
const FIX = {
  legal_name: "Fixture Legalname Zeta",
  nik: "3171234567890001",
  npwp: "123456789012345",
  bank_account: "0099887766",
  bank_account_holder: "FIXTURE HOLDER ZETA",
  address: "Fixture Lane 77, Nowhere",
  birth_place: "Fixtureville",
  contract_number: "CN-FIX-4242",
  employer_entity: "PT Fixture Entity",
  bank_name: "Fixture Bank Zeta",
  unit: "Fixture Unit Zeta",
};
const NEW_NIK = "3171234567890099";

function roster(viewer: string, scope: "all" | "scoped" = "all"): PeopleRoster {
  const flags = {
    papers_missing: 3,
    bonuses_pending: 0,
    leave_pending: 0,
    on_leave_today: false,
  };
  return {
    viewer_team_member_id: viewer,
    scope,
    can_edit: scope === "all",
    people: [
      {
        team_member_id: ID_A,
        employee_id: 1,
        has_record: true,
        name: "Made Contoh",
        role: "staff",
        department: "setup",
        unit: FIX.unit,
        hire_date: "2024-01-15",
        active: true,
        flags,
      },
      {
        team_member_id: ID_B,
        employee_id: 2,
        has_record: true,
        name: "Wayan Dua",
        role: "staff",
        department: "tax",
        unit: "Unit Bee",
        hire_date: "2024-02-01",
        active: true,
        flags,
      },
      {
        team_member_id: ID_N,
        employee_id: null,
        has_record: false,
        name: "Ketut Tanpa",
        role: "staff",
        department: "tax",
        unit: null,
        hire_date: null,
        active: true,
        flags: { ...flags, papers_missing: null },
      },
    ],
    pulse:
      scope === "all"
        ? {
            active_people: 2,
            people_without_record: 1,
            bonuses_pending_count: 0,
            bonuses_pending_idr: 0,
            leave_pending_count: 0,
            payroll: { month: 11, year: 2026, status: null },
          }
        : null,
  };
}

function overview(
  owner: string,
  over: Record<string, unknown> = {},
): MyHrOverview {
  return {
    team_member_id: owner,
    card: {
      employee_id: 1,
      full_name: "Made Contoh",
      role: "staff",
      department: "setup",
      hire_date: "2024-01-15",
      is_active: true,
      ptkp_status: "TK/0",
      base_salary_idr: 1000,
      npwp: FIX.npwp,
      bpjs_kesehatan_number: null,
      bpjs_ketenagakerjaan_number: null,
      bank_name: FIX.bank_name,
      bank_account: FIX.bank_account,
      bank_account_holder: FIX.bank_account_holder,
      legal_name: FIX.legal_name,
      nik: FIX.nik,
      birth_place: FIX.birth_place,
      birth_date: "1990-02-03",
      gender: null,
      address: FIX.address,
      religion: null,
      marital_status: null,
      employer_entity: FIX.employer_entity,
      unit: FIX.unit,
      contract: { number: FIX.contract_number, file_id: null },
      docs: {},
      end_date: null,
      ...over,
    },
    payslips: [],
    bonuses: [],
    bonus_totals: { month_total_idr: 0, month_count: 0, year_total_idr: 0 },
    leave: { balances: [], requests: [] },
    attendance: [],
  };
}
const person = (viewer: string, id: string, over = {}): PersonOverview => ({
  viewer_team_member_id: viewer,
  person: overview(id, over),
});

async function open(sub: string, scope: "all" | "scoped" = "all") {
  localStorage.setItem("auth_token", tokenFor(sub));
  render(<PeoplePage />);
  await waitFor(() => expect(call("/api/hr/people")).toHaveLength(1));
  await act(async () => call("/api/hr/people")[0].resolve(roster(sub, scope)));
}
async function openA(sub = "A") {
  await open(sub);
  await waitFor(() =>
    expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
  );
  await act(async () =>
    call(`/api/hr/people/${ID_A}/overview`)[0].resolve(person(sub, ID_A)),
  );
}
const field = (label: string) =>
  screen.getByLabelText(new RegExp(`^${label}`)) as
    HTMLInputElement | HTMLTextAreaElement;
const type = (label: string, value: string) =>
  fireEvent.change(field(label), { target: { value } });

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
  nav.person = null;
  capsState.value = caps(true);
  mockTransport();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("People card edit (/hr)", () => {
  it("shows Edit only to a viewer who may write cards", async () => {
    await openA();
    expect(screen.getByRole("button", { name: /^Edit$/ })).toBeInTheDocument();
  });

  it.each([
    ["an HR admin without card writes", caps(false)],
    ["a scoped viewer", caps(false, "scoped")],
    ["a scoped viewer even if the flag were set", caps(true, "scoped")],
  ])("shows no edit control to %s", async (_n, state) => {
    capsState.value = state;
    nav.person = ID_N;
    await open(
      "A",
      state.capabilities.people_scope === "scoped" ? "scoped" : "all",
    );
    expect(screen.queryByRole("button", { name: /^Edit$/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add to HR/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Create record/ })).toBeNull();
  });

  it("opens the form filled through DOM properties and never puts a personal value in an attribute", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    expect(field("NIK").value).toBe(FIX.nik);
    expect(field("Legal name").value).toBe(FIX.legal_name);
    expect(field("Address").value).toBe(FIX.address);
    expect(field("Contract number").value).toBe(FIX.contract_number);
    type("NIK", NEW_NIK);
    expect(field("NIK").value).toBe(NEW_NIK);

    // Every personal value of the fixture, raw and in the formats a formatter would produce
    // (PTKP is a constant option list chosen through the DOM property, so it is not personal here).
    const personal = [
      ...Object.values(FIX),
      NEW_NIK,
      "1990-02-03",
      "1990",
      `${FIX.npwp.slice(0, 2)}.${FIX.npwp.slice(2, 5)}.${FIX.npwp.slice(5, 8)}`,
      `${FIX.nik.slice(0, 4)} ${FIX.nik.slice(4, 8)}`,
      `${NEW_NIK.slice(0, 4)}-${NEW_NIK.slice(4, 8)}`,
      FIX.bank_account.replace(/^(\d{4})(\d{4})(\d+)$/, "$1 $2 $3"),
    ];
    const regions = document.querySelectorAll(
      "[data-sentry-block], .sentry-block",
    );
    expect(regions.length).toBeGreaterThan(0);
    const leaks: string[] = [];
    const recorded = ["aria-label", "title", "name", "alt", "id"];
    const fixed = new Set(["Search people"]);
    const recordedValues = new Set<string>();
    for (const region of regions) {
      for (const el of [region, ...Array.from(region.querySelectorAll("*"))]) {
        for (const attr of el.getAttributeNames()) {
          const v = el.getAttribute(attr) ?? "";
          if (recorded.includes(attr) && v && !fixed.has(v))
            recordedValues.add(`${attr}=${v}`);
          for (const p of personal)
            if (v.toLowerCase().includes(p.toLowerCase()))
              leaks.push(`${el.tagName}[${attr}]`);
        }
      }
    }
    expect(leaks).toEqual([]);
    expect([...recordedValues]).toEqual([]);
    // the form itself is inside the protected region
    expect(field("NIK").closest("[data-sentry-block]")).not.toBeNull();
  });

  it("saves only the changed keys, sends emptied fields under clear, and shows the new card", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("NIK", NEW_NIK);
    type("Bank name", "Bank Baru");
    type("Address", "");
    type("Contract number", "");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    const sent = JSON.parse(
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].body!,
    );
    expect(sent).toEqual({
      card: { bank_name: "Bank Baru", profile: { nik: NEW_NIK } },
      clear: ["address", "contract_number"],
    });
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        person("A", ID_A, {
          nik: NEW_NIK,
          bank_name: "Bank Baru",
          address: null,
          unit: "Unit Baru",
          legal_name: "Nama Baru Contoh",
        }),
      ),
    );
    expect(screen.queryByRole("button", { name: /^Save$/ })).toBeNull();
    expect(screen.getAllByText(/Unit Baru/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(screen.getAllByText("Nama Baru Contoh").length).toBeGreaterThan(0);
  });

  it("sends nothing when nothing changed", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(await screen.findByText("Nothing changed.")).toBeInTheDocument();
    expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(0);
  });

  it("validates the identifiers on the client before sending", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("NIK", "1234");
    type("NPWP", "12");
    type("Account number", "12");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(await screen.findByText("Must be 16 digits")).toBeInTheDocument();
    expect(screen.getByText("Must be 15 or 16 digits")).toBeInTheDocument();
    expect(screen.getByText("Must be 6 to 20 digits")).toBeInTheDocument();
    expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(0);
  });

  it("a hire date cannot be emptied", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("Hire date", "");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(await screen.findByText("Required")).toBeInTheDocument();
    expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(0);
  });

  it("shows a server 422 under its field with the server's fixed message and no value", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("Unit", "Whatever Unit");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        {
          detail: [
            { field: "unit", code: "invalid", message: "Server says no" },
            { field: "", code: "invalid", message: "General server problem" },
          ],
        },
        422,
      ),
    );
    expect(await screen.findByText("Server says no")).toBeInTheDocument();
    expect(screen.getByText("General server problem")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Save$/ })).toBeInTheDocument();
    expect(screen.queryByText(/Whatever Unit/)).toBeNull();
  });

  it("creates a record for a person without one: Add to HR needs a hire date", async () => {
    nav.person = ID_N;
    await open("A");
    expect(screen.queryByRole("link", { name: /Create record/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Add to HR/ }));
    type("Unit", "Unit Baru");
    fireEvent.click(screen.getByRole("button", { name: /^Add to HR$/ }));
    expect(await screen.findByText("Required")).toBeInTheDocument();
    expect(call(`/api/hr/people/${ID_N}/card`, "PATCH")).toHaveLength(0);
    type("Hire date", "2026-03-01");
    fireEvent.click(screen.getByRole("button", { name: /^Add to HR$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_N}/card`, "PATCH")).toHaveLength(1),
    );
    expect(
      JSON.parse(call(`/api/hr/people/${ID_N}/card`, "PATCH")[0].body!),
    ).toEqual({
      card: { hire_date: "2026-03-01", profile: { unit: "Unit Baru" } },
    });
    await act(async () =>
      call(`/api/hr/people/${ID_N}/card`, "PATCH")[0].resolve(
        person("A", ID_N, { hire_date: "2026-03-01", unit: "Unit Baru" }),
      ),
    );
    expect(screen.queryByText(/There is nothing to show/)).toBeNull();
  });

  it("discards a PATCH answer that names another reader than the bearer it was sent with", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("Unit", "Unit Baru");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        person("OTHER", ID_A, { unit: "Unit Intruso" }),
      ),
    );
    expect(screen.queryByText(/Unit Intruso/)).toBeNull();
    expect(screen.getByText(/session changed/i)).toBeInTheDocument();
  });

  it("keeps the pulse in step with a save that toggles Active", async () => {
    await openA();
    const count = () =>
      within(
        screen.getByText(/^Active people/).parentElement!.parentElement!,
      ).getByText(/^\d+$/).textContent;
    expect(count()).toBe("2");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.click(screen.getByLabelText("Active"));
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    expect(
      JSON.parse(call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].body!),
    ).toEqual({ card: { is_active: false } });
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        person("A", ID_A, { is_active: false }),
      ),
    );
    expect(count()).toBe("1");
  });

  it("a late PATCH answer for A leaves B's panel alone once B is selected and loaded", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("Unit", "Unit Baru");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    fireEvent.click(screen.getAllByRole("button", { name: /Wayan Dua/ })[0]);
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_B}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_B}/overview`)[0].resolve(
        person("A", ID_B, { legal_name: "Legal Bee Marker" }),
      ),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        person("A", ID_A, { unit: "Unit Baru", legal_name: "Legal Ay Marker" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(screen.getAllByText("Legal Bee Marker").length).toBeGreaterThan(0);
    expect(screen.queryByText("Legal Ay Marker")).toBeNull();
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(
      "Wayan Dua",
    );
  });

  it("does not apply a PATCH answer when the session's bearer changed while it was pending", async () => {
    await openA();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    type("Unit", "Unit Baru");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/card`, "PATCH")).toHaveLength(1),
    );
    await act(async () => {
      localStorage.setItem("auth_token", tokenFor("B"));
      window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    });
    await act(async () =>
      call(`/api/hr/people/${ID_A}/card`, "PATCH")[0].resolve(
        person("A", ID_A, { unit: "Unit Intruso" }),
      ),
    );
    expect(screen.queryByText(/Unit Intruso/)).toBeNull();
    expect(screen.queryByText(/Unit Baru/)).toBeNull();
    // the page re-reads People for the new bearer; nothing of the old identity is kept
    await waitFor(() => expect(call("/api/hr/people")).toHaveLength(2));
  });
});
