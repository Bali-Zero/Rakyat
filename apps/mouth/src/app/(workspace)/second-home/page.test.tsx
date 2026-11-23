/**
 * E33 console stage filter: every stage the dropdown offers must be listable.
 * "Active only" excludes epo/status_change server-side, so it must not be sent
 * with them. The hooks are mocked with a fake backend that mimics that rule. Data is invented.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import SecondHomeConsolePage from "./page";
import type {
  CaseSummary,
  ListCasesParams,
} from "@/lib/api/secondhome/secondhome.types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const calls = vi.hoisted(() => ({
  list: [] as unknown[],
  summary: undefined as unknown,
}));

function row(case_id: string, client_name: string, stage: string) {
  return {
    case_id,
    client_id: 1,
    client_name,
    basis: "deposit",
    stage,
    stayguard_eligible: false,
    created_at: "2026-01-01",
  } as unknown as CaseSummary;
}
const ROWS = [
  row("c1", "Alpha Test", "fit_memo"),
  row("c2", "Beta Test", "epo"),
  row("c3", "Gamma Test", "status_change"),
];
const TERMINAL = ["epo", "status_change"];

vi.mock("@/hooks/useSecondHome", () => ({
  useSecondHomeSummary: () => ({
    data: calls.summary,
    isLoading: false,
    isError: false,
  }),
  useSecondHomeCases: (params: ListCasesParams) => {
    calls.list.push(params);
    const cases = ROWS.filter(
      (r) =>
        (!params.stage || r.stage === params.stage) &&
        (!params.active_only || !TERMINAL.includes(r.stage)),
    );
    return { data: { cases, total: cases.length }, isLoading: false };
  },
}));

const last = () => calls.list[calls.list.length - 1] as ListCasesParams;

function openFilters() {
  render(<SecondHomeConsolePage />);
  fireEvent.click(screen.getByRole("button", { name: /filters/i }));
}
const pickStage = (v: string) =>
  fireEvent.change(screen.getByLabelText("Stage"), { target: { value: v } });

describe("second-home console stage filter", () => {
  beforeEach(() => {
    calls.list.length = 0;
    calls.summary = undefined;
  });

  describe("terminal chip", () => {
    const summary = (by_stage: Record<string, number>) => ({
      by_stage,
      active_total: 1,
      scan_switch: "disarmed",
    });

    it("does not count itap_eval", () => {
      calls.summary = summary({ epo: 2, status_change: 1, itap_eval: 3 });
      render(<SecondHomeConsolePage />);
      expect(screen.getByText(/^3 terminal/)).toBeTruthy();
    });

    it("states that it covers all time", () => {
      calls.summary = summary({ epo: 2 });
      render(<SecondHomeConsolePage />);
      expect(screen.getByText("2 terminal (all time)")).toBeTruthy();
    });
  });

  it("default view stays active-only", () => {
    openFilters();
    expect(last().active_only).toBe(true);
    expect(screen.queryByText("Beta Test")).toBeNull();
    expect(screen.getByText("Alpha Test")).toBeTruthy();
  });

  it.each([
    ["epo", "Beta Test"],
    ["status_change", "Gamma Test"],
  ])("choosing %s lists its rows without active_only", (stage, name) => {
    openFilters();
    pickStage(stage);
    expect(last().stage).toBe(stage);
    expect(last().active_only).toBeUndefined();
    expect(screen.getByText(name)).toBeTruthy();
    expect(screen.getByTestId("active-only-not-applied")).toBeTruthy();
    expect((screen.getByLabelText("Status") as HTMLSelectElement).value).toBe(
      "all",
    );
  });

  it("an active stage keeps active_only and returns to it after a terminal one", () => {
    openFilters();
    pickStage("fit_memo");
    expect(last().active_only).toBe(true);
    expect(screen.queryByTestId("active-only-not-applied")).toBeNull();
    pickStage("epo");
    expect(last().active_only).toBeUndefined();
    pickStage("fit_memo");
    expect(last().active_only).toBe(true);
  });
});
