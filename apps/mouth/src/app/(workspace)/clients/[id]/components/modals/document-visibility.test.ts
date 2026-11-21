import { describe, it, expect } from "vitest";
import { viewerCanChangeDocumentVisibility } from "./document-visibility";

describe("viewerCanChangeDocumentVisibility", () => {
  it.each([
    "Direktur",
    "Tax Supervisor",
    "Tax Responsible",
    "Tax Consultant",
    "Supervisor",
    "Tax Lead",
    "Team Leader",
    "admin",
    "Founder",
    "CEO",
    "Board Member",
    "  tax supervisor ",
  ])("innocence: %s is offered the toggle", (role) => {
    expect(viewerCanChangeDocumentVisibility(role)).toBe(true);
  });

  it.each([
    "Senior Consultant",
    "Executive Consultant",
    "HR & Accounting",
    "Accounting",
    "Reception",
    "client",
    "",
    null,
    undefined,
  ])("guilt: %s is not offered the toggle", (role) => {
    expect(viewerCanChangeDocumentVisibility(role)).toBe(false);
  });
});
