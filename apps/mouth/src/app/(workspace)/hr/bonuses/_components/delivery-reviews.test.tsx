import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeliveryReviews } from "./delivery-reviews";

const mocks = vi.hoisted(() => ({
  owner: { sub: "hr-admin", epoch: 1 },
  request: vi.fn(),
  post: vi.fn(),
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
vi.mock("@/lib/api", () => ({
  api: { requestWithCredential: mocks.request, post: mocks.post },
}));

const item = (month_closed: boolean) => ({
  practice_id: 47,
  service: "C1 Tourism",
  employee_id: 3,
  employee_name: "Example Consultant",
  earned_at: "2026-11-29T10:00:00Z",
  amount_idr: 125000,
  month_closed,
});

async function renderReview(month_closed: boolean) {
  mocks.request.mockResolvedValue({
    sentSub: "hr-admin",
    data: { viewer_team_member_id: "hr-admin", items: [item(month_closed)] },
  });
  render(<DeliveryReviews employees={[]} onResolved={() => {}} />);
  return screen.findByRole("button", { name: "Record pending bonus" });
}

describe("DeliveryReviews carry control", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.post.mockResolvedValue({});
  });
  it("hides the carry checkbox while the bonus month is open and never asks for the carry", async () => {
    const submit = await renderReview(false);
    expect(screen.queryByLabelText(/Move a locked award/)).toBeNull();
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.post).toHaveBeenCalledOnce());
    expect(mocks.post.mock.calls[0][1]).toMatchObject({
      amount_idr: 125000,
      use_current_period: false,
    });
  });
  it("offers the carry when the month is closed and sends it once ticked", async () => {
    const submit = await renderReview(true);
    const box = screen.getByLabelText(/Move a locked award to this month/);
    fireEvent.click(box);
    expect(box).toBeChecked();
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.post).toHaveBeenCalledOnce());
    expect(mocks.post.mock.calls[0][0]).toBe(
      "/api/hr/delivery-bonus-reviews/47",
    );
    expect(mocks.post.mock.calls[0][1]).toMatchObject({
      use_current_period: true,
    });
  });
});
