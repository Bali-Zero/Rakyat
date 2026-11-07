import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/dynamic", () => ({ default: () => () => null }));
vi.mock("@/lib/analytics", () => ({ trackKBLIChatQuestion: vi.fn() }));

import {
  CHAT_SESSION_KEY,
  CHAT_SESSION_TTL_MS,
  readStoredSession,
  writeStoredSession,
} from "./ZantaraChat";

describe("kbli-chat-session storage", () => {
  beforeEach(() => localStorage.clear());

  it("round-trips an id that has not expired", () => {
    writeStoredSession("abc", 1_000);
    expect(readStoredSession(1_000 + CHAT_SESSION_TTL_MS - 1)).toBe("abc");
  });

  it("drops an expired id", () => {
    writeStoredSession("abc", 1_000);
    expect(readStoredSession(1_000 + CHAT_SESSION_TTL_MS)).toBeNull();
  });

  it("drops the legacy plain-string entry", () => {
    localStorage.setItem(CHAT_SESSION_KEY, "legacy-uuid-without-expiry");
    expect(readStoredSession()).toBeNull();
  });

  it("drops a corrupt entry", () => {
    localStorage.setItem(CHAT_SESSION_KEY, '{"id":42}');
    expect(readStoredSession()).toBeNull();
  });

  it("stores the expiry next to the id", () => {
    writeStoredSession("abc", 5);
    expect(JSON.parse(localStorage.getItem(CHAT_SESSION_KEY)!)).toEqual({
      id: "abc",
      expiresAt: 5 + CHAT_SESSION_TTL_MS,
    });
  });
});
