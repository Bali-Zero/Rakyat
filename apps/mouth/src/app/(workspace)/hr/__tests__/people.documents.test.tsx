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
  body: string | FormData | undefined;
  resolve: (body: unknown, status?: number) => void;
}
let pending: Pending[];
function mockTransport() {
  pending = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((url, init) => {
    const i = init as { method?: string; body?: string | FormData };
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

const SECRET_NAME = "Rahasia-Made-Contoh-ktp.pdf";
const PDF = () =>
  new File(
    [new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 1, 2, 3])],
    SECRET_NAME,
    {
      type: "application/pdf",
    },
  );
const docUrl = (id: string, slot: string) =>
  `/api/hr/people/${id}/documents/${slot}`;

async function openMe(sub = "A") {
  await openA(sub);
  fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
}
const rowOf = (label: RegExp) =>
  within(screen.getByRole("heading", { name: /Documents/ }).parentElement!)
    .getByText(label)
    .closest("li") as HTMLElement;
const fileInputOf = (li: HTMLElement) =>
  li.querySelector('input[type="file"]') as HTMLInputElement;
function pick(li: HTMLElement, file: File) {
  const input = fileInputOf(li);
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  fireEvent.change(input);
}
const DOCS_ON = {
  docs: { ktp: "FILEKTP12345678" },
  contract: { number: "C-1", file_id: null },
};

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

describe("People documents block (/hr, C13)", () => {
  it("lists the six papers as present or missing with the Open link", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, DOCS_ON),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    const block = screen.getByRole("heading", {
      name: /Documents/,
    }).parentElement!;
    const rows = within(block).getAllByRole("listitem");
    expect(rows).toHaveLength(6);
    expect(
      within(rowOf(/ID card \(KTP\)/)).getByText("Present"),
    ).toBeInTheDocument();
    const openLink = within(rowOf(/ID card \(KTP\)/)).getByRole("link", {
      name: /Open/,
    });
    expect(openLink.getAttribute("href")).toBe(
      "https://drive.google.com/file/d/FILEKTP12345678/view",
    );
    expect(
      within(rowOf(/Family card/)).getByText("Missing"),
    ).toBeInTheDocument();
    expect(within(rowOf(/Family card/)).queryByRole("link")).toBeNull();
    expect(
      within(rowOf(/^Contract$/)).getByText("Missing"),
    ).toBeInTheDocument();
  });

  it("shows upload and remove controls only to a card writer", async () => {
    await openMe();
    expect(
      within(rowOf(/Family card/)).getByRole("button", { name: /Upload/ }),
    ).toBeInTheDocument();
    expect(
      within(rowOf(/Family card/)).queryByRole("button", { name: /Remove/ }),
    ).toBeNull();
    expect(fileInputOf(rowOf(/Family card/)).getAttribute("accept")).toBe(
      ".pdf,image/jpeg,image/png",
    );
  });

  it.each([
    ["an HR admin without card writes", caps(false)],
    ["a scoped viewer", caps(false, "scoped")],
    ["a scoped viewer even if the flag were set", caps(true, "scoped")],
  ])("shows no document control to %s", async (_n, state) => {
    capsState.value = state;
    await open(
      "A",
      state.capabilities.people_scope === "scoped" ? "scoped" : "all",
    );
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, DOCS_ON),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(
      screen.getByRole("heading", { name: /Documents/ }),
    ).toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Upload|Replace|Remove/ }),
    ).toBeNull();
  });

  it("refuses a file over 4 MB on the client without sending anything", async () => {
    await openMe();
    const big = new File([new Uint8Array(4 * 1024 * 1024 + 1)], "x.pdf", {
      type: "application/pdf",
    });
    pick(rowOf(/Family card/), big);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /larger than 4 MB/,
    );
    expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(0);
  });

  it("uploads as multipart, then re-reads the person and shows the slot as present", async () => {
    await openMe();
    pick(rowOf(/Family card/), PDF());
    await waitFor(() =>
      expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(1),
    );
    const sent = call(docUrl(ID_A, "kk"), "POST")[0].body as FormData;
    expect(sent).toBeInstanceOf(FormData);
    expect((sent.get("file") as File).name).toBe(SECRET_NAME);
    await act(async () =>
      call(docUrl(ID_A, "kk"), "POST")[0].resolve({
        slot: "kk",
        file_id: "FILEKK1234567",
        previous: "none",
      }),
    );
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(2),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[1].resolve(
        person("A", ID_A, { docs: { kk: "FILEKK1234567" } }),
      ),
    );
    expect(
      within(rowOf(/Family card/)).getByText("Present"),
    ).toBeInTheDocument();
    expect(
      within(rowOf(/Family card/)).getByRole("button", { name: /Replace/ }),
    ).toBeInTheDocument();
  });

  it("removes only after a confirm", async () => {
    await open("A");
    await waitFor(() =>
      expect(call(`/api/hr/people/${ID_A}/overview`)).toHaveLength(1),
    );
    await act(async () =>
      call(`/api/hr/people/${ID_A}/overview`)[0].resolve(
        person("A", ID_A, DOCS_ON),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    const li = rowOf(/ID card \(KTP\)/);
    fireEvent.click(within(li).getByRole("button", { name: /^Remove$/ }));
    expect(call(docUrl(ID_A, "ktp"), "DELETE")).toHaveLength(0);
    fireEvent.click(within(li).getByRole("button", { name: /Cancel/ }));
    expect(call(docUrl(ID_A, "ktp"), "DELETE")).toHaveLength(0);
    fireEvent.click(within(li).getByRole("button", { name: /^Remove$/ }));
    fireEvent.click(within(li).getByRole("button", { name: /Confirm remove/ }));
    await waitFor(() =>
      expect(call(docUrl(ID_A, "ktp"), "DELETE")).toHaveLength(1),
    );
  });

  it("shows the server's fixed refusal and no file name", async () => {
    await openMe();
    pick(rowOf(/Family card/), PDF());
    await waitFor(() =>
      expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(1),
    );
    await act(async () =>
      call(docUrl(ID_A, "kk"), "POST")[0].resolve(
        {
          detail: [
            {
              field: "file",
              code: "unsupported_type",
              message: "Only PDF, JPEG or PNG files are accepted",
            },
          ],
        },
        422,
      ),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Only PDF, JPEG or PNG/,
    );
    expect(document.body.textContent).not.toContain(SECRET_NAME);
  });

  it("discards an upload answer when the session's bearer changed while it was pending", async () => {
    await openMe();
    pick(rowOf(/Family card/), PDF());
    await waitFor(() =>
      expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(1),
    );
    await act(async () => {
      localStorage.setItem("auth_token", tokenFor("B"));
      window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    });
    await act(async () =>
      call(docUrl(ID_A, "kk"), "POST")[0].resolve({
        slot: "kk",
        file_id: "FILEKK1234567",
        previous: "none",
      }),
    );
    // no refresh of the person was issued for the old identity
    expect(call(`/api/hr/people/${ID_A}/overview`).length).toBeLessThanOrEqual(
      2,
    );
    expect(screen.queryByText("Present")).toBeNull();
  });

  it("does not apply an answer for A to B once B is selected", async () => {
    await openMe();
    pick(rowOf(/Family card/), PDF());
    await waitFor(() =>
      expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(1),
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
      call(docUrl(ID_A, "kk"), "POST")[0].resolve({
        slot: "kk",
        file_id: "FILEKK1234567",
        previous: "none",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Me/ }));
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(
      "Wayan Dua",
    );
    expect(
      within(rowOf(/Family card/)).getByText("Missing"),
    ).toBeInTheDocument();
  });

  it("never puts the person's name or the file name in an attribute", async () => {
    await openMe();
    pick(rowOf(/Family card/), PDF());
    await waitFor(() =>
      expect(call(docUrl(ID_A, "kk"), "POST")).toHaveLength(1),
    );
    const personal = [
      "Made Contoh",
      "Made",
      "Contoh",
      SECRET_NAME,
      "Rahasia",
      FIX.legal_name,
      FIX.nik,
    ];
    const leaks: string[] = [];
    const recorded = new Set<string>();
    const region = screen
      .getByRole("heading", { name: /Documents/ })
      .closest("[data-sentry-block], .sentry-block");
    expect(region).not.toBeNull();
    for (const el of [region!, ...Array.from(region!.querySelectorAll("*"))]) {
      for (const attr of el.getAttributeNames()) {
        const v = el.getAttribute(attr) ?? "";
        if (
          ["aria-label", "title", "name", "alt", "id"].includes(attr) &&
          v &&
          v !== "Search people"
        )
          recorded.add(`${attr}=${v}`);
        for (const p of personal)
          if (v.toLowerCase().includes(p.toLowerCase()))
            leaks.push(`${el.tagName}[${attr}]`);
      }
    }
    expect(leaks).toEqual([]);
    expect([...recorded]).toEqual([]);
    // the picked file name is not left in the input either
    expect(fileInputOf(rowOf(/Family card/)).value).toBe("");
  });
});
