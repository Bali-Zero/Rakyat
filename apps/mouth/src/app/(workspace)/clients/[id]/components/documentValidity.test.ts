import { afterEach, describe, expect, it } from "vitest";
import {
  dateValidity,
  dayNumber,
  documentValidity,
  expiryWording,
  formatCalendarDate,
  formatCardDate,
  isEnumUrgent,
  isLive,
  isUrgent,
  parseCalendarDate,
  worseLevel,
  type DocumentValidityInput,
} from "./documentValidity";

const DAY = "2026-11-07";

/** A date `n` days from DAY, built by the model's own calendar arithmetic. */
const plus = (n: number): string => {
  const target = dayNumber(DAY) + n;
  const at = new Date(target * 86_400_000);
  return at.toISOString().slice(0, 10);
};

describe("Rule L: liveness mirrors the backend _usable", () => {
  const live = (doc: DocumentValidityInput) => documentValidity(doc, DAY).live;

  describe("deleted_at (L1)", () => {
    it.each([
      ["null", null, true],
      ["undefined", undefined, true],
      ["an empty string", "", false],
      ["a timestamp", "2026-11-01T10:00:00Z", false],
    ])("%s", (_name, deleted_at, expected) => {
      expect(live({ deleted_at })).toBe(expected);
    });

    it("reads removed", () => {
      expect(documentValidity({ deleted_at: "x" }, DAY).voidReason).toBe(
        "removed",
      );
    });
  });

  describe("is_archived (L2)", () => {
    it.each([
      [true, false],
      [false, true],
      [null, true],
      [undefined, true],
    ])("%s", (is_archived, expected) => {
      expect(live({ is_archived })).toBe(expected);
    });

    it("reads archived", () => {
      expect(documentValidity({ is_archived: true }, DAY).voidReason).toBe(
        "archived",
      );
    });
  });

  describe("status (L3 and no-trim)", () => {
    it.each([
      ["rejected", false],
      ["REJECTED", false],
      [" rejected ", true],
      ["rejected ", true],
      ["deleted", false],
      ["Deleted", false],
      ["archived", true],
      ["expired", true],
      ["active", true],
      ["google_drive", true],
      ["pending", true],
      ["something-new", true],
      [null, true],
      [undefined, true],
    ])("%j", (status, expected) => {
      expect(live({ status })).toBe(expected);
    });

    it("reads rejected for rejected and removed for deleted", () => {
      expect(documentValidity({ status: "Rejected" }, DAY).voidReason).toBe(
        "rejected",
      );
      expect(documentValidity({ status: "deleted" }, DAY).voidReason).toBe(
        "removed",
      );
    });
  });

  it("the first test that fires gives the reason", () => {
    expect(
      documentValidity(
        { deleted_at: "x", is_archived: true, status: "rejected" },
        DAY,
      ).voidReason,
    ).toBe("removed");
    expect(
      documentValidity({ is_archived: true, status: "rejected" }, DAY)
        .voidReason,
    ).toBe("archived");
  });

  it("a void document is state void, keeps its date and never reads a positive state", () => {
    const v = documentValidity(
      { status: "rejected", expiry_date: plus(200) },
      DAY,
    );
    expect(v.live).toBe(false);
    expect(v.state).toBe("void");
    expect(v.expiry).toBe(plus(200));
    expect(v.daysLeft).toBe(200);
  });

  it("isLive is the same answer as the model's live field", () => {
    const docs: DocumentValidityInput[] = [
      {},
      { deleted_at: "x" },
      { is_archived: true },
      { status: "rejected" },
      { status: "deleted" },
      { status: " rejected " },
    ];
    for (const doc of docs) {
      expect(isLive(doc)).toBe(documentValidity(doc, DAY).live);
    }
  });
});

describe("Rule D: the backend date grammar", () => {
  it.each([
    ["2026-03-31", "2026-03-31"],
    ["2026-03-31T23:30:00Z", "2026-03-31"],
    ["2026-03-31 23:30", "2026-03-31"],
    ["2024-02-29", "2024-02-29"],
  ])("%s is the date as written", (value, expected) => {
    expect(parseCalendarDate(value)).toBe(expected);
  });

  it.each([
    ["an impossible date", "2026-02-30"],
    ["a non-leap Feb 29", "2026-02-29"],
    ["month 13", "2026-13-01"],
    ["day 0", "2026-01-00"],
    ["year 0", "0000-01-01"],
    ["basic format", "20260331"],
    ["week format", "2026-W14-2"],
    ["an empty string", ""],
    ["free text", "next spring"],
    ["a number", 20260331],
    ["null", null],
    ["undefined", undefined],
    ["an object", {}],
  ])("%s is null", (_name, value) => {
    expect(parseCalendarDate(value)).toBeNull();
  });

  it("does the calendar arithmetic for years below 100 without the 1900s remap", () => {
    expect(dayNumber("0099-01-02") - dayNumber("0099-01-01")).toBe(1);
    expect(dayNumber("1970-01-01")).toBe(0);
    expect(dayNumber("2026-11-07")).toBe(20764);
  });

  it("a date followed by a time is not converted to another day", () => {
    const v = dateValidity("2026-11-08T23:59:59-11:00", DAY);
    expect(v.expiry).toBe("2026-11-08");
    expect(v.daysLeft).toBe(1);
  });

  describe("day 0 and the 60-day boundary", () => {
    it.each([
      [-1, "expired"],
      [0, "expired"],
      [1, "expiring"],
      [60, "expiring"],
      [61, "valid"],
    ] as const)("daysLeft %i is %s", (n, state) => {
      const v = dateValidity(plus(n), DAY);
      expect(v.daysLeft).toBe(n);
      expect(v.state).toBe(state);
    });
  });

  it("an undated value has no daysLeft and no expiry", () => {
    expect(dateValidity(null, DAY)).toEqual({
      state: "undated",
      expiry: null,
      daysLeft: null,
    });
    expect(dateValidity("garbage", DAY).state).toBe("undated");
  });

  it("a status of expired with a future date stays valid or expiring", () => {
    expect(
      documentValidity({ status: "expired", expiry_date: plus(200) }, DAY)
        .state,
    ).toBe("valid");
    expect(
      documentValidity({ status: "expired", expiry_date: plus(10) }, DAY).state,
    ).toBe("expiring");
  });
});

