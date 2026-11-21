import { describe, expect, it } from "vitest";
import { viewerCanManageTaxDocuments } from "./tax-documents-access";

const SETUP = "staff-setup-01";
const TAX = "staff-tax-03";
const OTHER = "staff-outsider-05";

describe("viewerCanManageTaxDocuments", () => {
  it("trusts the backend boolean when present (even when fallback would disagree)", () => {
    expect(
      viewerCanManageTaxDocuments(
        { viewer_can_manage_tax_documents: true },
        { assigned_to: SETUP, tax_consultant: TAX },
        OTHER,
        "Team Member",
      ),
    ).toBe(true);

    expect(
      viewerCanManageTaxDocuments(
        { viewer_can_manage_tax_documents: false },
        { assigned_to: SETUP, tax_consultant: TAX },
        SETUP,
        "admin",
      ),
    ).toBe(false);
  });

  it("falls back to admin roles and assigned_to / tax_consultant when backend field is absent", () => {
    expect(
      viewerCanManageTaxDocuments(
        {},
        { assigned_to: SETUP, tax_consultant: TAX },
        OTHER,
        "Admin",
      ),
    ).toBe(true);

    expect(
      viewerCanManageTaxDocuments(
        {},
        { assigned_to: SETUP, tax_consultant: TAX },
        ` ${SETUP.toUpperCase()} `,
        "Team Member",
      ),
    ).toBe(true);

    expect(
      viewerCanManageTaxDocuments(
        {},
        { assigned_to: SETUP, tax_consultant: TAX },
        TAX.toUpperCase(),
        "Tax Consultant",
      ),
    ).toBe(true);

    expect(
      viewerCanManageTaxDocuments(
        {},
        { assigned_to: SETUP, tax_consultant: TAX },
        OTHER,
        "Tax Consultant",
      ),
    ).toBe(false);

    expect(
      viewerCanManageTaxDocuments(
        null,
        { assigned_to: SETUP, tax_consultant: TAX },
        null,
        null,
      ),
    ).toBe(false);
  });
});
