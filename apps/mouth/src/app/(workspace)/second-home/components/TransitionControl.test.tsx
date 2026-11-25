import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
const advanceBtn = () => screen.getByRole("button", { name: /advance/i });

describe("TransitionControl", () => {
  beforeEach(() => {
    mockMutateAsync.mockReset();
    mockMutateAsync.mockResolvedValue({});
  });

  it("never offers itap_eval even if the server list contains it", () => {
    render(
      <TransitionControl
        {...props({
          currentStage: "annual_maintenance",
          allowedNextStages: ["itap_eval", "renewal"] as E33Stage[],
        })}
      />,
    );
    const values = Array.from(select().options).map((o) => o.value);
    expect(values).toContain("renewal");
    expect(values).not.toContain("itap_eval");
  });

  it("renders the terminal message when only itap_eval is allowed", () => {
    render(
      <TransitionControl
        {...props({ allowedNextStages: ["itap_eval"] as E33Stage[] })}
      />,
    );
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByText(/terminal stage/i)).toBeTruthy();
  });

  it("disables annual_maintenance while guarantee evidence is incomplete", async () => {
    const user = userEvent.setup();
    render(
      <TransitionControl
        {...props({
          currentStage: "guarantee_proof_due",
          allowedNextStages: ["annual_maintenance"] as E33Stage[],
          guaranteeEvidenceComplete: false,
        })}
      />,
    );
    const opt = screen.getByRole("option", {
      name: /Annual Maintenance/,
    }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
    expect(opt.textContent).toContain("evidence incomplete");
    expect(screen.getByText(/requires the guarantee evidence/i)).toBeTruthy();
    // Forcing the value (the option cannot be picked by a user) keeps submit disabled.
    await user.selectOptions(select(), "annual_maintenance").catch(() => {});
    expect((advanceBtn() as HTMLButtonElement).disabled).toBe(true);
    expect(mockMutateAsync).not.toHaveBeenCalled();
  });

  it("enables annual_maintenance when guarantee evidence is complete", async () => {
    const user = userEvent.setup();
    render(
      <TransitionControl
        {...props({
          currentStage: "guarantee_proof_due",
          allowedNextStages: ["annual_maintenance"] as E33Stage[],
          guaranteeEvidenceComplete: true,
        })}
      />,
    );
    const opt = screen.getByRole("option", {
      name: /Annual Maintenance/,
    }) as HTMLOptionElement;
    expect(opt.disabled).toBe(false);
    await user.selectOptions(select(), "annual_maintenance");
    expect((advanceBtn() as HTMLButtonElement).disabled).toBe(false);
    await user.click(advanceBtn());
    expect(mockMutateAsync).toHaveBeenCalledWith({
      to_stage: "annual_maintenance",
      note: undefined,
      occurred_on: undefined,
    });
  });

  it.each(["entry", "itas_active"] as E33Stage[])(
    "sends occurred_on for %s",
    async (stage) => {
      const user = userEvent.setup();
      const { container } = render(
        <TransitionControl {...props({ allowedNextStages: [stage] })} />,
      );
      await user.selectOptions(select(), stage);
      const date = container.querySelector(
        'input[type="date"]',
      ) as HTMLInputElement;
      expect(date).not.toBeNull();
      // The field is prefilled with today; a real date field is overwritten
      // by typing, user-event appends to it, so clear it first.
      await user.clear(date);
      await user.type(date, "2026-03-04");
      await user.click(advanceBtn());
      expect(mockMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ to_stage: stage, occurred_on: "2026-03-04" }),
      );
    },
  );

  it("renders no date input and sends no occurred_on for other stages", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl {...props({ allowedNextStages: ["visa_issued"] })} />,
    );
    await user.selectOptions(select(), "visa_issued");
    expect(container.querySelector('input[type="date"]')).toBeNull();
    await user.click(advanceBtn());
    expect(mockMutateAsync).toHaveBeenCalledTimes(1);
    expect(mockMutateAsync.mock.calls[0][0].occurred_on).toBeUndefined();
  });

  it("does not send an occurred_on typed for entry after switching to a non-date stage", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TransitionControl
        {...props({ allowedNextStages: ["entry", "visa_issued"] })}
      />,
    );
    await user.selectOptions(select(), "entry");
    await user.type(
      container.querySelector('input[type="date"]') as HTMLInputElement,
      "2026-03-04",
    );
    await user.selectOptions(select(), "visa_issued");
    await user.click(advanceBtn());
    expect(mockMutateAsync).toHaveBeenCalledTimes(1);
    expect(mockMutateAsync.mock.calls[0][0].to_stage).toBe("visa_issued");
    expect(mockMutateAsync.mock.calls[0][0].occurred_on).toBeUndefined();
  });

  it("does not submit a previously chosen stage that is no longer allowed", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <TransitionControl
        {...props({ allowedNextStages: ["visa_issued", "application"] })}
      />,
    );
    await user.selectOptions(select(), "visa_issued");
    rerender(
      <TransitionControl {...props({ allowedNextStages: ["application"] })} />,
    );
    const btn = advanceBtn() as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    expect(mockMutateAsync).not.toHaveBeenCalled();
  });
});
