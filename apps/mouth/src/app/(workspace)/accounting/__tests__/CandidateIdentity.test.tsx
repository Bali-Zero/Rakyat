/** The identity line of a candidate row. All values invented. */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CandidateIdentity } from "../_components/CandidateIdentity";
import { candidate } from "./fixtures";

const line = (over: Parameters<typeof candidate>[0]) => {
  render(<CandidateIdentity candidate={candidate(over)} />);
  return screen.getByTestId("candidate-identity");
};

describe("CandidateIdentity", () => {
  it("whitespace-only number is No invoice number", () => {
    expect(line({ invoice_number: "  \t " }).textContent).toBe(
      "No invoice number",
    );
  });

  it("date n/a shows no date", () => {
    expect(
      line({ invoice_number: "INV-000000-00001", invoice_issued_at: "n/a" })
        .textContent,
    ).toBe("INV-000000-00001");
  });

  it.each([
    ["date only", "2026-01-10"],
    ["full timestamp with offset", "2026-01-10T23:30:00+08:00"],
  ])("%s shows its first ten characters", (_n, stamp) => {
    expect(
      line({ invoice_number: "INV-000000-00001", invoice_issued_at: stamp })
        .textContent,
    ).toBe("INV-000000-00001 · 2026-01-10");
  });

  it("non-string values throw nothing and count as missing", () => {
    const el = line({
      invoice_number: 12 as never,
      invoice_issued_at: { a: 1 } as never,
      service_name: 5 as never,
    });
    expect(el.textContent).toBe("No invoice number");
  });

  it.each([
    [{}, "No invoice number"],
    [
      { service_name: "Example service" },
      "No invoice number · Example service",
    ],
    [
      { invoice_number: "INV-000000-00001", service_name: "  " },
      "INV-000000-00001",
    ],
    [
      { invoice_issued_at: "2026-01-10", service_name: "Example service" },
      "No invoice number · 2026-01-10 · Example service",
    ],
  ])("leaves no dangling separator (%j)", (over, expected) => {
    const text = line(over).textContent as string;
    expect(text).toBe(expected);
    expect(text).not.toMatch(/^\s*·|·\s*$/);
  });

  it("lets a long unbroken value wrap inside its cell", () => {
    const el = line({ invoice_number: "X".repeat(200) });
    expect(el).toHaveClass("min-w-0");
    expect(el.className).toMatch(/break-words|overflow-wrap:anywhere/);
  });
});
