import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { GuaranteeInfo } from "@/lib/api/secondhome/secondhome.types";
import { GuaranteePanel } from "./GuaranteePanel";

const stale: GuaranteeInfo = {
  deadline: "2020-04-01",
  days_remaining: -234,
  alert_schedule: [{ date: "2020-02-01", severity: "warning" }],
};

describe("GuaranteePanel", () => {
  it("shows days overdue and the alert schedule while the gate is open", () => {
    render(
      <GuaranteePanel
        guarantee={stale}
        hasEntryOrItasDate
        gateOpen
        evidenceComplete={false}
      />,
    );
    expect(screen.getByText("234d overdue")).toBeTruthy();
    expect(screen.getByText("Alert schedule")).toBeTruthy();
  });

  it("shows a neutral completed line, no countdown, when the evidence is complete", () => {
    render(
      <GuaranteePanel
        guarantee={stale}
        hasEntryOrItasDate
        gateOpen={false}
        evidenceComplete
      />,
    );
    expect(screen.queryByText(/overdue/)).toBeNull();
    expect(screen.queryByText("Alert schedule")).toBeNull();
    expect(screen.getByText(/Guarantee proof complete/)).toBeTruthy();
  });

  it("shows no countdown for a closed gate without complete evidence (terminal)", () => {
    render(
      <GuaranteePanel
        guarantee={stale}
        hasEntryOrItasDate
        gateOpen={false}
        evidenceComplete={false}
      />,
    );
    expect(screen.queryByText(/overdue/)).toBeNull();
    expect(screen.getByText(/Day-90 gate closed/)).toBeTruthy();
  });
});
