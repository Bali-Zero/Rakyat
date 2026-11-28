import { describe, expect, it } from "vitest";

import {
  bonusDayLabel,
  bonusMonthLabel,
  bonusPaydayText,
} from "./bonus-run-format";

describe("bonus run wording", () => {
  it("keeps a plain date as written", () => {
    expect(bonusDayLabel("2026-12-25")).toBe("25 Dec 2026");
    expect(bonusMonthLabel("2026-11-01")).toBe("November 2026");
    expect(bonusMonthLabel("2026-11-01", true)).toBe("Nov 2026");
  });

  it("dates a timestamp by the Bali day it fell on, not the UTC day", () => {
    expect(bonusDayLabel("2026-12-25T15:59:59Z")).toBe("25 Dec 2026"); // 23:59:59 WITA
    expect(bonusDayLabel("2026-12-25T16:30:00Z")).toBe("26 Dec 2026"); // 00:30 WITA
    expect(bonusDayLabel("2026-12-25T16:30:00+00:00")).toBe("26 Dec 2026");
    expect(bonusDayLabel("2026-12-31T16:00:00Z")).toBe("1 Jan 2027");
    expect(bonusDayLabel("2026-12-26T00:30:00+08:00")).toBe("26 Dec 2026");
  });

  it("states the payday or the salary it waits on", () => {
    expect(bonusPaydayText("2026-12-25", "2026-12-01")).toBe(
      "Expected 25 Dec 2026",
    );
    expect(bonusPaydayText(null, "2026-12-01")).toBe(
      "7 days after the Dec 2026 salary is paid",
    );
  });
});
