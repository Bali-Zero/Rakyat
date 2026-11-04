import { describe, it, expect, vi, beforeEach } from "vitest";

const mockCookieGet = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({
    get: mockCookieGet,
  })),
}));

import { toggleClockStatus } from "./actions";

function makeJwt(payload: Record<string, unknown>): string {
  const header = Buffer.from(
    JSON.stringify({ alg: "HS256", typ: "JWT" }),
  ).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${header}.${body}.sig`;
}

describe("chat actions toggleClockStatus", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = mockFetch;
  });

  it("posts user_id and email JSON body to /api/team/clock-in when currently clocked out", async () => {
    const token = makeJwt({ sub: "user-42", email: "zero@balizero.com" });
    mockCookieGet.mockReturnValue({ value: token });
    mockFetch.mockResolvedValueOnce({ ok: true });

    const res = await toggleClockStatus(false);

    expect(res).toEqual({ isClockIn: true });
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/team\/clock-in$/),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          user_id: "user-42",
          email: "zero@balizero.com",
        }),
      },
    );
  });

  it("posts user_id and email JSON body to /api/team/clock-out when currently clocked in", async () => {
    const token = makeJwt({ userId: 77, email: "team@balizero.com" });
    mockCookieGet.mockReturnValue({ value: token });
    mockFetch.mockResolvedValueOnce({ ok: true });

    const res = await toggleClockStatus(true);

    expect(res).toEqual({ isClockIn: false });
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/team\/clock-out$/),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          user_id: "77",
          email: "team@balizero.com",
        }),
      },
    );
  });

  it("fails closed when cookie token is missing or lacks actor identity claims", async () => {
    mockCookieGet.mockReturnValue(undefined);
    expect(await toggleClockStatus(false)).toEqual({
      isClockIn: false,
      error: "Not authenticated",
    });

    mockCookieGet.mockReturnValue({ value: "opaque-non-jwt" });
    expect(await toggleClockStatus(true)).toEqual({
      isClockIn: true,
      error: "Not authenticated",
    });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
