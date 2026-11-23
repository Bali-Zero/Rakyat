import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeliveryBonusDesk } from "./DeliveryBonusDesk";
const mocks = vi.hoisted(() => ({
  owner: { sub: "employee-a", epoch: 1 },
  request: vi.fn(),
}));
vi.mock("@/lib/auth/session-identity", () => ({
  useSessionIdentity: () => mocks.owner,
  isSameIdentity: (a: typeof mocks.owner, b: typeof mocks.owner) =>
    a?.sub === b?.sub && a?.epoch === b?.epoch,
  acceptResponse: (
    res: { sentSub: string },
    identity: typeof mocks.owner,
    owner?: string,
  ) => res.sentSub === identity.sub && (!owner || owner === identity.sub),
}));
vi.mock("@/lib/api", () => ({ api: { requestWithCredential: mocks.request } }));
const desk = () => ({
  team_member_id: "employee-a",
  on_payroll: true,
  month: "2026-11-01",
  milestones: 3,
  target: 5,
  totals: {
    pending: 100000,
    approved: 0,
    paid: 0,
    awaiting_rate: 1,
    payroll_locked: 0,
  },
  recent: [
    {
      practice_id: 47,
      service: "C1 Tourism",
      event_kind: "electronic_visa_issued",
      earned_at: "2026-11-29T10:00:00Z",
      awarded_at: "2026-11-30T16:00:00Z",
      status: "pending",
      amount_idr: 100000,
    },
  ],
  next_case: {
    practice_id: 47,
    service: "C1 Tourism",
    amount_idr: 100000,
    already_earned: true,
  },
});
describe("DeliveryBonusDesk", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.owner.sub = "employee-a";
  });
  it("shows the issuance, outstanding ITK, payroll month and an independently editable goal", async () => {
    let d = desk();
    mocks.request.mockImplementation(
      async (url: string, opts?: { body?: string }) => {
        if (url.endsWith("/delivery-goal"))
          d = { ...d, target: JSON.parse(opts!.body!).target };
        return {
          sentSub: "employee-a",
          data: url.endsWith("/delivery")
            ? {
                slots: [
                  { label: "Electronic Visa", document_id: 8 },
                  { label: "ITK", document_id: null },
                ],
              }
            : d,
        };
      },
    );
    render(<DeliveryBonusDesk />);
    expect(
      await screen.findByRole("link", { name: "C1 Tourism · ITK" }),
    ).toHaveAttribute("href", "/process/47");
    expect(screen.getByText("Bonus milestone already recorded")).toBeVisible();
    expect(screen.getByText(/Visa issued.*Dec 2026 payroll/)).toBeVisible();
    expect(screen.getByText("1 awaiting a rate")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Edit personal goal" }));
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("4 more to reach your goal")).toBeVisible();
  });
  it("drops a response owned by another employee without rendering payroll figures", async () => {
    mocks.request.mockResolvedValue({ sentSub: "employee-b", data: desk() });
    render(<DeliveryBonusDesk />);
    await waitFor(() => expect(mocks.request).toHaveBeenCalledOnce());
    expect(
      screen.queryByRole("heading", { name: "My delivery bonuses" }),
    ).toBeNull();
  });
});
