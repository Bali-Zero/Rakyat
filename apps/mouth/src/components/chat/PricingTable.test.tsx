import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PricingTable } from "./PricingTable";
import type { PricingResponse } from "@/types/pricing";

describe("PricingTable compliance_retainers", () => {
  it("renders Compliance Retainers category items", () => {
    const data = {
      official_notice: "OFFICIAL PRICES",
      compliance_retainers: {
        "Monthly Compliance Retainer": {
          price: "5.000.000 IDR",
          timeline: "Monthly",
        },
      },
    } as unknown as PricingResponse;

    render(<PricingTable data={data} />);

    expect(screen.getByText("Compliance Retainers")).toBeInTheDocument();
    expect(screen.getByText("Monthly Compliance Retainer")).toBeInTheDocument();
    expect(screen.getByText("5.000.000 IDR")).toBeInTheDocument();
  });
});
