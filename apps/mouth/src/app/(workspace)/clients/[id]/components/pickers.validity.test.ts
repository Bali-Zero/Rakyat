/**
 * Pickers (rows 1, 2) and the family result (rows 25, 27): a rejected
 * document is never the current one, and its live twin still is.
 */
import { describe, expect, it } from "vitest";
import type { ClientDocument, FamilyMember } from "@/lib/api/crm/crm.types";
import {
  countVoidPermitsFor,
  pickCurrentPermitFor,
  pickCurrentVisa,
} from "./pickCurrentVisa";
import { countVoidPassports, pickCurrentPassport } from "./utils";
import {
  familyPassport,
  familyPermit,
  levelPill,
  pickMemberPassport,
} from "./familyValidity";

const DAY = "2026-11-07";

const permit = (
  over: Partial<ClientDocument> & { id: number },
): ClientDocument => ({
  client_id: 1,
  document_type: "kitas",
  document_category: "immigration",
  ...over,
});

const passport = (
  over: Partial<ClientDocument> & { id: number },
): ClientDocument => ({
  client_id: 1,
  document_type: "passport",
  document_category: "personal",
  ...over,
});

describe("pickCurrentVisa — Rule L", () => {
  const farthest = {
    expiry_date: "2030-01-01",
    created_at: "2026-01-01T00:00:00Z",
  };
  const twin = {
    expiry_date: "2028-01-01",
    created_at: "2026-02-01T00:00:00Z",
  };

  it.each([
    ["rejected", { status: "rejected" }],
    ["REJECTED", { status: "REJECTED" }],
    ["deleted", { status: "deleted" }],
    ["removed by the client", { deleted_at: "2026-10-01T00:00:00Z" }],
    ["archived", { is_archived: true }],
  ] as const)(
    "GUILT: a %s document with the farthest expiry is never picked; its live twin is",
    (_n, voidFields) => {
      const docs = [
        permit({
          id: 1,
          ...farthest,
          ...(voidFields as Partial<ClientDocument>),
        }),
        permit({ id: 2, ...twin }),
      ];
      expect(pickCurrentVisa(docs)?.id).toBe(2);
    },
  );

  it("INNOCENCE: the same fields, live, ARE picked — the farthest expiry still wins", () => {
    const docs = [permit({ id: 1, ...farthest }), permit({ id: 2, ...twin })];
    expect(pickCurrentVisa(docs)?.id).toBe(1);
  });

  it.each([
    [" rejected ", "trimmed statuses are live"],
    ["expired", "an expired status is live"],
    ["active", "active is live"],
    ["google_drive", "google_drive is live"],
    ["archived", "an archived STATUS is live (only is_archived voids)"],
  ])("INNOCENCE: status %j is live — %s", (status, _why) => {
    expect(
      pickCurrentVisa([permit({ id: 1, status: status as never })])?.id,
    ).toBe(1);
  });

  it("a void-only holder has no current permit", () => {
    expect(
      pickCurrentPermitFor([permit({ id: 1, status: "rejected" })], null),
    ).toBeUndefined();
  });

  it("GUILT: the family-member pick skips a rejected permit too", () => {
    const docs = [
      permit({
        id: 1,
        family_member_id: 9,
        expiry_date: "2031-01-01",
        status: "rejected",
      }),
      permit({ id: 2, family_member_id: 9, expiry_date: "2028-01-01" }),
    ];
    expect(pickCurrentPermitFor(docs, 9)?.id).toBe(2);
  });

  it("counts the void permits of one holder for the signpost", () => {
    const docs = [
      permit({ id: 1, status: "rejected" }),
      permit({ id: 2, deleted_at: "2026-10-01T00:00:00Z" }),
      permit({ id: 3 }),
      permit({ id: 4, family_member_id: 9, status: "rejected" }),
    ];
    expect(countVoidPermitsFor(docs, null)).toBe(2);
    expect(countVoidPermitsFor(docs, 9)).toBe(1);
  });
});

describe("pickCurrentPassport — Rule L", () => {
  it("GUILT: a rejected passport uploaded last is never current; the older live one is", () => {
    const docs = [
      passport({ id: 1, created_at: "2025-01-01T00:00:00Z" }),
      passport({
        id: 2,
        created_at: "2026-10-01T00:00:00Z",
        status: "rejected",
      }),
    ];
    expect(pickCurrentPassport(docs)?.id).toBe(1);
  });

  it("INNOCENCE: the same newest passport, live, IS current", () => {
    const docs = [
      passport({ id: 1, created_at: "2025-01-01T00:00:00Z" }),
      passport({ id: 2, created_at: "2026-10-01T00:00:00Z" }),
    ];
    expect(pickCurrentPassport(docs)?.id).toBe(2);
  });

  it("counts the void own passports for the signpost, not a family passport", () => {
    const docs = [
      passport({ id: 1, status: "rejected" }),
      passport({ id: 2, deleted_at: "2026-10-01T00:00:00Z" }),
      passport({ id: 3, family_member_id: 5, status: "rejected" }),
      passport({ id: 4 }),
    ];
    expect(countVoidPassports(docs)).toBe(2);
  });
});

