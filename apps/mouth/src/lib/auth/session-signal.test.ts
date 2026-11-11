import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { ApiClientBase } from "@/lib/api/client";
import { PublicAuthClient } from "@/lib/api/public-auth";
import { getValidToken } from "@/lib/utils/token";
import {
  SESSION_CHANGED_EVENT,
  getSessionEpoch,
  notifySessionChanged,
} from "./session-signal";
import { tokenFor } from "./session-test-utils";

const PROFILE = { id: "1", email: "a@example.test" } as never;

describe("bz:session-changed signal and epoch (bearer token only)", () => {
  let fired: number;
  const onSignal = () => {
    fired += 1;
  };

  beforeEach(() => {
    localStorage.clear();
    fired = 0;
    window.addEventListener(SESSION_CHANGED_EVENT, onSignal);
  });
  afterEach(() => {
    window.removeEventListener(SESSION_CHANGED_EVENT, onSignal);
    vi.useRealTimers();
  });

  function expectOneSignalAndEpochBump(action: () => void) {
    const before = getSessionEpoch();
    action();
    expect(fired).toBe(1);
    expect(getSessionEpoch()).toBe(before + 1);
  }

  function expectNoSignalNoBump(action: () => void) {
    const before = getSessionEpoch();
    action();
    expect(fired).toBe(0);
    expect(getSessionEpoch()).toBe(before);
  }

  it("fires on ApiClient token set", () => {
    const client = new ApiClientBase("https://api.test");
    expectOneSignalAndEpochBump(() => client.setToken(tokenFor("1")));
  });

  it("does NOT fire on an ApiClient profile write", () => {
    const client = new ApiClientBase("https://api.test");
    expectNoSignalNoBump(() => client.setUserProfile(PROFILE));
  });

  it("fires once on ApiClient clearToken (token + profile removed together)", () => {
    const client = new ApiClientBase("https://api.test");
    client.setToken(tokenFor("1"));
    client.setUserProfile(PROFILE);
    fired = 0;
    expectOneSignalAndEpochBump(() => client.clearToken());
  });

  it("public auth client: token set and clear fire, a profile write does not", () => {
    const client = new PublicAuthClient();
    expectOneSignalAndEpochBump(() => client.setToken(tokenFor("1")));
    fired = 0;
    expectNoSignalNoBump(() => client.setUserProfile(PROFILE));
    expectOneSignalAndEpochBump(() => client.clearToken());
  });

  it("fires on the expiry removal inside getValidToken", () => {
    localStorage.setItem("auth_token", tokenFor("1", -3600));
    expectOneSignalAndEpochBump(() => {
      expect(getValidToken("auth_token", localStorage)).toBeNull();
    });
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("does not fire when getValidToken returns a valid token", () => {
    localStorage.setItem("auth_token", tokenFor("1"));
    expectNoSignalNoBump(() => {
      getValidToken("auth_token", localStorage);
    });
  });

  it("does not fire for the expiry removal of an unrelated key", () => {
    localStorage.setItem("other_token", tokenFor("1", -3600));
    expectNoSignalNoBump(() => {
      getValidToken("other_token", localStorage);
    });
  });

  it("fires once when ApiClient.getToken removes an expired token", () => {
    localStorage.setItem("auth_token", tokenFor("1", -3600));
    const client = new ApiClientBase("https://api.test");
    fired = 0;
    const before = getSessionEpoch();
    expect(client.getToken()).toBeNull();
    expect(fired).toBe(1);
    expect(getSessionEpoch()).toBe(before + 1);
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("bumps the epoch on a storage event for the token key or a clear(), not for others", () => {
    const before = getSessionEpoch();
    window.dispatchEvent(new StorageEvent("storage", { key: "theme" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(getSessionEpoch()).toBe(before);
    window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    expect(getSessionEpoch()).toBe(before + 1);
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    expect(getSessionEpoch()).toBe(before + 2);
  });

  it("notifySessionChanged alone bumps and dispatches", () => {
    expectOneSignalAndEpochBump(() => notifySessionChanged());
  });
});
