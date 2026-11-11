import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const getProfile = vi.fn();
const listLeaveRequests = vi.fn();

vi.mock("@/lib/api", () => ({ api: { getProfile: () => getProfile() } }));
vi.mock("@/lib/api/hr/hr", () => ({
  getCapabilities: () =>
    Promise.resolve({
      hr_admin: false,
      accounting_admin: false,
      has_employee_record: true,
    }),
  listLeaveRequests: () => listLeaveRequests(),
  getLeaveBalance: () => Promise.resolve({ balances: [] }),
  getTeamLeaveSummary: () => Promise.reject(new Error("403")),
  approveLeave: vi.fn(),
  rejectLeave: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const base = {
  leave_type_id: 1,
  leave_type_name: "Annual",
  start_date: "2026-12-15",
  end_date: "2026-12-16",
  total_days: 2,
  reason: null,
  requested_at: "2026-11-09T00:00:00Z",
  reviewed_by: null,
  reviewed_at: null,
  rejection_reason: null,
};

describe("leave page for a non-admin approver", () => {
  beforeEach(() => {
    vi.resetModules();
    getProfile.mockResolvedValue({ email: "approver@example.test", role: "member" });
  });

  it("shows approve/reject only on the row the server marks reviewable, not on the approver's own", async () => {
    listLeaveRequests.mockResolvedValue({
      requests: [
        { ...base, id: 1, employee_id: 10, employee_name: "Report", employee_email: "r@example.test", status: "pending", is_mine: false, can_review: true },
        { ...base, id: 2, employee_id: 11, employee_name: "Me", employee_email: "approver@example.test", status: "pending", is_mine: true, can_review: false },
      ],
    });
    const { default: LeavePage } = await import("./page");
    render(<LeavePage />);
    await waitFor(() => expect(screen.getByText("Pending Approval")).toBeInTheDocument());
    expect(screen.getAllByTitle("Approve")).toHaveLength(1);
    expect(screen.getAllByTitle("Reject")).toHaveLength(1);
  });

  it("shows no approval controls to a plain member", async () => {
    listLeaveRequests.mockResolvedValue({
      requests: [
        { ...base, id: 2, employee_id: 11, employee_name: "Me", employee_email: "approver@example.test", status: "pending", is_mine: true, can_review: false },
      ],
    });
    const { default: LeavePage } = await import("./page");
    render(<LeavePage />);
    await waitFor(() => expect(screen.getByText("My Requests")).toBeInTheDocument());
    expect(screen.queryByText("Pending Approval")).toBeNull();
    expect(screen.queryAllByTitle("Approve")).toHaveLength(0);
  });
});
