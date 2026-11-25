import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DependentsSection } from "./DependentsSection";

describe("DependentsSection", () => {
  it("lists each dependent with its name, code, stage and a link to its case", () => {
    render(
      <DependentsSection
        dependents={[
          {
            case_id: "E33-2026-aaaaaa",
            code: "E31B",
            client_id: 11,
            client_name: "Zz Spouse",
            stage: "fit_memo",
            relationship: null,
          },
          {
            case_id: "E33-2026-bbbbbb",
            code: "E31E",
            client_id: 12,
            relationship: null,
          },
        ]}
      />,
    );
    expect(screen.getByText("Zz Spouse")).toBeTruthy();
    expect(screen.getByText("E31B")).toBeTruthy();
    expect(screen.getByText("(fit_memo)")).toBeTruthy();
    expect(screen.getByText("Client #12")).toBeTruthy();
    expect(
      screen.getByText("Zz Spouse").closest("a")?.getAttribute("href"),
    ).toBe("/second-home/E33-2026-aaaaaa");
    expect(screen.queryByText("No dependents linked to this case.")).toBeNull();
  });

  it("keeps the empty state for a principal with no dependents", () => {
    render(<DependentsSection dependents={[]} />);
    expect(screen.getByText("No dependents linked to this case.")).toBeTruthy();
  });
});