describe("the wording ladder", () => {
  it.each([
    [-3, "Expired 3d ago"],
    [-1, "Expired 1d ago"],
    [0, "Expired today"],
    [1, "⏰ 1d left"],
    [365, "⏰ 365d left"],
  ])("short %i", (n, text) => {
    expect(expiryWording(n)).toBe(text);
  });

  it("goes to years and months past 365 days", () => {
    expect(expiryWording(366)).toMatch(/^1y .*left$/);
  });

  it.each([
    [-3, "Expired 3 days ago"],
    [-1, "Expired 1 day ago"],
    [0, "Expired today"],
    [1, "1 day left"],
    [62, "62 days left"],
  ])("long %i", (n, text) => {
    expect(expiryWording(n, "long")).toBe(text);
  });
});

describe("urgency and enums", () => {
  const live = (
    state: "valid" | "expiring" | "expired" | "undated",
    n: number | null,
  ) => ({
    live: true,
    state,
    daysLeft: n,
  });

  it("is urgent when expired, expiring, inside the window or by enum", () => {
    expect(isUrgent(live("expired", 0), 90)).toBe(true);
    expect(isUrgent(live("expiring", 30), 0)).toBe(true);
    expect(isUrgent(live("valid", 90), 90)).toBe(true);
    expect(isUrgent(live("valid", 91), 90)).toBe(false);
    expect(isUrgent(live("valid", 200), 90, "red")).toBe(true);
    expect(isUrgent(live("valid", 200), 90, "yellow")).toBe(true);
    expect(isUrgent(live("valid", 200), 90, "green")).toBe(false);
  });

  it("an undated live document is never urgent by date", () => {
    expect(isUrgent(live("undated", null), 90)).toBe(false);
  });

  it("a void document is never urgent, whatever it carries", () => {
    expect(
      isUrgent({ live: false, state: "void", daysLeft: -5 }, 90, "expired"),
    ).toBe(false);
  });

  it("a live expired document with a green enum is still urgent", () => {
    expect(isUrgent(live("expired", -4), 90, "green")).toBe(true);
  });

  it("isEnumUrgent reads red, yellow and expired only", () => {
    expect(isEnumUrgent("red")).toBe(true);
    expect(isEnumUrgent("yellow")).toBe(true);
    expect(isEnumUrgent("expired")).toBe(true);
    expect(isEnumUrgent("green")).toBe(false);
    expect(isEnumUrgent(null)).toBe(false);
  });

  it("the worse of an enum and the model wins, never the better", () => {
    expect(worseLevel("green", "expired")).toBe("expired");
    expect(worseLevel("expired", "valid")).toBe("expired");
    expect(worseLevel("yellow", "valid")).toBe("yellow");
    expect(worseLevel("yellow", "expiring")).toBe("red");
    expect(worseLevel("red", "valid")).toBe("red");
    expect(worseLevel(null, "expiring")).toBe("red");
    expect(worseLevel(undefined, "valid")).toBe("green");
    expect(worseLevel("red", "undated")).toBe("red");
    expect(worseLevel(null, "undated")).toBeNull();
  });
});

describe("the date formatter", () => {
  const original = process.env.TZ;
  afterEach(() => {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  });

  it.each(["America/Los_Angeles", "Asia/Makassar", "Pacific/Kiritimati"])(
    "renders the same calendar day in %s",
    (tz) => {
      process.env.TZ = tz;
      expect(formatCalendarDate("2026-03-31")).toBe("31 Mar 2026");
      expect(formatCalendarDate("2026-01-01")).toBe("1 Jan 2026");
      expect(formatCalendarDate("2026-03-31T23:00:00Z")).toBe("31 Mar 2026");
    },
  );

  it("renders a null date as a dash", () => {
    expect(formatCalendarDate(null)).toBe("—");
    expect(formatCalendarDate("nope")).toBe("—");
  });

  it("formatCardDate keeps the SSR placeholder and the empty string", () => {
    expect(formatCardDate("2026-03-31", false)).toBe("...");
    expect(formatCardDate("", true)).toBe("");
    expect(formatCardDate("2026-03-31", true)).toBe("31 Mar 2026");
  });

  it("formatCardDate keeps local formatting for an instant", () => {
    process.env.TZ = "Asia/Makassar";
    expect(formatCardDate("2026-03-31T17:00:00Z", true)).toBe("1 Apr 2026");
  });
});
