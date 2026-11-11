import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { caps, getDashboard, getMyDashboard } = vi.hoisted(() => ({
  caps: vi.fn(),
  getDashboard: vi.fn(),
  getMyDashboard: vi.fn(),
}));

const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/lib/api/hr/hr", () => ({
  getCapabilities: caps,
  getDashboard,
  getMyDashboard,
}));

import HRDashboardPage from "../page";

const ADMIN = {
  employee_count: 8,
  pending_bonuses: { count: 0, total: 0 },
  pending_leave_requests: 0,
  current_period: null,
  month: 11,
  year: 2026,
};
const PERSONAL = {
  month_bonuses: { count: 0, total: 0 },
  latest_payslip: null,
  leave_balances: [],
};

// Text that only one view renders.
const TEAM = "Active Employees";
const OWN = "annual leave remaining";
const NO_RECORD = /No HR record/;
const LOAD_ERROR = /Failed to load the HR dashboard/;
const ACCESS_ERROR = /Could not load your HR access/;
const TEAM_NOTICE = /Could not load the team overview/;
const OWN_NOTICE = /Could not load your own HR card/;
const ALL = [
  TEAM,
  OWN,
  NO_RECORD,
  LOAD_ERROR,
  ACCESS_ERROR,
  TEAM_NOTICE,
  OWN_NOTICE,
];

type Outcome = "ok" | "fail";
interface Case {
  name: string;
  admin: boolean;
  record: boolean;
  adminCall?: Outcome;
  ownCall?: Outcome;
  shows: (string | RegExp)[];
}

const CASES: Case[] = [
  {
    name: "member without a record is told so",
    admin: false,
    record: false,
    shows: [NO_RECORD],
  },
  {
    name: "admin with a record sees the team overview and their own card",
    admin: true,
    record: true,
    adminCall: "ok",
    ownCall: "ok",
    shows: [TEAM, OWN],
  },
  {
    name: "admin keeps the overview when their own card fails",
    admin: true,
    record: true,
    adminCall: "ok",
    ownCall: "fail",
    shows: [TEAM, OWN_NOTICE],
  },
  {
    name: "admin keeps their own card when the overview fails",
    admin: true,
    record: true,
    adminCall: "fail",
    ownCall: "ok",
    shows: [OWN, TEAM_NOTICE],
  },
  {
    name: "admin without a record whose overview fails gets an error",
    admin: true,
    record: false,
    adminCall: "fail",
    shows: [LOAD_ERROR],
  },
  {
    name: "admin without a record sees the overview only",
    admin: true,
    record: false,
    adminCall: "ok",
    shows: [TEAM],
  },
];

function arm(mock: ReturnType<typeof vi.fn>, outcome: Outcome, data: unknown) {
  if (outcome === "ok") mock.mockResolvedValue(data);
  else mock.mockRejectedValue(new Error("boom"));
}

describe("HR dashboard states", () => {
  beforeEach(() => {
    caps.mockReset();
    getDashboard.mockReset();
    getMyDashboard.mockReset();
    replace.mockReset();
  });

  it.each(CASES)("$name", async (c) => {
    caps.mockResolvedValue({
      hr_admin: c.admin,
      accounting_admin: false,
      has_employee_record: c.record,
    });
    if (c.adminCall) arm(getDashboard, c.adminCall, ADMIN);
    if (c.ownCall) arm(getMyDashboard, c.ownCall, PERSONAL);

    render(<HRDashboardPage />);
    await waitFor(() =>
      expect(screen.getByText(c.shows[0])).toBeInTheDocument(),
    );
    for (const text of ALL) {
      if (c.shows.includes(text)) {
        expect(screen.getByText(text)).toBeInTheDocument();
      } else {
        expect(screen.queryByText(text)).not.toBeInTheDocument();
      }
    }
    // A card the caller may not see is never requested.
    expect(getDashboard).toHaveBeenCalledTimes(c.adminCall ? 1 : 0);
    expect(getMyDashboard).toHaveBeenCalledTimes(c.ownCall ? 1 : 0);
  });

  it("a member with a record is sent to /hr/me and nothing is requested here", async () => {
    caps.mockResolvedValue({
      hr_admin: false,
      accounting_admin: false,
      has_employee_record: true,
    });
    const { container } = render(<HRDashboardPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/hr/me"));
    expect(container).toBeEmptyDOMElement();
    expect(getDashboard).not.toHaveBeenCalled();
    expect(getMyDashboard).not.toHaveBeenCalled();
  });

  it("an HR admin stays on the dashboard and is not redirected", async () => {
    caps.mockResolvedValue({
      hr_admin: true,
      accounting_admin: false,
      has_employee_record: true,
    });
    getDashboard.mockResolvedValue(ADMIN);
    getMyDashboard.mockResolvedValue(PERSONAL);
    render(<HRDashboardPage />);
    await waitFor(() => expect(screen.getByText(TEAM)).toBeInTheDocument());
    expect(replace).not.toHaveBeenCalled();
  });

  it("a failed capabilities read is an access error and requests nothing", async () => {
    caps.mockRejectedValue(new Error("503"));
    render(<HRDashboardPage />);
    await waitFor(() =>
      expect(screen.getByText(ACCESS_ERROR)).toBeInTheDocument(),
    );
    expect(screen.queryByText(NO_RECORD)).not.toBeInTheDocument();
    expect(getDashboard).not.toHaveBeenCalled();
    expect(getMyDashboard).not.toHaveBeenCalled();
  });
});
