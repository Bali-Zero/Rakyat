/**
 * The employee form sends every field and the server stores what it receives. For a
 * person who already has a record the form must start from what is stored: an empty
 * form saved for them would replace their bank, tax and BPJS data with blanks.
 * Invented people only.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import EmployeesPage from "./page";

const hrApiMock = vi.hoisted(() => ({
  listEmployees: vi.fn(),
  upsertEmployee: vi.fn(),
}));

vi.mock("@/lib/api/hr/hr", () => hrApiMock);

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const useTeamMembersMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/hr/use-hr-capabilities", () => ({
  useHrCapabilities: () => ({
    capabilities: { hr_card_write: true },
    loaded: true,
    failed: false,
  }),
}));

vi.mock("@/hooks/useTeamMembers", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/useTeamMembers")>(
    "@/hooks/useTeamMembers",
  );
  return { ...actual, useTeamMembers: useTeamMembersMock };
});

const member = (id: string, name: string) => ({
  id,
  email: `${id}@example.test`,
  full_name: name,
  name,
  role: "Consultant",
  avatar_url: null,
  avatar: null,
});

const STORED = {
  id: 900001,
  team_member_id: "tm-1",
  full_name: "Person One",
  email: "tm-1@example.test",
  role: "Consultant",
  hire_date: "2026-01-05T00:00:00",
  base_salary_idr: 9_000_000,
  bank_name: "Bank Contoh",
  bank_account: "0000000001",
  bank_account_holder: "Person One",
  npwp: "00.000.000.0-000.000",
  ptkp_status: "K/1",
  bpjs_kesehatan_number: "0000000000001",
  bpjs_ketenagakerjaan_number: "00000000001",
  notes: null,
  is_active: true,
  created_at: "2026-01-05T00:00:00",
  updated_at: "2026-01-05T00:00:00",
};

async function openFormAndPick(teamMemberId: string) {
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: /add employee/i }),
  );
  await user.selectOptions(
    await screen.findByRole("combobox", { name: /team member/i }),
    teamMemberId,
  );
  return user;
}

const setHireDate = (container: HTMLElement, value: string) => {
  const input = container.querySelector('input[type="date"]');
  expect(input).not.toBeNull();
  fireEvent.change(input as Element, { target: { value } });
};

const setSalary = (value: string) =>
  fireEvent.change(screen.getByPlaceholderText("5000000"), {
    target: { value },
  });

async function save(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /save employee/i }));
  await waitFor(() =>
    expect(hrApiMock.upsertEmployee).toHaveBeenCalledTimes(1),
  );
  return hrApiMock.upsertEmployee.mock.calls[0][0];
}

beforeEach(() => {
  vi.clearAllMocks();
  useTeamMembersMock.mockReturnValue({
    data: [
      member("tm-1", "Person One"),
      member("tm-2", "Person Two"),
      member("tm-3", "Person Three"),
    ],
    isLoading: false,
  });
  hrApiMock.listEmployees.mockResolvedValue({ employees: [STORED] });
  hrApiMock.upsertEmployee.mockResolvedValue({
    success: true,
    employee: STORED,
  });
});

describe("EmployeesPage form and a person who already has a record", () => {
  it("saving a new salary keeps the stored bank, tax and BPJS data", async () => {
    const { container } = render(<EmployeesPage />);
    const user = await openFormAndPick("tm-1");
    setHireDate(container, "2026-01-05");
    setSalary("9500000");

    const sent = await save(user);

    expect(sent).toMatchObject({
      team_member_id: "tm-1",
      hire_date: "2026-01-05",
      base_salary_idr: 9_500_000,
      bank_name: "Bank Contoh",
      bank_account: "0000000001",
      bank_account_holder: "Person One",
      npwp: "00.000.000.0-000.000",
      ptkp_status: "K/1",
      bpjs_kesehatan_number: "0000000000001",
      bpjs_ketenagakerjaan_number: "00000000001",
    });
  });

  it("shows what is stored and says the form edits a record", async () => {
    const { container } = render(<EmployeesPage />);
    await openFormAndPick("tm-1");

    expect(await screen.findByDisplayValue("0000000001")).toBeTruthy();
    expect(screen.getByDisplayValue("Bank Contoh")).toBeTruthy();
    expect(
      (container.querySelector('input[type="date"]') as HTMLInputElement).value,
    ).toBe("2026-01-05");
    expect(
      (screen.getByPlaceholderText("5000000") as HTMLInputElement).value,
    ).toBe("9000000");
    expect(screen.getByText("Edit Employee Record")).toBeTruthy();
  });

  it("does not send a blank for a value that is stored as empty", async () => {
    render(<EmployeesPage />);
    const user = await openFormAndPick("tm-1");

    const sent = await save(user);

    expect("notes" in sent).toBe(false);
    expect(sent.bank_account).toBe("0000000001");
  });

  it("carries nothing of that person over to the next one picked", async () => {
    render(<EmployeesPage />);
    const user = await openFormAndPick("tm-1");
    await screen.findByDisplayValue("0000000001");

    await user.selectOptions(
      screen.getByRole("combobox", { name: /team member/i }),
      "tm-2",
    );

    expect(screen.queryByDisplayValue("0000000001")).toBeNull();
    expect(screen.queryByDisplayValue("Bank Contoh")).toBeNull();
    expect(
      (screen.getByPlaceholderText("5000000") as HTMLInputElement).value,
    ).toBe("");
    expect(screen.getByText("New Employee")).toBeTruthy();
  });
});

describe("EmployeesPage form and a person without a record", () => {
  it("sends what was typed, as before", async () => {
    const { container } = render(<EmployeesPage />);
    const user = await openFormAndPick("tm-2");
    setHireDate(container, "2026-02-02");
    setSalary("7000000");
    await user.type(
      screen.getByPlaceholderText("e.g. BCA, Mandiri"),
      "Bank Baru",
    );

    const sent = await save(user);

    expect(sent).toMatchObject({
      team_member_id: "tm-2",
      hire_date: "2026-02-02",
      base_salary_idr: 7_000_000,
      bank_name: "Bank Baru",
      bank_account: "",
      ptkp_status: "TK/0",
      notes: "",
    });
  });

  it("keeps what was typed when another person without a record is picked", async () => {
    render(<EmployeesPage />);
    const user = await openFormAndPick("tm-2");
    await user.type(
      screen.getByPlaceholderText("e.g. BCA, Mandiri"),
      "Bank Baru",
    );

    await user.selectOptions(
      screen.getByRole("combobox", { name: /team member/i }),
      "tm-3",
    );

    expect(screen.getByDisplayValue("Bank Baru")).toBeTruthy();
  });
});
