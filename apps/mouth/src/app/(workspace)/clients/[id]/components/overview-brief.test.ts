import { describe, expect, it } from "vitest";
import type {
  Client,
  ClientDocument,
  ClientProfile,
  Interaction,
} from "@/lib/api/crm/crm.types";
import {
  ageYears,
  contactFact,
  daysUntil,
  pipelineAmount,
  witaDateKey,
  initials,
  isBalanceUnknown,
  outstandingAmount,
  passportFact,
  permitFact,
  type PermitFact,
} from "./overview-brief";

const NOW = Date.parse("2026-11-03T00:00:00Z");
type Practice = ClientProfile["practices"][number];
const prac = (over: Record<string, unknown>) => over as unknown as Practice;
const doc = (over: Record<string, unknown>) =>
  ({
    id: 1,
    document_category: "immigration",
    document_type: "kitas",
    ...over,
  }) as unknown as ClientDocument;
const client = (over: Record<string, unknown> = {}) =>
  over as unknown as Client;
const inter = (over: Record<string, unknown>) => over as unknown as Interaction;
const permit = (days: number | null): PermitFact => ({
  kind: days === null ? "none" : "dated",
  label: "",
  days,
  expiry: null,
  openVisaPractice: null,
});

describe("outstandingAmount", () => {
  it("GUILT: partial = price minus paid", () => {
    expect(
      outstandingAmount([
        prac({
          actual_price: 14500000,
          paid_amount: 7000000,
          payment_status: "partial",
        }),
      ]),
    ).toBe(7500000);
  });
  it("GUILT: unpaid = full price", () => {
    expect(
      outstandingAmount([
        prac({ actual_price: 1000, payment_status: "unpaid" }),
      ]),
    ).toBe(1000);
  });
  it("GUILT: a partial practice with no paid_amount is unknown, not the full price", () => {
    const p = prac({ actual_price: 14500000, payment_status: "partial" });
    expect(isBalanceUnknown(p)).toBe(true);
    expect(outstandingAmount([p])).toBe(0);
  });
  it("INNOCENCE: unpaid without paid_amount still owes the full price and is known; partial with paid_amount 0 is known", () => {
    const unpaid = prac({ actual_price: 1000, payment_status: "unpaid" });
    expect(isBalanceUnknown(unpaid)).toBe(false);
    expect(outstandingAmount([unpaid])).toBe(1000);
    const zero = prac({
      actual_price: 1000,
      paid_amount: 0,
      payment_status: "partial",
    });
    expect(isBalanceUnknown(zero)).toBe(false);
    expect(outstandingAmount([zero])).toBe(1000);
  });
  it("INNOCENCE: paid or undefined status owes nothing", () => {
    expect(
      outstandingAmount([prac({ actual_price: 1000, payment_status: "paid" })]),
    ).toBe(0);
    expect(outstandingAmount([prac({ actual_price: 1000 })])).toBe(0);
  });
  it("INNOCENCE: overpaid never goes negative", () => {
    expect(
      outstandingAmount([
        prac({
          actual_price: 1000,
          paid_amount: 5000,
          payment_status: "partial",
        }),
      ]),
    ).toBe(0);
  });
  it("GUILT: string numerics are coerced", () => {
    expect(
      outstandingAmount([
        prac({
          actual_price: "14500000.00",
          paid_amount: "500000.00",
          payment_status: "partial",
        }),
      ]),
    ).toBe(14000000);
  });
});

describe("passportFact.endsBeforePermit", () => {
  const c = client({ passport_expiry: "2027-01-01" });
  it("GUILT: both future and passport earlier", () => {
    expect(passportFact(c, [], permit(400), NOW).endsBeforePermit).toBe(true);
  });
  it("INNOCENCE: passport later than permit", () => {
    expect(passportFact(c, [], permit(10), NOW).endsBeforePermit).toBe(false);
  });
  it("INNOCENCE: permit date unknown", () => {
    expect(passportFact(c, [], permit(null), NOW).endsBeforePermit).toBe(false);
  });
  it("INNOCENCE: passport date unknown", () => {
    expect(passportFact(client(), [], permit(400), NOW).endsBeforePermit).toBe(
      false,
    );
  });
  it("INNOCENCE: passport already expired is not 'ends before permit'", () => {
    expect(
      passportFact(
        client({ passport_expiry: "2026-01-01" }),
        [],
        permit(400),
        NOW,
      ).endsBeforePermit,
    ).toBe(false);
  });
  it("INNOCENCE: permit already expired", () => {
    expect(passportFact(c, [], permit(-5), NOW).endsBeforePermit).toBe(false);
  });
});

