import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { getCode } from "@/lib/kbli-data";
import { LicensingSection } from "./LicensingSection";

function cellValue(label: string): string {
  const cell = screen.getByText(label).parentElement;
  return (cell?.textContent ?? "").replace(label, "").trim();
}

describe("LicensingSection — Web ↔ macOS risk/licensing/EN coherence", () => {
  it("renders full multi-tier Risk Level range on 56101 without understating Besar/PMA tier", () => {
    const kbli = getCode("56101")!;
    render(<LicensingSection kbli={kbli} gold={null} />);

    expect(cellValue("Risk Level")).toBe(
      "Medium-Low → Medium-High (Menengah Rendah → Menengah Tinggi)",
    );
    expect(cellValue("License Type")).toBe("NIB + Sertifikat Standar");
  });

  it("renders multi-tier License Type range on 01115 instead of Mikro-only NIB", () => {
    const kbli = getCode("01115")!;
    render(<LicensingSection kbli={kbli} gold={null} />);

    expect(cellValue("Risk Level")).toBe(
      "Low → Medium-High (Rendah → Menengah Tinggi)",
    );
    expect(cellValue("License Type")).toBe("NIB → NIB + Sertifikat Standar");
  });

  it("renders English scale labels, formatted timeframe pill, and explicit (Indonesian) obligation header", () => {
    const kbli = getCode("56101")!;
    render(<LicensingSection kbli={kbli} gold={null} />);

    // Scale tabs and active tier header use English Micro/Small/Medium/Large (parity with macOS ScaleMatrix)
    expect(
      screen.getAllByRole("button", { name: "Micro" }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: "Small" }).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Medium" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Large" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mikro" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Besar" })).toBeNull();

    // Timeframe pill in PP28 detail formats raw "Otomatis" as "Instant"
    expect(screen.getByText("⏱ Instant")).toBeInTheDocument();
    expect(screen.queryByText("⏱ Otomatis")).toBeNull();

    // Raw Indonesian obligations carry explicit (Indonesian) label like Requirements (Indonesian)
    expect(
      screen.getByText("Post-License Obligations (Indonesian)"),
    ).toBeInTheDocument();
  });
});
