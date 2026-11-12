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

const capsState = vi.hoisted(() => ({
  value: {
    capabilities: {
      hr_admin: true,
      accounting_admin: false,
      has_employee_record: true,
      people_scope: "all",
      hr_edit: true,
    },
    loaded: true,
    failed: false,
  } as Record<string, unknown>,
}));
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
  auth: string | undefined;
  resolve: (body: unknown, status?: number) => void;
}
let pending: Pending[];

function mockTransport() {
  pending = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((url, init) => {
    const headers = (init as { headers: Record<string, string> }).headers;
    return new Promise<Response>((resolve) => {
      pending.push({
        url: String(url),
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
const call = (suffix: string) => pending.filter((p) => p.url.endsWith(suffix));

// Synthetic data only.
const ID_A = "11111111-1111-4111-8111-111111111111";
const ID_B = "22222222-2222-4222-8222-222222222222";
const ID_N = "33333333-3333-4333-8333-333333333333";

const row = (
  id: string,
  name: string,
  over: Partial<PeopleRoster["people"][number]> = {},
): PeopleRoster["people"][number] => ({
  team_member_id: id,
  employee_id: 1,
  has_record: true,
  name,
  role: "staff",
  department: "setup",
  unit: "Unit Contoh",
  hire_date: "2024-01-15",
  active: true,
  flags: {
    papers_missing: 0,
    bonuses_pending: 0,
    leave_pending: 0,
    on_leave_today: false,
  },
  ...over,
});

function roster(viewer: string, scope: "all" | "scoped" = "all"): PeopleRoster {
  return {
    viewer_team_member_id: viewer,
    scope,
    can_edit: scope === "all",
    people: [
      row(ID_A, "Made Contoh", {
        flags: {
          papers_missing: 3,
          bonuses_pending: 2,
          leave_pending: 1,
          on_leave_today: true,
        },
      }),
      row(ID_B, "Wayan Dua", { department: "tax" }),
      row(ID_N, "Ketut Tanpa", {
        employee_id: null,
        has_record: false,
        hire_date: null,
        flags: {
          papers_missing: null,
          bonuses_pending: 0,
          leave_pending: 0,
          on_leave_today: false,
        },
      }),
    ],
    pulse:
      scope === "all"
        ? {
            active_people: 3,
            people_without_record: 1,
            bonuses_pending_count: 2,
            bonuses_pending_idr: 750000,
            leave_pending_count: 1,
            payroll: { month: 11, year: 2026, status: null },
          }
        : null,
  };
}

function overview(owner: string, name: string): MyHrOverview {
  return {
    team_member_id: owner,
    card: {
      employee_id: 1,
      full_name: name,
      role: "staff",
      department: "setup",
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
      contract: { number: "C-000", file_id: null },
      docs: {},
      end_date: null,
    },
    payslips: [
      {
        id: 31,
        payroll_year: 2026,
        payroll_month: 10,
        net_salary_idr: 1111,
        base_salary_idr: 1000,
        bonus_total_idr: 0,
        allowance_total_idr: 0,
        deduction_total_idr: 0,
        thr_idr: 0,
      },
    ],
    bonuses: [],
    bonus_totals: { month_total_idr: 0, month_count: 0, year_total_idr: 0 },
    leave: { balances: [], requests: [] },
    attendance: [],
  };
}
const person = (viewer: string, id: string, name: string): PersonOverview => ({
  viewer_team_member_id: viewer,
  person: overview(id, name),
});

const settle = () => act(async () => {});
const shown = (name: string | RegExp) => screen.queryByText(name);

async function open(sub: string, scope: "all" | "scoped" = "all") {
  localStorage.setItem("auth_token", tokenFor(sub));
  render(<PeoplePage />);
  await waitFor(() => expect(call("/api/hr/people")).toHaveLength(1));
  await act(async () => call("/api/hr/people")[0].resolve(roster(sub, scope)));
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
  nav.person = null;
  capsState.value = {
    capabilities: {
      hr_admin: true,
      accounting_admin: false,
      has_employee_record: true,
      people_scope: "all",
      hr_edit: true,
    },
    loaded: true,
    failed: false,
  };
  mockTransport();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("People page (/hr)", () => {
  it("reads the roster with the bearer, groups it by department and opens the first person", async () => {
    const token = tokenFor("A");
    localStorage.setItem("auth_token", token);
    render(<PeoplePage />);
    await waitFor(() => expect(call("/api/hr/people")).toHaveLength(1));
    expect(call("/api/hr/people")[0].auth).toBe(`Bearer ${token}`);
    await act(async () => call("/api/hr/people")[0].resolve(roster("A")));

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "People",
    );
    expect(screen.getByText(/^Setup/)).toBeInTheDocument();
    expect(screen.getByText(/^Tax/)).toBeInTheDocument();
    expect(screen.queryByText(/^Office/)).toBeNull();
    for (const n of ["Made Contoh", "Wayan Dua", "Ketut Tanpa"]) {
      expect(screen.getAllByText(n).length).toBeGreaterThan(0);
    }
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    expect(screen.getByRole("button", { name: /^Pay/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Time/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Me/ })).toBeInTheDocument();
    expect(screen.getByText("Payslips")).toBeInTheDocument();
    expect(screen.getByText("Visible only to HR admins.")).toBeInTheDocument();
  });

  it("never puts a person's name or personal value in an attribute Sentry's click breadcrumbs record", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    // htmlTreeAsString (Sentry 11) serialises id, class and these attributes, never
    // text. Inside the protected region every such value must be a fixed UI string:
    // nothing derived from a person (name, counts, leave, papers, bank) may appear.
    const recorded = ["aria-label", "title", "name", "alt", "id"];
    const region = document.querySelector("[data-sentry-block]");
    expect(region).not.toBeNull();
    const values = new Set(
      Array.from(region!.querySelectorAll("*")).flatMap((el) =>
        recorded.map((a) => el.getAttribute(a)).filter((v): v is string => !!v),
      ),
    );
    const FIXED_UI_STRINGS = new Set(["Search people"]);
    expect([...values].filter((v) => !FIXED_UI_STRINGS.has(v))).toEqual([]);
    // the mobile roster buttons keep an accessible name from their text
    expect(
      screen.getAllByRole("button", { name: /Wayan Dua/ }).length,
    ).toBeGreaterThan(0);
  });

  it("makes every roster badge readable to a screen reader and on hover, as text", async () => {
    await open("A");
    for (const label of [
      "3 papers missing",
      "2 bonuses waiting",
      "On leave today",
      "1 leave request to approve",
      "No HR record yet",
    ]) {
      // once for screen readers (sr-only), once in the hover tooltip (aria-hidden)
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
      expect(document.querySelector(`[aria-label="${label}"]`)).toBeNull();
      expect(document.querySelector(`[title="${label}"]`)).toBeNull();
    }
  });

  it("shows the pulse as links and the two header buttons to a full admin", async () => {
    await open("A");
    const href = (re: RegExp) =>
      screen.getByText(re).closest("a")?.getAttribute("href");
    expect(href(/^Bonuses waiting/)).toBe("/hr/bonuses");
    expect(href(/^Leave to approve/)).toBe("/hr/leave");
    expect(href(/^Payroll November/)).toBe("/hr/payroll");
    expect(screen.getByText("Not uploaded yet")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Upload payroll/ }),
    ).toHaveAttribute("href", "/hr/payroll");
    expect(screen.getByRole("link", { name: /Add employee/ })).toHaveAttribute(
      "href",
      "/hr/employees",
    );
  });

  it("hides the pulse and both header buttons from a scoped viewer", async () => {
    capsState.value = {
      capabilities: {
        hr_admin: false,
        accounting_admin: false,
        has_employee_record: true,
        people_scope: "scoped",
        hr_edit: false,
      },
      loaded: true,
      failed: false,
    };
    await open("S", "scoped");
    expect(shown(/Active people/)).toBeNull();
    expect(shown(/Bonuses waiting/)).toBeNull();
    expect(screen.queryByRole("link", { name: /Upload payroll/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Add employee/ })).toBeNull();
  });

  it("offers no edit, upload, delete, replace, approve or adjust control anywhere", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    for (const panel of [/^Pay/, /^Time/, /^Me/]) {
      fireEvent.click(screen.getByRole("button", { name: panel }));
      const labels = [
        ...screen.getAllByRole("button"),
        ...screen.getAllByRole("link"),
      ].map((e) => `${e.textContent} ${e.getAttribute("aria-label") ?? ""}`);
      expect(
        screen.queryByRole("button", { name: /^(approve|decline)/i }),
      ).toBeNull();
      for (const l of labels) {
        expect(l).not.toMatch(
          /\b(edit|upload document|delete|replace|adjust|decline|deactivate|add bonus)\b/i,
        );
      }
    }
  });

  it("selecting another person fetches and shows them, and puts the id in the URL", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    fireEvent.click(screen.getAllByRole("button", { name: /Wayan Dua/ })[0]);
    expect(nav.replace).toHaveBeenCalledWith(`/hr?person=${ID_B}`, {
      scroll: false,
    });
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_B}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_B}/overview`)[0].resolve(
        person("A", ID_B, "Wayan Dua"),
      ),
    );
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(
      "Wayan Dua",
    );
  });

  it("ignores a late answer for the previously selected person", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    fireEvent.click(screen.getAllByRole("button", { name: /Wayan Dua/ })[0]);
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_B}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_B}/overview`)[0].resolve(
        person("A", ID_B, "Wayan Dua"),
      ),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(
      "Wayan Dua",
    );
    expect(screen.getAllByText("Payslips").length).toBe(1);
  });

  it("never shows a person whose body names a different member than the one selected", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_B, "Wayan Dua"),
      ),
    );
    expect(screen.queryByText("Payslips")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(/could not load/i);
  });

  it("clears everything when the identity switches, and shows nothing of the old one", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, "Made Contoh"),
      ),
    );
    expect(screen.getAllByText("Made Contoh").length).toBeGreaterThan(0);
    await act(async () => {
      localStorage.setItem("auth_token", tokenFor("B"));
      window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    });
    await settle();
    expect(screen.queryByText("Made Contoh")).toBeNull();
    expect(screen.queryByText("Payslips")).toBeNull();
    await waitFor(() => expect(call("/api/hr/people")).toHaveLength(2));
    expect(call("/api/hr/people")[1].auth).toBe(`Bearer ${tokenFor("B")}`);
  });

  it("does not show a roster that names another reader than the bearer it was sent with", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<PeoplePage />);
    await waitFor(() => expect(call("/api/hr/people")).toHaveLength(1));
    await act(async () => call("/api/hr/people")[0].resolve(roster("OTHER")));
    expect(screen.queryByText("Made Contoh")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(/session changed/i);
  });

  it("does not show a person overview that names another reader", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("OTHER", ID_A, "Made Contoh"),
      ),
    );
    expect(screen.queryByText("Payslips")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(/session changed/i);
  });

  it("falls back to the first person when ?person= is not on the roster", async () => {
    nav.person = "99999999-9999-4999-8999-999999999999";
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    expect(pending.some((p) => p.url.includes("99999999-9999-4999"))).toBe(
      false,
    );
  });

  it("opens the person named in ?person= when it is on the roster", async () => {
    nav.person = ID_B;
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_B}/overview`)).toHaveLength(1),
    );
    expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(0);
  });

  it("shows a friendly empty state for a person without a record, with Create record for admins only", async () => {
    nav.person = ID_N;
    await open("A");
    // the empty-state card (the roster badge says the same thing as sr-only text)
    expect(
      screen.getByText(/There is nothing to show for this person/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Create record/ })).toHaveAttribute(
      "href",
      "/hr/employees",
    );
    expect(call(`/api/hr/people/${ID_N}/overview`)).toHaveLength(0);
  });

  it("gives a scoped viewer the empty state without a Create record link", async () => {
    capsState.value = {
      capabilities: {
        hr_admin: false,
        accounting_admin: false,
        has_employee_record: true,
        people_scope: "scoped",
        hr_edit: false,
      },
      loaded: true,
      failed: false,
    };
    nav.person = ID_N;
    await open("S", "scoped");
    // the empty-state card (the roster badge says the same thing as sr-only text)
    expect(
      screen.getByText(/There is nothing to show for this person/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Create record/ })).toBeNull();
  });

  it("filters the roster with the search box", async () => {
    await open("A");
    fireEvent.change(screen.getByLabelText("Search people"), {
      target: { value: "wayan" },
    });
    // the search box lives in the desktop rail and filters it
    const rail = within(document.querySelector("aside") as HTMLElement);
    expect(rail.queryByText("Ketut Tanpa")).toBeNull();
    expect(rail.getAllByText("Wayan Dua").length).toBeGreaterThan(0);
  });

  it("redirects a plain member with a record to /hr/me and fetches nothing", async () => {
    capsState.value = {
      capabilities: {
        hr_admin: false,
        accounting_admin: false,
        has_employee_record: true,
        people_scope: null,
        hr_edit: false,
      },
      loaded: true,
      failed: false,
    };
    localStorage.setItem("auth_token", tokenFor("M"));
    render(<PeoplePage />);
    await settle();
    expect(nav.replace).toHaveBeenCalledWith("/hr/me");
    expect(pending).toHaveLength(0);
  });

  it("tells a member without a record so, and fetches nothing", async () => {
    capsState.value = {
      capabilities: {
        hr_admin: false,
        accounting_admin: false,
        has_employee_record: false,
        people_scope: null,
        hr_edit: false,
      },
      loaded: true,
      failed: false,
    };
    localStorage.setItem("auth_token", tokenFor("M"));
    render(<PeoplePage />);
    await settle();
    expect(screen.getByRole("status").textContent).toMatch(/No HR record/);
    expect(pending).toHaveLength(0);
  });
});
