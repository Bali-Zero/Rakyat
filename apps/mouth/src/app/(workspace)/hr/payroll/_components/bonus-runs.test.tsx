import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/error-handler";
import type { BonusRun } from "@/types/hr";
import { BonusRuns } from "./bonus-runs";

const hrApi = vi.hoisted(() => ({
  approveBonusRun: vi.fn(),
  markBonusRunPaid: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("@/lib/api/hr/hr", () => hrApi);
vi.mock("sonner", () => ({ toast }));

const run = (over: Partial<BonusRun> = {}): BonusRun => ({
  bonus_month: "2026-11-01",
  status: "open",
  ended: true,
  can_approve: true,
  approved_at: null,
  approved_by: null,
  paid_at: null,
  approved_total_idr: 300000,
  approved_count: 3,
  pending_count: 2,
  pending_total_idr: 150000,
  held_count: 1,
  held_total_idr: 50000,
  expected_payday: null,
  payday_salary_month: "2026-12-01",
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(window.confirm).mockReturnValue(true);
  hrApi.approveBonusRun.mockResolvedValue({ success: true });
  hrApi.markBonusRunPaid.mockResolvedValue({ success: true });
});

describe("bonus runs on the payroll page", () => {
  it("offers Approve run and Mark paid to a payroll writer only", () => {
    const runs = [
      run({ bonus_month: "2026-12-01", status: "approved" }),
      run(),
    ];
    const { rerender } = render(
      <BonusRuns runs={runs} canWrite onChanged={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /Approve run/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Mark paid/ })).toBeVisible();
    rerender(<BonusRuns runs={runs} canWrite={false} onChanged={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Approve run/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Mark paid/ })).toBeNull();
  });

  it("keeps Approve run disabled for an ended month while an earlier one is open, and says why", () => {
    render(
      <BonusRuns
        runs={[run({ can_approve: false })]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Approve run/ })).toBeDisabled();
    expect(screen.getByText("Close the earlier month first.")).toBeVisible();
  });

  it("lists the newest month first", () => {
    render(
      <BonusRuns
        runs={[run(), run({ bonus_month: "2026-12-01", ended: false })]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    expect(
      screen
        .getAllByText(/^(November|December) 2026$/)
        .map((e) => e.textContent),
    ).toEqual(["December 2026", "November 2026"]);
  });

  it("disables Approve run before the month ends and says why", () => {
    render(
      <BonusRuns
        runs={[run({ ended: false, can_approve: false })]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Approve run/ })).toBeDisabled();
    expect(screen.getByText("Opens when the month ends.")).toBeVisible();
    expect(screen.getByText("Open")).toBeVisible();
  });

  it("names no salary for the payday of a month still running, and calls its held rows held", () => {
    render(
      <BonusRuns
        runs={[
          run({
            bonus_month: "2026-12-01",
            ended: false,
            can_approve: false,
            payday_salary_month: "2026-12-01",
          }),
        ]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("Payday known once the month ends")).toBeVisible();
    expect(document.body).not.toHaveTextContent("Dec 2026 salary");
    expect(document.body).toHaveTextContent("1 held");
    expect(document.body).toHaveTextContent("carried to a later run");
  });

  it("confirms with the gross amount and the pending count, then posts the figures shown", async () => {
    const onChanged = vi.fn();
    render(<BonusRuns runs={[run()]} canWrite onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: /Approve run/ }));
    const asked = vi.mocked(window.confirm).mock.calls[0][0] as string;
    expect(asked).toContain("Close November 2026 and pay Rp");
    expect(asked).toContain("300.000 gross");
    expect(asked).toContain("2 pending bonuses");
    expect(asked).toContain("This cannot be undone.");
    await waitFor(() =>
      expect(hrApi.approveBonusRun).toHaveBeenCalledWith("2026-11", {
        approved_total_idr: 300000,
        pending_count: 2,
      }),
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it("posts nothing when the confirmation is declined", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<BonusRuns runs={[run()]} canWrite onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /Approve run/ }));
    expect(hrApi.approveBonusRun).not.toHaveBeenCalled();
  });

  it("toasts a stale month and reloads on a 409 bonus_run_changed", async () => {
    hrApi.approveBonusRun.mockRejectedValue(
      new ApiError("changed", 409, { detail: "x", code: "bonus_run_changed" }),
    );
    const onChanged = vi.fn();
    render(<BonusRuns runs={[run()]} canWrite onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: /Approve run/ }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "The month changed: review it again",
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
  });

  it.each([
    "bonus_run_not_due",
    "bonus_run_out_of_order",
    "bonus_run_closed",
    "bonus_run_not_approved",
  ])(
    "toasts the refusal %s, reloads, and never says approved",
    async (code) => {
      hrApi.approveBonusRun.mockRejectedValue(
        new ApiError("Refused by the server", 409, {
          detail: "Refused by the server",
          code,
        }),
      );
      const onChanged = vi.fn();
      render(<BonusRuns runs={[run()]} canWrite onChanged={onChanged} />);
      await userEvent.click(
        screen.getByRole("button", { name: /Approve run/ }),
      );
      await waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith("Refused by the server"),
      );
      expect(toast.success).not.toHaveBeenCalled();
      expect(onChanged).toHaveBeenCalled();
    },
  );

  it("disables every button while a request is in flight, so a second click posts nothing", async () => {
    let release: (value: unknown) => void = () => {};
    hrApi.approveBonusRun.mockReturnValue(
      new Promise((resolve) => (release = resolve)),
    );
    const onChanged = vi.fn();
    render(
      <BonusRuns
        runs={[run({ bonus_month: "2026-12-01", status: "approved" }), run()]}
        canWrite
        onChanged={onChanged}
      />,
    );
    const approveButton = screen.getByRole("button", { name: /Approve run/ });
    await userEvent.click(approveButton);
    await waitFor(() => expect(approveButton).toBeDisabled());
    expect(screen.getByRole("button", { name: /Mark paid/ })).toBeDisabled();
    await userEvent.click(approveButton);
    await userEvent.click(screen.getByRole("button", { name: /Mark paid/ }));
    expect(hrApi.approveBonusRun).toHaveBeenCalledTimes(1);
    expect(hrApi.markBonusRunPaid).not.toHaveBeenCalled();
    release({ success: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(approveButton).toBeEnabled();
  });

  it("toasts a mark-paid refusal, reloads, and never says paid", async () => {
    hrApi.markBonusRunPaid.mockRejectedValue(
      new ApiError("That bonus run is not waiting for payment", 409, {
        detail: "That bonus run is not waiting for payment",
        code: "bonus_run_not_approved",
      }),
    );
    const onChanged = vi.fn();
    render(
      <BonusRuns
        runs={[run({ status: "approved" })]}
        canWrite
        onChanged={onChanged}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Mark paid/ }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "That bonus run is not waiting for payment",
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
  });

  it("dates a payment by the Bali day: 00:30 WITA on 26 Dec is the 26th", () => {
    render(
      <BonusRuns
        runs={[run({ status: "paid", paid_at: "2026-12-25T16:30:00Z" })]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("Paid 26 Dec 2026")).toBeVisible();
  });

  it("marks an approved run paid after confirming the amount", async () => {
    render(
      <BonusRuns
        runs={[run({ status: "approved" })]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Mark paid/ }));
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toContain("300.000");
    await waitFor(() =>
      expect(hrApi.markBonusRunPaid).toHaveBeenCalledWith("2026-11"),
    );
  });

  it("shows gross, pending, held and the payday, and never a tax, PPh21 or net text", () => {
    render(
      <BonusRuns
        runs={[
          run({ status: "approved" }),
          run({
            bonus_month: "2026-12-01",
            status: "paid",
            paid_at: "2026-12-26T05:00:00Z",
          }),
        ]}
        canWrite
        onChanged={vi.fn()}
      />,
    );
    const nov = screen.getByText("November 2026").closest("div")!
      .parentElement as HTMLElement;
    expect(within(nov).getByText("Awaiting payment")).toBeVisible();
    expect(nov).toHaveTextContent("Rp 300.000 gross");
    expect(nov).toHaveTextContent("2 pending");
    expect(nov).toHaveTextContent("not paid by this run");
    expect(nov).toHaveTextContent("1 carried");
    expect(nov).toHaveTextContent("paid by a later run");
    expect(nov).toHaveTextContent("7 days after the Dec 2026 salary is paid");
    expect(screen.getByText("Paid 26 Dec 2026")).toBeVisible();
    expect(document.body).not.toHaveTextContent(/PPh|tax|net/i);
  });
});
