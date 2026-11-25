import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LeaveRequestPage from "./page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api/hr/hr", () => ({
  getLeaveTypes: vi.fn().mockResolvedValue({
    leave_types: [{ id: 1, name: "Annual Leave", code: "annual" }],
  }),
  requestLeave: vi.fn(),
}));

function pick(dateButtonIndex: number, day: string) {
  fireEvent.click(screen.getAllByText("Select date")[dateButtonIndex]);
  fireEvent.click(screen.getByRole("button", { name: day }));
}

describe("LeaveRequestPage day count", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 10, 23, 9, 0, 0));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not submit a weekend-only range and says why", async () => {
    render(<LeaveRequestPage />);
    const submit = screen.getByRole("button", { name: /Submit Request/ });
    await waitFor(() => expect(submit).not.toBeDisabled());

    pick(0, "28");
    pick(0, "29");

    expect(
      screen.getByText("These dates contain no working day."),
    ).toBeInTheDocument();
    expect(submit).toBeDisabled();
  });

  it("counts the working days of a week and lets it be submitted", async () => {
    render(<LeaveRequestPage />);
    const submit = screen.getByRole("button", { name: /Submit Request/ });
    await waitFor(() => expect(submit).not.toBeDisabled());

    pick(0, "23");
    pick(0, "29");

    expect(screen.getByText("5")).toBeInTheDocument();
    expect(
      screen.queryByText("These dates contain no working day."),
    ).not.toBeInTheDocument();
    expect(submit).not.toBeDisabled();
  });
});