describe("permitFact", () => {
  it("none: no immigration document", () => {
    expect(permitFact([], [], NOW).kind).toBe("none");
    expect(
      permitFact([doc({ document_category: "identity" })], [], NOW).kind,
    ).toBe("none");
  });
  it("undated: a visa document without expiry", () => {
    const f = permitFact([doc({ expiry_date: null })], [], NOW);
    expect(f.kind).toBe("undated");
    expect(f.days).toBeNull();
  });
  it("dated: days until expiry and label", () => {
    const f = permitFact(
      [
        doc({
          expiry_date: "2026-11-13T00:00:00Z",
          permit_label: "Work KITAS",
        }),
      ],
      [],
      NOW,
    );
    expect(f.kind).toBe("dated");
    expect(f.days).toBe(10);
    expect(f.label).toBe("Work KITAS");
  });
  it("INNOCENCE: archived or family-member documents are ignored", () => {
    expect(
      permitFact(
        [doc({ is_archived: true, expiry_date: "2027-01-01" })],
        [],
        NOW,
      ).kind,
    ).toBe("none");
    expect(
      permitFact(
        [doc({ family_member_id: 3, expiry_date: "2027-01-01" })],
        [],
        NOW,
      ).kind,
    ).toBe("none");
  });
  it("farthest expiry wins among dated documents", () => {
    const f = permitFact(
      [
        doc({ id: 1, expiry_date: "2026-12-01T00:00:00Z" }),
        doc({ id: 2, expiry_date: "2027-06-01T00:00:00Z" }),
      ],
      [],
      NOW,
    );
    expect(f.expiry).toBe("2027-06-01T00:00:00Z");
  });
});

describe("contactFact", () => {
  it("GUILT: newest interaction wins over the client's own date", () => {
    const f = contactFact(
      client({ last_interaction_date: "2026-01-01T00:00:00Z" }),
      [
        inter({
          interaction_date: "2026-10-20T00:00:00Z",
          created_at: "2026-10-20T00:00:00Z",
          interaction_type: "whatsapp",
          summary: "old",
        }),
        inter({
          interaction_date: "2026-11-01T00:00:00Z",
          created_at: "2026-11-01T00:00:00Z",
          interaction_type: "email",
          summary: "new",
        }),
      ],
      NOW,
    );
    expect(f.days).toBe(2);
    expect(f.channel).toBe("Email");
    expect(f.summary).toBe("new");
  });
  it("fallback: client's last_interaction_date when no interactions", () => {
    const f = contactFact(
      client({ last_interaction_date: "2026-10-24T00:00:00Z" }),
      [],
      NOW,
    );
    expect(f.days).toBe(10);
    expect(f.channel).toBeNull();
  });
  it("INNOCENCE: null when neither exists", () => {
    expect(contactFact(client(), [], NOW)).toEqual({
      days: null,
      channel: null,
      summary: null,
    });
  });
});

describe("initials", () => {
  it("first and last word", () =>
    expect(initials("Sample Fixture Person")).toBe("SP"));
  it("single word", () => expect(initials("Madonna")).toBe("M"));
  it("empty", () => expect(initials("")).toBe(""));
});

describe("ageYears", () => {
  it("GUILT: birthday not yet reached this year subtracts one", () => {
    expect(ageYears("1990-12-25", NOW)).toBe(35);
  });
  it("birthday already passed this year", () => {
    expect(ageYears("1990-01-15", NOW)).toBe(36);
  });
  it("INNOCENCE: unknown or invalid is null", () => {
    expect(ageYears(undefined, NOW)).toBeNull();
    expect(ageYears("nope", NOW)).toBeNull();
  });
});

// 2026-11-03T00:30+08:00 — Bali is already on the 3rd, UTC is still on the 2nd.
const WITA_MORNING = Date.parse("2026-11-02T16:30:00Z");
// 2026-11-02T23:59+08:00 — one minute before WITA midnight.
const WITA_LATE = Date.parse("2026-11-02T15:59:00Z");

