import { describe, expect, it } from "vitest";
import type { Practice } from "@/lib/api/crm/crm.types";
import { mergePracticeUpdate } from "./mergePracticeUpdate";

const base = {
  id: 5,
  client_id: 7,
  status: "inquiry",
  priority: "normal",
  payment_status: "unpaid",
  practice_type_id: 1,
  created_at: "2026-11-12T00:00:00Z",
} as Practice;

describe("mergePracticeUpdate", () => {
  it("takes the response's own fields", () => {
    const merged = mergePracticeUpdate(base, { ...base, priority: "urgent" });
    expect(merged.priority).toBe("urgent");
  });

  it("an older response never restores an older invoice company, in either order", () => {
    const savedB = {
      ...base,
      invoice_company_id: 2,
      invoice_company_name: "PT B",
    };
    const olderA = {
      ...base,
      notes: "older save",
      invoice_company_id: 1,
      invoice_company_name: "PT A",
    };
    // card saved B, then the older notes response (carrying A) arrives
    const afterOlder = mergePracticeUpdate(savedB, olderA);
    expect(afterOlder.notes).toBe("older save");
    expect(afterOlder.invoice_company_id).toBe(2);
    expect(afterOlder.invoice_company_name).toBe("PT B");
    // older response first, card's own patch second
    const first = mergePracticeUpdate(base, olderA);
    expect(first.invoice_company_id).toBeUndefined();
    const second = {
      ...first,
      invoice_company_id: 2,
      invoice_company_name: "PT B",
    };
    expect(
      mergePracticeUpdate(second, { ...base, priority: "high" })
        .invoice_company_id,
    ).toBe(2);
  });

  it("starts from the response when there is no current practice", () => {
    expect(mergePracticeUpdate(null, base)).toBe(base);
  });

  it("an unrelated save never restores an older service, the picker's own save does", () => {
    const tax = {
      ...base,
      practice_type_id: 9,
      practice_type_code: "tax",
      practice_type_name: "Tax",
    };
    const olderNotes = {
      ...base,
      notes: "older",
      practice_type_id: 1,
      practice_type_code: "open_inquiry",
      practice_type_name: "Open inquiry",
    };
    const kept = mergePracticeUpdate(tax, olderNotes);
    expect(kept.notes).toBe("older");
    expect(kept.practice_type_code).toBe("tax");
    expect(kept.practice_type_id).toBe(9);
    expect(kept.practice_type_name).toBe("Tax");
    const picked = mergePracticeUpdate(base, tax, { service: true });
    expect(picked.practice_type_code).toBe("tax");
  });
});
