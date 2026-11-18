import { describe, expect, it } from "vitest";

import { viewerCanCreateCompany } from "./company-create";

describe("viewerCanCreateCompany — mirrors crm_utils.py can_create_company", () => {
  it.each([
    "admin",
    "Founder",
    "CEO",
    "board member",
    "Tax Consultant",
    "tax lead",
    "Tax Supervisor",
    " Tax Responsible ",
  ])("allows %s", (role) => {
    expect(viewerCanCreateCompany(role)).toBe(true);
  });

  it.each([
    "consultant",
    "junior consultant",
    "tax care",
    "team leader",
    "reception",
    "",
    null,
    undefined,
  ])("refuses %s", (role) => {
    expect(viewerCanCreateCompany(role)).toBe(false);
  });
});
