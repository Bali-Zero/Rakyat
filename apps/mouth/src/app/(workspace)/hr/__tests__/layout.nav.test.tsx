import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getCapabilitiesMock, getProfileMock } = vi.hoisted(() => ({
  getCapabilitiesMock: vi.fn(),
  getProfileMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/hr" }));
vi.mock("@/lib/api", () => ({ api: { getProfile: getProfileMock } }));
vi.mock("@/lib/api/hr/hr", () => ({ getCapabilities: getCapabilitiesMock }));

import HRLayout from "../layout";

const MEMBER_LINKS = ["My HR", "Bonuses", "Payroll", "Leave"];

function hrefs(): string[] {
  return screen
    .getAllByRole("link")
    .map((a) => a.getAttribute("href") ?? "")
    .filter((h, i, all) => all.indexOf(h) === i);
}

describe("HR layout navigation", () => {
  beforeEach(() => {
    getCapabilitiesMock.mockReset();
    getProfileMock.mockReset();
    getProfileMock.mockResolvedValue({ email: "member@balizero.com" });
  });

  it("shows a member only their own HR pages", async () => {
    getCapabilitiesMock.mockResolvedValue({
      hr_admin: false,
      accounting_admin: false,
      has_employee_record: true,
    });
    render(<HRLayout>x</HRLayout>);
    await waitFor(() => expect(getCapabilitiesMock).toHaveBeenCalled());
    expect(hrefs()).toEqual(["/hr", "/hr/bonuses", "/hr/payroll", "/hr/leave"]);
    for (const label of MEMBER_LINKS) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("adds the admin pages for an HR admin, without accounting", async () => {
    getCapabilitiesMock.mockResolvedValue({
      hr_admin: true,
      accounting_admin: false,
      has_employee_record: true,
    });
    render(<HRLayout>x</HRLayout>);
    await waitFor(() => expect(hrefs()).toContain("/hr/employees"));
    expect(hrefs()).toContain("/hr/settings");
    expect(hrefs()).not.toContain("/accounting");
    expect(hrefs()).not.toContain("/hr/owner-cashout");
  });

  it("links accounting for an accounting admin", async () => {
    getCapabilitiesMock.mockResolvedValue({
      hr_admin: true,
      accounting_admin: true,
      has_employee_record: true,
    });
    render(<HRLayout>x</HRLayout>);
    await waitFor(() => expect(hrefs()).toContain("/accounting"));
  });
});