describe("calendar days in WITA", () => {
  it("witaDateKey reads the Bali calendar date of an instant", () => {
    expect(witaDateKey(WITA_MORNING)).toBe("2026-11-03");
    expect(witaDateKey(WITA_LATE)).toBe("2026-11-02");
  });
  it("GUILT: just after WITA midnight an expiry of today is 0, yesterday is -1", () => {
    expect(daysUntil("2026-11-03", witaDateKey(WITA_MORNING))).toBe(0);
    expect(daysUntil("2026-11-02", witaDateKey(WITA_MORNING))).toBe(-1);
    expect(daysUntil("2026-11-04", witaDateKey(WITA_MORNING))).toBe(1);
  });
  it("GUILT: just before WITA midnight an expiry of today is still 0", () => {
    expect(daysUntil("2026-11-02", witaDateKey(WITA_LATE))).toBe(0);
    expect(daysUntil("2026-11-03", witaDateKey(WITA_LATE))).toBe(1);
  });
  it("a full instant is read as written, with no timezone conversion (the backend's value[:10])", () => {
    // 2026-11-02T17:00Z is 2026-11-03 01:00 in Bali, but the backend reads
    // the first ten characters: the 2nd.
    expect(daysUntil("2026-11-02T17:00:00Z", witaDateKey(WITA_MORNING))).toBe(
      -1,
    );
    expect(
      daysUntil("2026-11-03T23:59:00-11:00", witaDateKey(WITA_MORNING)),
    ).toBe(0);
  });
  it("INNOCENCE: missing or unparsable dates are null", () => {
    expect(daysUntil(undefined, witaDateKey(NOW))).toBeNull();
    expect(daysUntil("nope", witaDateKey(NOW))).toBeNull();
  });
  it("GUILT: contact days count WITA calendar dates, not 24h blocks", () => {
    // 23:59 WITA on the 2nd vs 00:30 WITA on the 3rd: one calendar day apart.
    const f = contactFact(
      client({ last_interaction_date: "2026-11-02T15:59:00Z" }),
      [],
      WITA_MORNING,
    );
    expect(f.days).toBe(1);
  });
  it("GUILT: age uses the WITA date of now (birthday on the 3rd already reached)", () => {
    expect(ageYears("1990-11-03", WITA_MORNING)).toBe(36);
    expect(ageYears("1990-11-03", WITA_LATE)).toBe(35);
  });
});

describe("contactFact source choice", () => {
  it("GUILT: a newer client date beats an older interaction, text stays with the client", () => {
    const f = contactFact(
      client({
        last_interaction_date: "2026-11-02T00:00:00Z",
        last_interaction_summary: "client summary",
      }),
      [
        inter({
          interaction_date: "2026-10-01T00:00:00Z",
          created_at: "2026-10-01T00:00:00Z",
          interaction_type: "whatsapp",
          summary: "interaction summary",
        }),
      ],
      NOW,
    );
    expect(f.days).toBe(1);
    expect(f.channel).toBeNull();
    expect(f.summary).toBe("client summary");
  });
  it("INNOCENCE: a newer interaction beats an older client date, text stays with it", () => {
    const f = contactFact(
      client({
        last_interaction_date: "2026-01-01T00:00:00Z",
        last_interaction_summary: "client summary",
      }),
      [
        inter({
          interaction_date: "2026-11-01T00:00:00Z",
          created_at: "2026-11-01T00:00:00Z",
          interaction_type: "email",
          summary: "interaction summary",
        }),
      ],
      NOW,
    );
    expect(f.channel).toBe("Email");
    expect(f.summary).toBe("interaction summary");
  });
});

describe("pipelineAmount", () => {
  it("GUILT: sums actual, else quoted, with string numerics coerced", () => {
    expect(
      pipelineAmount([
        prac({ actual_price: "1000.00", quoted_price: 5 }),
        prac({ quoted_price: 500 }),
      ]),
    ).toBe(1500);
  });
  it("INNOCENCE: unpriced or garbage prices add nothing", () => {
    expect(pipelineAmount([prac({}), prac({ actual_price: "abc" })])).toBe(0);
  });
});

describe("impossible calendar dates (gate round 2)", () => {
  it("GUILT: a month 13 or a 30 February is refused, never rolled over", () => {
    expect(daysUntil("2026-13-01", witaDateKey(NOW))).toBeNull();
    expect(daysUntil("2026-02-30", witaDateKey(NOW))).toBeNull();
    expect(ageYears("1990-13-01", NOW)).toBeNull();
  });

  it("INNOCENCE: a real leap day and an ordinary date still count", () => {
    expect(daysUntil("2028-02-29", witaDateKey(NOW))).not.toBeNull();
    expect(daysUntil("2026-11-10", witaDateKey(NOW))).toBe(7);
    expect(ageYears("1990-01-01", NOW)).toBe(36);
  });
});
