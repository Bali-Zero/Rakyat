import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CaseSummary } from "@/lib/api/secondhome/secondhome.types";
import { CaseRow } from "./CaseListSection";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const PAST = "2020-01-01";

function makeCase(over: Partial<CaseSummary>): CaseSummary {
  return {
    case_id: "E33-2026-abc123",
    client_id: 1,
    client_name: "Alice Example",
    basis: "deposit",
    stage: "guarantee_proof_due",
    guarantee_proof_deadline: PAST,
    stayguard_eligible: false,
    guarantee_gate_open: true,
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("CaseRow guarantee countdown", () => {
  it("shows the overdue countdown while the gate is open", () => {
    render(<CaseRow item={makeCase({})} />);
    expect(screen.getByText(/d overdue/)).toBeTruthy();
  });

  it("shows no countdown for annual_maintenance with a stale past deadline", () => {
    render(
      <CaseRow
        item={makeCase({
          stage: "annual_maintenance",
          guarantee_gate_open: false,
          stayguard_eligible: true,
        })}
      />,
    );
    expect(screen.queryByText(/overdue/)).toBeNull();
    expect(screen.queryByText(/to deadline/)).toBeNull();
  });

  it("shows the StayGuard badge and never an overdue countdown with it", () => {
    render(
      <CaseRow
        item={makeCase({
          stage: "itas_active",
          stayguard_eligible: true,
          guarantee_gate_open: false,
        })}
      />,
    );
    expect(screen.getByText("StayGuard")).toBeTruthy();
    expect(screen.queryByText(/overdue/)).toBeNull();
  });

  it("shows no countdown for a terminal case", () => {
    render(
      <CaseRow item={makeCase({ stage: "epo", guarantee_gate_open: false })} />,
    );
    expect(screen.queryByText(/overdue/)).toBeNull();
  });
});