const member = (over: Partial<FamilyMember> = {}): FamilyMember =>
  ({
    id: 5,
    client_id: 1,
    full_name: "Family Member A",
    relationship: "spouse",
    ...over,
  }) as FamilyMember;

describe("familyPermit — one shared result", () => {
  it("source field: the member's own date, enum read", () => {
    const r = familyPermit(
      member({ visa_expiry: "2027-09-15", visa_alert: "green" }),
      [],
      DAY,
    );
    expect(r.source).toBe("field");
    expect(r.expiry).toBe("2027-09-15");
    expect(r.level).toBe("green");
    expect(r.urgent).toBe(false);
  });

  it("GUILT: NULL visa_expiry with enum green and an expired live fallback document reads expired", () => {
    const r = familyPermit(
      member({ visa_expiry: undefined, visa_alert: "green" }),
      [permit({ id: 1, family_member_id: 5, expiry_date: "2026-10-01" })],
      DAY,
    );
    expect(r.source).toBe("document");
    expect(r.level).toBe("expired");
    expect(levelPill(r.level!).label).toBe("Expired");
    expect(r.urgent).toBe(true);
  });

  it("GUILT: enum expired with a model valid reads expired — the worse wins", () => {
    const r = familyPermit(
      member({ visa_expiry: "2028-01-01", visa_alert: "expired" }),
      [],
      DAY,
    );
    expect(r.level).toBe("expired");
    expect(r.urgent).toBe(true);
  });

  it("INNOCENCE: an enum green with a model valid stays valid and quiet", () => {
    const r = familyPermit(
      member({ visa_expiry: "2028-01-01", visa_alert: "green" }),
      [],
      DAY,
    );
    expect(r.level).toBe("green");
    expect(r.urgent).toBe(false);
  });

  it("GUILT: a rejected fallback document is never picked — no date, source none", () => {
    const r = familyPermit(
      member({ visa_expiry: undefined, visa_alert: "red" }),
      [
        permit({
          id: 1,
          family_member_id: 5,
          expiry_date: "2029-01-01",
          status: "rejected",
        }),
      ],
      DAY,
    );
    expect(r.source).toBe("none");
    expect(r.validity).toBeNull();
    expect(r.level).toBeNull();
  });

  it("INNOCENCE: the same fallback document, live, is picked", () => {
    const r = familyPermit(
      member({ visa_expiry: undefined }),
      [permit({ id: 1, family_member_id: 5, expiry_date: "2029-01-01" })],
      DAY,
    );
    expect(r.source).toBe("document");
    expect(r.level).toBe("green");
  });

  it("the enum source rule: an enum is not read when the date comes from a document", () => {
    const r = familyPermit(
      member({ visa_expiry: undefined, visa_alert: "red" }),
      [permit({ id: 1, family_member_id: 5, expiry_date: "2029-01-01" })],
      DAY,
    );
    expect(r.level).toBe("green");
    expect(r.urgent).toBe(false);
  });

  it("an unparseable own date falls through to the live document", () => {
    const r = familyPermit(
      member({ visa_expiry: "not a date", visa_alert: "red" }),
      [permit({ id: 1, family_member_id: 5, expiry_date: "2029-01-01" })],
      DAY,
    );
    expect(r.source).toBe("document");
  });
});

describe("familyPassport", () => {
  it("is urgent at 180 days with no enum, not at 181", () => {
    // 2026-11-07 + 180 = 2027-05-06
    const at180 = familyPassport(
      member({ passport_expiry: "2027-05-06" }),
      [],
      DAY,
    );
    const at181 = familyPassport(
      member({ passport_expiry: "2027-05-07" }),
      [],
      DAY,
    );
    expect(at180.validity?.daysLeft).toBe(180);
    expect(at180.urgent).toBe(true);
    expect(at181.validity?.daysLeft).toBe(181);
    expect(at181.urgent).toBe(false);
  });

  it("with an enum the 180-day fallback is not used", () => {
    const r = familyPassport(
      member({ passport_expiry: "2027-06-01", passport_alert: "green" }),
      [],
      DAY,
    );
    expect(r.urgent).toBe(false);
  });

  it("GUILT: a green enum never turns an expired passport better", () => {
    const r = familyPassport(
      member({ passport_expiry: "2026-10-01", passport_alert: "green" }),
      [],
      DAY,
    );
    expect(r.urgent).toBe(true);
    expect(r.level).toBe("expired");
  });

  it("GUILT: the newest LIVE passport document is the fallback; a rejected newer one is not", () => {
    const docs = [
      passport({
        id: 1,
        family_member_id: 5,
        expiry_date: "2030-01-01",
        created_at: "2025-01-01T00:00:00Z",
      }),
      passport({
        id: 2,
        family_member_id: 5,
        expiry_date: "2026-01-01",
        created_at: "2026-06-01T00:00:00Z",
        status: "rejected",
      }),
    ];
    expect(pickMemberPassport(docs, 5)?.id).toBe(1);
    const r = familyPassport(member({ passport_expiry: undefined }), docs, DAY);
    expect(r.source).toBe("document");
    expect(r.expiry).toBe("2030-01-01");
  });
});
