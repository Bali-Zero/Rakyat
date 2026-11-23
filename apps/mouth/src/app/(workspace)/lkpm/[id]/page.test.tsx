import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/error-handler";

import LKPMReadyPackPage from "./page";

const mocks = vi.hoisted(() => ({
  getReadyPack: vi.fn(),
  markSubmitted: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useParams: () => ({ id: "42" }) }));
vi.mock("@/lib/api/workspace/lkpm.api", () => ({
  lkpmApi: {
    getReadyPack: mocks.getReadyPack,
    markSubmitted: mocks.markSubmitted,
  },
}));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ error: mocks.toastError, success: mocks.toastSuccess }),
}));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));

const pack = (status: string) => ({
  draft_id: 42,
  company_name: "PT Synthetic Test",
  quarter: "Q1",
  year: 2026,
  realized: {},
  cumulative: {},
  employment: {},
  validation_summary: null,
  html_content: "<p>Ready</p>",
  status,
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getReadyPack.mockResolvedValue(pack("approved"));
  mocks.markSubmitted.mockResolvedValue({ success: true });
});

describe("LKPM ready pack submission", () => {
  it("does not offer marking a report that is not approved", async () => {
    mocks.getReadyPack.mockResolvedValue(pack("client_review"));
    render(<LKPMReadyPackPage />);

    expect(
      await screen.findByText("Status: client_review"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Mark Submitted" }),
    ).not.toBeInTheDocument();
  });

  it("asks for confirmation before submitting an approved report", async () => {
    render(<LKPMReadyPackPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Submitted" }),
    );

    expect(
      screen.getByText(
        "Mark this report as submitted to OSS? Do this only after you have filed it in OSS.",
      ),
    ).toBeInTheDocument();
    expect(mocks.markSubmitted).not.toHaveBeenCalled();
  });

  it("does not submit when confirmation is cancelled", async () => {
    render(<LKPMReadyPackPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Submitted" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(mocks.markSubmitted).not.toHaveBeenCalled();
  });

  it("submits exactly once when confirmation is accepted", async () => {
    render(<LKPMReadyPackPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Submitted" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(mocks.markSubmitted).toHaveBeenCalledTimes(1));
    expect(mocks.markSubmitted).toHaveBeenCalledWith(42);
  });

  it("shows the server conflict message", async () => {
    mocks.markSubmitted.mockRejectedValue(
      new ApiError("Conflict", 409, {
        detail: "LKPM report is already submitted",
      }),
    );
    render(<LKPMReadyPackPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Submitted" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        "Failed to mark submitted",
        "LKPM report is already submitted",
      ),
    );
  });

  it("keeps the generic retry text", async () => {
    mocks.markSubmitted.mockRejectedValue(
      new ApiError("Server error", 500, { detail: "internal detail" }),
    );
    render(<LKPMReadyPackPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Submitted" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        "Failed to mark submitted",
        "Please try again",
      ),
    );
  });
});
