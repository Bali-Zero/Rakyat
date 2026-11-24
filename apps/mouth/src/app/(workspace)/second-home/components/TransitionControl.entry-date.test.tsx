import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { E33Stage } from "@/lib/api/secondhome/secondhome.types";
import { TransitionControl } from "./TransitionControl";

const { mockMutateAsync } = vi.hoisted(() => ({ mockMutateAsync: vi.fn() }));

vi.mock("@/hooks/useSecondHome", () => ({
  useAdvanceSecondHomeCase: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
  }),
}));
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

type Props = React.ComponentProps<typeof TransitionControl>;

function props(over: Partial<Props> = {}): Props {
  return {
    caseId: "case-1",
    currentStage: "payment",
    allowedNextStages: ["visa_issued"],
    basis: "deposit",
    guaranteeEvidenceComplete: true,
    ...over,
  };
}

const select = () => screen.getByRole("combobox") as HTMLSelectElement;
const advanceBtn = () =>
  screen.getByRole("button", { name: /advance/i }) as HTMLButtonElement;
const dateInput = (container: HTMLElement) =>
  container.querySelector('input[type="date"]') as HTMLInputElement;

// 06:00 on 2026-12-01 in Bali (WITA, UTC+8) is still 22:00 on 2026-11-30 UTC:
// the instant at which a UTC "today" lands a day early.
const BALI_MORNING = new Date("2026-11-30T22:00:00Z");
const BALI_TODAY = "2026-12-01";

describe("TransitionControl entry / ITAS date", () => {
  beforeEach(() => {
    mockMutateAsync.mockReset();
    mockMutateAsync.mockResolvedValue({});
    // Only Date is faked: userEvent keeps its real timers.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(BALI_MORNING);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each(["entry", "itas_active"] as E33Stage[])(
    "prefills %s with today's date in Bali, not the UTC date",
    async (stage) => {
      const user = userEvent.setup();
      const { container } = render(
        <TransitionControl {...props({ allowedNextStages: [stage] })} />,
      );
      await user.selectOptions(select(), stage);
      expect(dateInput(container).value).toBe(BALI_TODAY);
    },
  );

  it.each(["entry", "itas_active"] as E33Stage[])(
    "sends the Bali date explicitly for %s when the operator leaves it as offered",
    async (stage) => {
      const user = userEvent.setup();
      render(<TransitionControl {...props({ allowedNextStages: [stage] })} />);
      await user.selectOptions(select(), stage);
      await user.click(advanceBtn());
      expect(mockMutateAsync).toHaveBeenCalledTimes(1);
      expect(mockMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ to_stage: stage, occurred_on: BALI_TODAY }),
      );
    },
  );

  it("keeps an earlier date the operator chose", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl {...props({ allowedNextStages: ["entry"] })} />,
    );
    await user.selectOptions(select(), "entry");
    fireEvent.change(dateInput(container), { target: { value: "2026-11-27" } });
    expect(dateInput(container).value).toBe("2026-11-27");
    await user.click(advanceBtn());
    expect(mockMutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ to_stage: "entry", occurred_on: "2026-11-27" }),
    );
  });

  it.each(["entry", "itas_active"] as E33Stage[])(
    "requires the date for %s: a cleared field shows an alert and cannot be submitted",
    async (stage) => {
      const user = userEvent.setup();
      const { container } = render(
        <TransitionControl {...props({ allowedNextStages: [stage] })} />,
      );
      await user.selectOptions(select(), stage);
      const input = dateInput(container);
      expect(input.required).toBe(true);
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.change(input, { target: { value: "" } });
      expect(input.value).toBe("");
      expect(advanceBtn().disabled).toBe(true);
      expect(screen.getByRole("alert").textContent).toMatch(/required/i);
      fireEvent.submit(container.querySelector("form") as HTMLFormElement);
      expect(mockMutateAsync).not.toHaveBeenCalled();
    },
  );

  it("refuses a date in the future", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl {...props({ allowedNextStages: ["entry"] })} />,
    );
    await user.selectOptions(select(), "entry");
    const input = dateInput(container);
    expect(input.max).toBe(BALI_TODAY);
    fireEvent.change(input, { target: { value: "2026-12-02" } });
    expect(advanceBtn().disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toMatch(/future/i);
    fireEvent.submit(container.querySelector("form") as HTMLFormElement);
    expect(mockMutateAsync).not.toHaveBeenCalled();
  });

  it("accepts today's Bali date as the latest allowed day", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl {...props({ allowedNextStages: ["itas_active"] })} />,
    );
    await user.selectOptions(select(), "itas_active");
    fireEvent.change(dateInput(container), { target: { value: BALI_TODAY } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(advanceBtn().disabled).toBe(false);
  });

  it("no longer promises a server-side default in the label", async () => {
    const user = userEvent.setup();
    render(<TransitionControl {...props({ allowedNextStages: ["entry"] })} />);
    await user.selectOptions(select(), "entry");
    expect(screen.queryByText(/defaults to today if left blank/i)).toBeNull();
    expect(screen.getByText(/required/i)).toBeTruthy();
  });

  it("drops the date again when the operator switches to a stage that does not anchor one", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl
        {...props({ allowedNextStages: ["entry", "visa_issued"] })}
      />,
    );
    await user.selectOptions(select(), "entry");
    fireEvent.change(dateInput(container), { target: { value: "2026-11-27" } });
    await user.selectOptions(select(), "visa_issued");
    expect(dateInput(container)).toBeNull();
    await user.click(advanceBtn());
    expect(mockMutateAsync).toHaveBeenCalledTimes(1);
    expect(mockMutateAsync.mock.calls[0][0].to_stage).toBe("visa_issued");
    expect(mockMutateAsync.mock.calls[0][0].occurred_on).toBeUndefined();
  });

  it("asks for no date and sends none for a stage that does not anchor one", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl {...props({ allowedNextStages: ["visa_issued"] })} />,
    );
    await user.selectOptions(select(), "visa_issued");
    expect(dateInput(container)).toBeNull();
    expect(advanceBtn().disabled).toBe(false);
    await user.click(advanceBtn());
    expect(mockMutateAsync.mock.calls[0][0].occurred_on).toBeUndefined();
  });
});
