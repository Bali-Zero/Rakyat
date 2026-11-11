import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { ApiClientBase } from "@/lib/api/client";
import {
  acceptResponse,
  readSessionIdentity,
  subscribeSessionChange,
  useSessionIdentity,
} from "./session-identity";
import { getSessionEpoch, notifySessionChanged } from "./session-signal";
import { makeJwt, tokenFor } from "./session-test-utils";

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("readSessionIdentity", () => {
  it("returns the sub and the current epoch of a valid token", () => {
    localStorage.setItem("auth_token", tokenFor("42"));
    expect(readSessionIdentity()).toEqual({
      sub: "42",
      epoch: getSessionEpoch(),
    });
  });

  it("is null when there is no token", () => {
    expect(readSessionIdentity()).toBeNull();
  });

  it("is null for an expired token and does not remove it (pure read)", () => {
    const expired = tokenFor("42", -3600);
    localStorage.setItem("auth_token", expired);
    expect(readSessionIdentity()).toBeNull();
    expect(localStorage.getItem("auth_token")).toBe(expired);
  });

  it.each([
    ["not a jwt", "garbage"],
    ["bad base64", "a.%%%.c"],
    ["payload is not json", `a.${btoa("nope")}.c`],
    ["no sub", makeJwt({ exp: 9999999999 })],
    ["numeric sub", makeJwt({ sub: 42, exp: 9999999999 })],
    ["empty sub", makeJwt({ sub: "", exp: 9999999999 })],
  ])("is null for a malformed token (%s) and never throws", (_n, token) => {
    localStorage.setItem("auth_token", token);
    expect(() => readSessionIdentity()).not.toThrow();
    expect(readSessionIdentity()).toBeNull();
  });

  it("ignores user_profile entirely", () => {
    localStorage.setItem("user_profile", JSON.stringify({ id: "99" }));
    expect(readSessionIdentity()).toBeNull();
  });
});

describe("subscribeSessionChange", () => {
  it("fires on the same-tab signal (token set through the client)", () => {
    const cb = vi.fn();
    const off = subscribeSessionChange(cb);
    new ApiClientBase("https://api.test").setToken(tokenFor("1"));
    expect(cb).toHaveBeenCalledTimes(1);
    off();
  });

  it("fires on a storage event for the token key and clear(), not for other keys", () => {
    const cb = vi.fn();
    const off = subscribeSessionChange(cb);
    window.dispatchEvent(new StorageEvent("storage", { key: "theme" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(cb).not.toHaveBeenCalled();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    expect(cb).toHaveBeenCalledTimes(1);
    cb.mockClear();
    window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    expect(cb).toHaveBeenCalledTimes(1);
    off();
  });

  it("re-checks on focus and when the tab becomes visible, and stops after unsubscribe", () => {
    const cb = vi.fn();
    const off = subscribeSessionChange(cb);
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(cb).toHaveBeenCalledTimes(2);
    off();
    window.dispatchEvent(new Event("focus"));
    new ApiClientBase("https://api.test").clearToken();
    expect(cb).toHaveBeenCalledTimes(2);
  });
});

describe("acceptResponse", () => {
  const sentA = { sentSub: "A" };

  function signIn(sub: string) {
    localStorage.setItem("auth_token", tokenFor(sub));
  }

  it("accepts a bearer-sent response whose sub is the stored sub and the displayed identity", () => {
    signIn("A");
    expect(acceptResponse(sentA, readSessionIdentity())).toBe(true);
  });

  it("(a) refuses a response that was sent without a bearer", () => {
    signIn("A");
    expect(acceptResponse({ sentSub: null }, readSessionIdentity())).toBe(
      false,
    );
  });

  it("(b) refuses when storage now holds another sub or no valid token, without any event", () => {
    signIn("A");
    const displayed = readSessionIdentity();
    signIn("B");
    expect(acceptResponse(sentA, displayed)).toBe(false);
    localStorage.removeItem("auth_token");
    expect(acceptResponse(sentA, displayed)).toBe(false);
    localStorage.setItem("auth_token", tokenFor("A", -10));
    expect(acceptResponse(sentA, displayed)).toBe(false);
  });

  it("(c) refuses a response naming another owner, accepts the same owner as string or number", () => {
    signIn("1");
    const displayed = readSessionIdentity();
    expect(acceptResponse({ sentSub: "1" }, displayed, "1")).toBe(true);
    expect(acceptResponse({ sentSub: "1" }, displayed, 1)).toBe(true);
    expect(acceptResponse({ sentSub: "1" }, displayed, "2")).toBe(false);
  });

  it("(d) refuses when the displayed identity is missing, another sub, or an older epoch", () => {
    signIn("A");
    const displayed = readSessionIdentity()!;
    expect(acceptResponse(sentA, null)).toBe(false);
    expect(acceptResponse(sentA, { ...displayed, sub: "B" })).toBe(false);
    notifySessionChanged();
    expect(acceptResponse(sentA, displayed)).toBe(false);
  });
});

describe("useSessionIdentity", () => {
  it("follows set, change and removal of the token", async () => {
    const client = new ApiClientBase("https://api.test");
    const { result } = renderHook(() => useSessionIdentity());
    expect(result.current).toBeNull();
    act(() => client.setToken(tokenFor("1")));
    expect(result.current?.sub).toBe("1");
    act(() => client.setToken(tokenFor("2")));
    expect(result.current?.sub).toBe("2");
    act(() => client.clearToken());
    expect(result.current).toBeNull();
  });

  it("drops the identity by itself when the token reaches its expiry", () => {
    vi.useFakeTimers();
    localStorage.setItem("auth_token", tokenFor("1", 120));
    const { result } = renderHook(() => useSessionIdentity());
    expect(result.current?.sub).toBe("1");
    act(() => {
      vi.advanceTimersByTime(200_000);
    });
    expect(result.current).toBeNull();
  });
});

describe("useSessionIdentity and the profile", () => {
  it("a profile write does not change or re-create the identity", () => {
    const client = new ApiClientBase("https://api.test");
    client.setToken(tokenFor("1"));
    const { result } = renderHook(() => useSessionIdentity());
    const before = result.current;
    expect(before?.sub).toBe("1");
    act(() =>
      client.setUserProfile({ id: "1", email: "a@example.test" } as never),
    );
    expect(result.current).toBe(before);
  });
});
