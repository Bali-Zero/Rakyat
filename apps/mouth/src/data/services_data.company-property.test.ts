import { describe, expect, it } from "vitest";

import { SERVICES_DATA } from "./services_data";
import { getExactSnapshotPrice } from "@/lib/pricing-snapshot";

describe("/services company, property & tax SSOT alignment", () => {
  it("wires PT PMA Setup and Alcohol License (NPBBKC) to exact SSOT prices", () => {
    const pma = SERVICES_DATA.company.packages.find(
      (pkg) => pkg.name === "PT PMA Setup",
    );
    expect(pma?.livePriceCategory).toBe("company_services");
    expect(pma?.livePriceKey).toBe("New Company (PT PMA)");
    expect(
      getExactSnapshotPrice(pma!.livePriceCategory!, pma!.livePriceKey!),
    ).toBe("25.000.000 IDR");

    const alcohol = SERVICES_DATA.company.packages.find(
      (pkg) => pkg.name === "Alcohol License (NPBBKC)",
    );
    expect(alcohol?.livePriceCategory).toBe("company_services");
    expect(alcohol?.livePriceKey).toBe("Alcohol License — Golongan A + B + C");
    expect(
      getExactSnapshotPrice(
        alcohol!.livePriceCategory!,
        alcohol!.livePriceKey!,
      ),
    ).toBe("12.000.000 IDR");
  });

  it("states PP 18/2021 80-year HGB and PP 16/2021 PBG/SLF via SIMBG", () => {
    const hgbFaq = SERVICES_DATA.property.faqs.find((f) =>
      f.question.includes("Hak Guna Bangunan"),
    );
    expect(hgbFaq?.answer).toContain("80 years");
    expect(hgbFaq?.answer).toMatch(/30.*20.*30/);

    const pbgFaq = SERVICES_DATA.property.faqs.find((f) =>
      f.question.includes("IMB and PBG"),
    );
    expect(pbgFaq?.answer).toContain("SIMBG");
    expect(pbgFaq?.answer).toContain("SLF");
  });

  it("states PMK 81/2024 monthly tax deadlines (15th payment, 20th filing, end of following month PPN)", () => {
    const deadlineFaq = SERVICES_DATA.tax.faqs.find((f) =>
      f.question.includes("tax filing deadline"),
    );
    expect(deadlineFaq?.answer).toContain("15th");
    expect(deadlineFaq?.answer).toContain("20th");
    expect(deadlineFaq?.answer).toContain("end of the following month");
  });
});
