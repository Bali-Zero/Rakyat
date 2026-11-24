/**
 * Quoted Price gate in the process board's status menu (owner decision 2026-11-13).
 *
 * The backend refuses to move a practice INTO Sending Invoice without a Quoted Price
 * (null or 0, 422). The board says so in plain words before the request: the option is
 * disabled with the Indonesian hint, and a click that still gets through is stopped.
 * Practices with a price, and every other status, behave as before.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PratichePage from "../page";
import type { Practice } from "@/lib/api/crm/crm.types";

const mocks = vi.hoisted(() => ({
  getPractices: vi.fn(),
  updatePractice: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/process",
}));
vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({
      id: "1",
      email: "zero@balizero.com",
      name: "Test User",
      role: "admin",
    }),
    crm: {
      getPractices: mocks.getPractices,
      updatePractice: mocks.updatePractice,
      getPracticeTypesCatalog: vi
        .fn()
        .mockResolvedValue({ categories: [], total_services: 0 }),
    },
  },
}));
vi.mock("@/lib/analytics");
vi.mock("@/hooks/useTeamMembers", () => ({
  useTeamMemberOptions: () => ({ options: [] }),
}));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({
    success: mocks.toastSuccess,
    error: mocks.toastError,
    warning: vi.fn(),
    info: vi.fn(),
  }),
}));

const practice = (over: Partial<Practice>): Practice =>
  ({
    id: 1,
    client_id: 101,
    client_name: "Synthetic Alpha",
    client_lead: "zero@balizero.com",
    practice_type_id: 1,
    practice_type_code: "kitas_application",
    status: "waiting_documents",
    priority: "normal",
    payment_status: "unpaid",
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
    ...over,
  }) as Practice;

const HINT = "Isi Quoted Price dulu sebelum Sending Invoice";

async function openMenuFor(clientName: string) {
  render(<PratichePage />);
  const card = (await screen.findAllByText(clientName))[0];
  fireEvent.contextMenu(card);
  await screen.findByText("Update Status");
}

describe("process board: Quoted Price gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mocks.updatePractice.mockResolvedValue({});
  });

  it("disables Sending Invoice with the Indonesian reason when the price is missing", async () => {
    mocks.getPractices.mockResolvedValue([
      practice({ quoted_price: undefined }),
    ]);
    await openMenuFor("Synthetic Alpha");
    const option = screen.getByRole("button", { name: /^Sending Invoice/ });
    expect(option).toBeDisabled();
    expect(screen.getAllByText(HINT).length).toBeGreaterThan(0);
  });

  it("also blocks the legacy Sending Invoice aliases, which the backend normalises", async () => {
    mocks.getPractices.mockResolvedValue([
      practice({ quoted_price: undefined }),
    ]);
    await openMenuFor("Synthetic Alpha");
    for (const name of [
      /Quotation Sent/,
      /Payment Pending/,
      /Waiting Payment/,
    ]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });

  it("treats a zero price like a missing one", async () => {
    mocks.getPractices.mockResolvedValue([practice({ quoted_price: 0 })]);
    await openMenuFor("Synthetic Alpha");
    expect(
      screen.getByRole("button", { name: /^Sending Invoice/ }),
    ).toBeDisabled();
  });

  it("keeps Sending Invoice enabled, without hint, when the practice has a price", async () => {
    mocks.getPractices.mockResolvedValue([
      practice({ quoted_price: 1_500_000 }),
    ]);
    await openMenuFor("Synthetic Alpha");
    const option = screen.getByRole("button", { name: /^Sending Invoice/ });
    expect(option).toBeEnabled();
    expect(screen.queryByText(HINT)).toBeNull();
    await userEvent.setup().click(option);
    await waitFor(() =>
      expect(mocks.updatePractice).toHaveBeenCalledWith(1, {
        status: "sending_invoice",
      }),
    );
  });

  it("never blocks the other statuses of a practice without a price", async () => {
    mocks.getPractices.mockResolvedValue([
      practice({ quoted_price: undefined }),
    ]);
    await openMenuFor("Synthetic Alpha");
    const inquiry = screen.getByRole("button", { name: /^Inquiry$/ });
    expect(inquiry).toBeEnabled();
    expect(screen.queryByRole("button", { name: /^On Process$/ })).toBeNull();
    await userEvent.setup().click(inquiry);
    await waitFor(() =>
      expect(mocks.updatePractice).toHaveBeenCalledWith(1, {
        status: "inquiry",
      }),
    );
  });

  it("never blocks a practice that is already in Sending Invoice", async () => {
    mocks.getPractices.mockResolvedValue([
      practice({ status: "sending_invoice", quoted_price: undefined }),
    ]);
    await openMenuFor("Synthetic Alpha");
    // The current status is disabled because it is current, with no price hint on it.
    expect(screen.queryByText(HINT)).toBeNull();
    expect(
      screen.getByRole("button", { name: /Waiting Documents/ }),
    ).toBeEnabled();
  });

  it("maps the backend refusal to the Indonesian text if it still comes back", async () => {
    mocks.getPractices.mockResolvedValue([practice({ quoted_price: 1000 })]);
    mocks.updatePractice.mockRejectedValueOnce(
      Object.assign(
        new Error("Quoted Price is required before Sending Invoice"),
        {
          detail: "Quoted Price is required before Sending Invoice",
        },
      ),
    );
    await openMenuFor("Synthetic Alpha");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /^Sending Invoice/ }));
    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith("Error", HINT),
    );
  });

  it("falls back to the generic message for any other failure", async () => {
    mocks.getPractices.mockResolvedValue([practice({ quoted_price: 1000 })]);
    mocks.updatePractice.mockRejectedValueOnce(new Error("boom"));
    await openMenuFor("Synthetic Alpha");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /^Inquiry$/ }));
    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        "Error",
        "Failed to update process status",
      ),
    );
  });
});
