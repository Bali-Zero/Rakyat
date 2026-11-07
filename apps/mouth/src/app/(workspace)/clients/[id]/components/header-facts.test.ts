import { describe, expect, it } from "vitest";
import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { firstName, permitLine, roleLine } from "./header-facts";

const link = (over: Record<string, unknown>) =>
  ({
    link_id: 1,
    company_id: 1,
    company_name: "PT Contoh Abadi",
    company_type: "PT PMA",
    role: "director",
    is_primary: false,
    status: "active",
    ...over,
  }) as never;

const doc = (over: Partial<ClientDocument>): ClientDocument =>
  ({
    id: 1,
    client_id: 1,
    document_type: "kitas",
    document_category: "immigration",
    expiry_date: "2030-01-01",
    ...over,
  }) as ClientDocument;

describe("roleLine", () => {
  it("humanises the role and names the PT", () => {
    expect(roleLine([link({})])).toBe("Director · PT Contoh Abadi");
    expect(roleLine([link({ role: "commissioner" })])).toBe(
      "Commissioner · PT Contoh Abadi",
    );
  });
  it("prefers the primary link, else the first with a role", () => {
    expect(
      roleLine([
        link({ role: "shareholder", company_name: "PT Satu" }),
        link({ role: "director", company_name: "PT Dua", is_primary: true }),
      ]),
    ).toBe("Director · PT Dua");
    expect(
      roleLine([
        link({ role: "", company_name: "PT Kosong" }),
        link({ role: "shareholder", company_name: "PT Satu" }),
      ]),
    ).toBe("Shareholder · PT Satu");
  });
  it("is null with no links, no role or no company name", () => {
    expect(roleLine(undefined)).toBeNull();
    expect(roleLine([])).toBeNull();
    expect(roleLine([link({ role: "  " })])).toBeNull();
    expect(roleLine([link({ company_name: "" })])).toBeNull();
  });
});

describe("permitLine", () => {
  it("is `code · name`, dropping a copy of the code the label leads with", () => {
    expect(
      permitLine([
        doc({ permit_code: "E28A", permit_label: "E28A — Investor KITAS" }),
      ]),
    ).toBe("E28A · Investor KITAS");
    expect(
      permitLine([
        doc({ permit_code: "E28A", permit_label: "Investor KITAS" }),
      ]),
    ).toBe("E28A · Investor KITAS");
  });
  it("falls back to the family label, then the raw type; never invents a code", () => {
    expect(permitLine([doc({ permit_family_label: "KITAS / ITAS" })])).toBe(
      "KITAS / ITAS",
    );
    expect(permitLine([doc({})])).toBe("Kitas");
  });
  it("is null with no current permit", () => {
    expect(permitLine([])).toBeNull();
    expect(
      permitLine([doc({ deleted_at: "2026-10-01T00:00:00Z" })]),
    ).toBeNull();
    expect(permitLine([doc({ family_member_id: 3 })])).toBeNull();
  });
});

describe("firstName", () => {
  it("is the first word of the display name", () => {
    expect(firstName("Ayu Sample Person", "ayu@example.test")).toBe("Ayu");
    expect(firstName("Dewa Ayu", "dewaayu.tax@example.test")).toBe("Dewa");
  });
  it("falls back to the first segment of the email local part, capitalised", () => {
    expect(firstName(undefined, "kadek.tax@example.test")).toBe("Kadek");
    expect(firstName("", "member_a@example.test")).toBe("Member");
  });
  it("never leaks an address when the name is itself an email", () => {
    expect(firstName("kadek.tax@example.test", undefined)).toBe("Kadek");
  });
  it("is empty when there is nothing", () => {
    expect(firstName(undefined, undefined)).toBe("");
  });
});
