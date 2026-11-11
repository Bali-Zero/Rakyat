import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { logger } from "@/lib/logger";
import { tokenFor } from "@/lib/auth/session-test-utils";
import { ApiClientBase } from "./client";

// SESSION.md S8, "One copy": the profile in storage is the only copy.
const ADMIN_A = {
  id: "A",
  email: "a@example.test",
  name: "A",
  role: "admin",
};
const KEY = "user_profile";

function fireEveryRecheckEvent() {
  window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
  window.dispatchEvent(new Event("focus"));
  const visibility = vi
    .spyOn(document, "visibilityState", "get")
    .mockReturnValue("visible");
  document.dispatchEvent(new Event("visibilitychange"));
  visibility.mockRestore();
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ApiClientBase keeps no copy of the profile", () => {
  it("has no cached profile field", () => {
    const client = new ApiClientBase("https://api.test");
    client.setUserProfile(ADMIN_A as never);
    expect("userProfile" in client).toBe(false);
  });

  it("gate repro: cached A, current token B, profile removed by another tab, then the events -> no profile, not admin, no admin headers", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    localStorage.setItem(KEY, JSON.stringify(ADMIN_A));
    const client = new ApiClientBase("https://api.test"); // sees A at construction
    expect(client.isAdmin()).toBe(true);

    localStorage.removeItem(KEY); // another tab; no event delivered here

    fireEveryRecheckEvent();

    expect(client.getUserProfile()).toBeNull();
    expect(client.isAdmin()).toBe(false);
    expect(client.isBoard()).toBe(false);
    expect(() => client.getAdminHeaders()).toThrow("Admin access required");
  });

  it("isAdmin() goes true -> false the moment the stored profile disappears, with no event at all", () => {
    const client = new ApiClientBase("https://api.test");
    client.setUserProfile(ADMIN_A as never);
    expect(client.isAdmin()).toBe(true);
    expect(client.getAdminHeaders()).toEqual({ "X-User-Email": ADMIN_A.email });

    localStorage.removeItem(KEY);

    expect(client.isAdmin()).toBe(false);
    expect(client.getUserProfile()).toBeNull();
  });

  it("every read goes to storage now: a stored profile written elsewhere is seen at once", () => {
    const client = new ApiClientBase("https://api.test");
    expect(client.getUserProfile()).toBeNull();
    localStorage.setItem(KEY, JSON.stringify(ADMIN_A));
    expect(client.getUserProfile()).toEqual(ADMIN_A);
  });

  it("absent or unparseable is no profile, and nothing is logged about it", () => {
    const client = new ApiClientBase("https://api.test");
    for (const raw of ["not-json", "null", '"str"']) {
      localStorage.setItem(KEY, raw);
      expect(client.getUserProfile()).toBeNull();
    }
    expect(logger.debug).not.toHaveBeenCalled();
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("clearToken removes the stored profile and nothing remains readable", () => {
    const client = new ApiClientBase("https://api.test");
    client.setUserProfile(ADMIN_A as never);
    client.clearToken();
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(client.getUserProfile()).toBeNull();
  });
});

describe("two views of storage converge (stale renderer cache)", () => {
  // The tab's view still serves A while the shared store has none. The tab's
  // OWN writes update its own view; a delivered event refreshes it.
  function installStaleView() {
    let stale = true;
    const real = window.localStorage;
    vi.stubGlobal("localStorage", {
      getItem: (key: string) =>
        key === KEY && stale ? JSON.stringify(ADMIN_A) : real.getItem(key),
      setItem: (key: string, value: string) => real.setItem(key, value),
      removeItem: (key: string) => {
        if (key === KEY) stale = false;
        real.removeItem(key);
      },
      clear: () => real.clear(),
      key: (i: number) => real.key(i),
      get length() {
        return real.length;
      },
    });
    return { real, refresh: () => (stale = false) };
  }

  it("event delivery refreshes the view: both the stored profile and getUserProfile() converge to none", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    const client = new ApiClientBase("https://api.test");
    const view = installStaleView();

    // Before the event the stale view is what this tab can see (S7 residual).
    expect(client.getUserProfile()).toEqual(ADMIN_A);

    view.refresh(); // the event and the refreshed cache arrive together
    fireEveryRecheckEvent();

    expect(view.real.getItem(KEY)).toBeNull();
    expect(client.getUserProfile()).toBeNull();
    expect(client.isAdmin()).toBe(false);
  });

  it("with the view still stale at the event, the enforcer removes A (token is B) and both converge", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    const client = new ApiClientBase("https://api.test");
    const view = installStaleView();

    fireEveryRecheckEvent();

    expect(view.real.getItem(KEY)).toBeNull();
    expect(client.getUserProfile()).toBeNull();
  });
});
