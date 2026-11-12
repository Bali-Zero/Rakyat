import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
  requestWithCredential: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

import * as hr from "./hr";

beforeEach(() => vi.clearAllMocks());

describe("bonus client", () => {
  it("reads the practice types for the dropdowns", () => {
    hr.listBonusPracticeTypes();
    expect(apiMock.get).toHaveBeenCalledWith("/api/hr/bonus-practice-types");
  });

  it("adds a manual bonus with the payload as given", () => {
    const body = {
      employee_id: 7,
      amount_idr: 150000,
      reason: "Weekend filing",
      practice_type_code: null,
      awarded_on: "2026-11-10",
    };
    hr.createManualBonus(body);
    expect(apiMock.post).toHaveBeenCalledWith("/api/hr/bonuses", body);
  });

  it("rejects with an optional note and deletes by id", () => {
    hr.rejectBonus(4);
    expect(apiMock.post).toHaveBeenLastCalledWith(
      "/api/hr/bonuses/4/reject",
      {},
    );
    hr.rejectBonus(4, "duplicate");
    expect(apiMock.post).toHaveBeenLastCalledWith("/api/hr/bonuses/4/reject", {
      note: "duplicate",
    });
    hr.deleteBonus(4);
    expect(apiMock.delete).toHaveBeenCalledWith("/api/hr/bonuses/4");
  });
});
