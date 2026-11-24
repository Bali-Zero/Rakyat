import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProcessDeliveryPeek } from "./ProcessDeliveryPeek";

const { request, current } = vi.hoisted(() => ({
  request: vi.fn(),
  current: vi.fn(),
}));
const identity = { sub: "operator-a", epoch: 1 };
vi.mock("@/lib/api", () => ({ api: { requestWithCredential: request } }));
vi.mock("@/lib/auth/session-identity", () => ({
  readSessionIdentity: current,
  isSameIdentity: (a: typeof identity, b: typeof identity) =>
    a?.sub === b.sub && a.epoch === b.epoch,
  acceptResponse: (r: { sentSub: string }, b: typeof identity) =>
    r.sentSub === b.sub && current()?.sub === b.sub,
}));
describe("ProcessDeliveryPeek", () => {
  beforeEach(() => {
    request.mockReset();
    current.mockReset().mockReturnValue(identity);
  });
  it("loads effective requirements only when opened", async () => {
    request.mockResolvedValue({
      sentSub: identity.sub,
      data: {
        needs_titles: false,
        visa_followup: {
          slot_key: "b",
          due_on: "2026-12-02",
          fulfilled_at: null,
        },
        slots: [
          {
            key: "a",
            label: "Electronic Visa",
            document_id: 7,
            file_name: "visa.pdf",
          },
          { key: "b", label: "ITK", document_id: null },
          {
            key: "c",
            label: "Amended filing",
            document_id: null,
            condition: "If applicable",
            waiver_reason: "No amendment",
          },
          {
            key: "d",
            label: "Additional certificate",
            document_id: null,
            condition: "If applicable",
          },
        ],
      },
    });
    render(<ProcessDeliveryPeek practiceId={7} identity={identity} />);
    expect(request).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("Delivery documents"));
    expect(await screen.findByText("ITK")).toBeVisible();
    for (const value of ["Received", "Missing", "Not applicable", "To confirm"])
      expect(screen.getByText(value)).toBeVisible();
    expect(screen.getByText("visa.pdf")).toBeVisible();
    expect(screen.getByText("2 Dec 2026")).toBeVisible();
    expect(request).toHaveBeenCalledWith("/api/crm/practices/7/delivery");
    expect(screen.getByRole("link", { name: "Open delivery" })).toHaveAttribute(
      "href",
      "/process/7#final-delivery",
    );
  });
  it("omits document names when the response belongs to another session", async () => {
    request.mockResolvedValue({
      sentSub: "operator-b",
      data: {
        needs_titles: false,
        slots: [{ key: "x", label: "Private report" }],
      },
    });
    render(<ProcessDeliveryPeek practiceId={19} identity={identity} />);
    await userEvent.click(screen.getByText("Delivery documents"));
    await screen.findByRole("alert");
    expect(screen.queryByText("Private report")).toBeNull();
    request.mockResolvedValueOnce({
      sentSub: identity.sub,
      data: { needs_titles: true, slots: [] },
    });
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() =>
      expect(screen.getByText(/names still need to be agreed/i)).toBeVisible(),
    );
  });
});
