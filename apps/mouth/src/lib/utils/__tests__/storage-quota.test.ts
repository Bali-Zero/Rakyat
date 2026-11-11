import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { AuthApi } from "@/lib/api/auth/auth.api";
import { ApiClientBase } from "@/lib/api/client";
import { TeamApi } from "@/lib/api/team/team.api";
import { subscribeSessionChange } from "@/lib/auth/session-identity";
import { tokenFor } from "@/lib/auth/session-test-utils";
import { safeStorage } from "../storage";

// A localStorage whose probe passed at module load but which REJECTS writes
// afterwards (quota). Reads and, optionally, removals keep working.
const real = window.localStorage;
let rejectWrites: boolean;
let rejectRemovals: boolean;

function installQuotaStorage() {
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => real.getItem(k),
    setItem: (k: string, v: string) => {
      if (rejectWrites) throw new DOMException("quota", "QuotaExceededError");
      real.setItem(k, v);
    },
    removeItem: (k: string) => {
      if (rejectRemovals) throw new DOMException("denied", "SecurityError");
      real.removeItem(k);
    },
    clear: () => real.clear(),
    key: (i: number) => real.key(i),
    get length() {
      return real.length;
    },
  });
}

const storageEvent = (key: string | null, newValue: string | null = null) =>
  window.dispatchEvent(new StorageEvent("storage", { key, newValue }));

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  real.clear();
  safeStorage._resetForTesting();
  rejectWrites = false;
  rejectRemovals = false;
  installQuotaStorage();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  real.clear();
  safeStorage._resetForTesting();
});

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("safeStorage when localStorage rejects a write after the probe", () => {
  it("login still works: profile readable, isAdmin true, clockIn proceeds", async () => {
    const client = new ApiClientBase("https://api.test");
    const auth = new AuthApi(client);
    const user = { id: "A", email: "a@example.test", name: "A", role: "admin" };
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    rejectWrites = true; // quota exhausted before the login lands
    fetchSpy.mockResolvedValueOnce(
      ok({
        success: true,
        message: "ok",
        data: {
          token: tokenFor("A"),
          token_type: "Bearer",
          expiresIn: 3600,
          user,
        },
      }),
    );
    await auth.login("a@example.test", "1234");

    expect(client.getUserProfile()).toEqual(user);
    expect(client.isAdmin()).toBe(true);
    expect(client.getToken()).not.toBeNull();

    fetchSpy.mockResolvedValueOnce(ok({ success: true }));
    await expect(new TeamApi(client).clockIn()).resolves.toBeDefined();
    const [url, init] = fetchSpy.mock.calls[1] as [string, { body: string }];
    expect(url).toContain("/api/team/clock-in");
    expect(JSON.parse(init.body)).toEqual({ user_id: "A", email: user.email });
  });

  it("A's token in localStorage, B's write fails: reads return B and A is gone from shared storage", () => {
    const tokenA = tokenFor("A");
    const tokenB = tokenFor("B");
    expect(safeStorage.setItem("auth_token", tokenA)).toBe(true);
    rejectWrites = true;

    expect(safeStorage.setItem("auth_token", tokenB)).toBe(false);

    expect(safeStorage.getItem("auth_token")).toBe(tokenB);
    expect(real.getItem("auth_token")).toBeNull();
  });

  it("if removing the older value fails too, reads still prefer the fallback for that key", () => {
    const tokenA = tokenFor("A");
    const tokenB = tokenFor("B");
    safeStorage.setItem("auth_token", tokenA);
    rejectWrites = true;
    rejectRemovals = true;

    safeStorage.setItem("auth_token", tokenB);

    expect(real.getItem("auth_token")).toBe(tokenA); // could not be removed
    expect(safeStorage.getItem("auth_token")).toBe(tokenB);
  });

  it("a later successful write puts the value back in localStorage and drops the fallback", () => {
    rejectWrites = true;
    safeStorage.setItem("k", "fallback");
    rejectWrites = false;
    expect(safeStorage.setItem("k", "shared")).toBe(true);
    expect(real.getItem("k")).toBe("shared");
    rejectWrites = true; // even if writes break again, "shared" is not shadowed by an old copy
    expect(safeStorage.getItem("k")).toBe("shared");
  });

  it("removeItem clears both places", () => {
    safeStorage.setItem("k", "old");
    rejectWrites = true;
    safeStorage.setItem("k", "new");
    safeStorage.removeItem("k");
    expect(safeStorage.getItem("k")).toBeNull();
    expect(real.getItem("k")).toBeNull();
  });

  it("clear() drops fallback-held keys too", () => {
    rejectWrites = true;
    safeStorage.setItem("k", "v");
    safeStorage.clear();
    expect(safeStorage.getItem("k")).toBeNull();
  });
});

describe("another tab settles a fallback-held key through its storage event", () => {
  it("newValue non-null: the shared value wins", () => {
    rejectWrites = true;
    safeStorage.setItem("auth_token", "mine");
    real.setItem("auth_token", "theirs"); // the other tab wrote a newer value
    expect(safeStorage.getItem("auth_token")).toBe("mine"); // event not delivered yet (S7)

    storageEvent("auth_token", "theirs");

    expect(safeStorage.getItem("auth_token")).toBe("theirs");
  });

  it("newValue null: the removal wins (another tab logged out), even when our older value could not be removed", () => {
    safeStorage.setItem("auth_token", "older");
    rejectWrites = true;
    rejectRemovals = true;
    safeStorage.setItem("auth_token", "mine");
    expect(safeStorage.getItem("auth_token")).toBe("mine");

    real.removeItem("auth_token"); // the other tab's logout
    storageEvent("auth_token", null);

    expect(safeStorage.getItem("auth_token")).toBeNull();
  });

  it("storage.clear() in another tab (null key) drops every fallback copy", () => {
    rejectWrites = true;
    safeStorage.setItem("a", "1");
    safeStorage.setItem("b", "2");
    storageEvent(null);
    expect(safeStorage.getItem("a")).toBeNull();
    expect(safeStorage.getItem("b")).toBeNull();
  });

  it("an event for a key held in localStorage, or an unrelated key, changes nothing", () => {
    rejectWrites = true;
    safeStorage.setItem("held", "v");
    storageEvent("something-else", "x");
    expect(safeStorage.getItem("held")).toBe("v");
  });

  it("ordering: a session listener that reads storage during the event already sees the settled value", () => {
    rejectWrites = true;
    safeStorage.setItem("auth_token", "mine");
    real.removeItem("auth_token");
    const seen: Array<string | null> = [];
    const off = subscribeSessionChange(() =>
      seen.push(safeStorage.getItem("auth_token")),
    );

    storageEvent("auth_token", null);

    off();
    expect(seen).toEqual([null]);
  });
});
