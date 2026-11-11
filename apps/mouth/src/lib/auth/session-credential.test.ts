import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { AuthApi } from "@/lib/api/auth/auth.api";
import { ApiClientBase } from "@/lib/api/client";
import { PublicAuthClient } from "@/lib/api/public-auth";
import {
  enforceProfileInvariant,
  installProfileInvariantEnforcer,
} from "./session-credential";
import { SESSION_CHANGED_EVENT } from "./session-signal";
import { tokenFor } from "./session-test-utils";

const profile = (id: string) => ({ id, email: `${id}@example.test`, name: id });
const storedProfileId = () => {
  const raw = localStorage.getItem("user_profile");
  return raw ? (JSON.parse(raw) as { id: string }).id : null;
};

let signals: string[];
const onSignal = (e: Event) => {
  signals.push((e as CustomEvent<{ kind: string }>).detail.kind);
};

beforeEach(() => {
  localStorage.clear();
  signals = [];
  installProfileInvariantEnforcer();
  window.addEventListener(SESSION_CHANGED_EVENT, onSignal);
});
afterEach(() => {
  window.removeEventListener(SESSION_CHANGED_EVENT, onSignal);
  vi.restoreAllMocks();
});

describe("profile invariant enforcer", () => {
  it("removes a profile that contradicts the valid token, on a storage event", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(storedProfileId()).toBeNull();
  });

  it("acts on focus and on visibility->visible, not on hidden", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(storedProfileId()).toBe("A");
    visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(storedProfileId()).toBeNull();

    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new Event("focus"));
    expect(storedProfileId()).toBeNull();
  });

  it("removes a profile when there is no valid token (absent or expired), on token-class triggers", () => {
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new Event("focus"));
    expect(storedProfileId()).toBeNull();

    localStorage.setItem("auth_token", tokenFor("A", -10));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new StorageEvent("storage", { key: "auth_token" }));
    expect(storedProfileId()).toBeNull();
  });

  it("removal signals once and cannot loop; with nothing to remove it is silent", () => {
    localStorage.setItem("auth_token", tokenFor("B"));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    expect(enforceProfileInvariant()).toBe(true);
    expect(signals).toEqual(["enforced"]);
    expect(enforceProfileInvariant()).toBe(false);
    window.dispatchEvent(new Event("focus"));
    expect(signals).toEqual(["enforced"]);
  });

  it("keeps a profile that matches, and one under a valid token whose sub cannot be decoded", () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new Event("focus"));
    expect(storedProfileId()).toBe("A");

    localStorage.setItem("auth_token", "opaque-token");
    window.dispatchEvent(new Event("focus"));
    expect(storedProfileId()).toBe("A");
  });

  it("drops the client's in-memory profile together with the stored one", () => {
    const client = new ApiClientBase("https://api.test");
    client.setToken(tokenFor("B"));
    client.setUserProfile(profile("B") as never);
    // another tab's stale write of A's profile
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(client.getUserProfile()).toBeNull();
  });
});

function loginResponse(sub: string): Response {
  return new Response(
    JSON.stringify({
      success: true,
      message: "ok",
      data: {
        token: tokenFor(sub),
        token_type: "Bearer",
        expiresIn: 3600,
        user: profile(sub),
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("login order: token first, then profile (AuthApi.login / verifyMagicLink / public auth)", () => {
  it("login A, login B over A, logout, stale write from another tab", async () => {
    const client = new ApiClientBase("https://api.test");
    const auth = new AuthApi(client);
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    fetchSpy.mockResolvedValueOnce(loginResponse("A"));
    await auth.login("a@example.test", "1234");
    expect(storedProfileId()).toBe("A");
    expect(client.getUserProfile()?.id).toBe("A");

    // B over A: the token write removes A's profile, the profile write keeps B's.
    fetchSpy.mockResolvedValueOnce(loginResponse("B"));
    await auth.login("b@example.test", "1234");
    expect(storedProfileId()).toBe("B");
    expect(client.getUserProfile()?.id).toBe("B");

    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await auth.logout();
    expect(storedProfileId()).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();

    // stale write from another tab after the logout, then after a new login
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(storedProfileId()).toBeNull();

    fetchSpy.mockResolvedValueOnce(loginResponse("B"));
    await auth.login("b@example.test", "1234");
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    window.dispatchEvent(new StorageEvent("storage", { key: "user_profile" }));
    expect(storedProfileId()).toBeNull();
    expect(localStorage.getItem("auth_token")).not.toBeNull();
  });

  it("magic link: token then profile, the new profile survives", async () => {
    const client = new ApiClientBase("https://api.test");
    localStorage.setItem("auth_token", tokenFor("A"));
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(loginResponse("C"));
    await new AuthApi(client).verifyMagicLink("tok");
    expect(storedProfileId()).toBe("C");
  });

  it("public auth client: token then profile, the new profile survives", () => {
    const client = new PublicAuthClient();
    localStorage.setItem("user_profile", JSON.stringify(profile("A")));
    client.setToken(tokenFor("B")); // A's profile contradicts B: removed here
    expect(storedProfileId()).toBeNull();
    client.setUserProfile(profile("B") as never);
    expect(storedProfileId()).toBe("B");
  });

  it("profile first, then token (tolerated): the profile survives the token write that matches it", () => {
    const client = new ApiClientBase("https://api.test");
    client.setUserProfile(profile("B") as never); // no token yet: tolerated
    expect(storedProfileId()).toBe("B");
    client.setToken(tokenFor("B"));
    expect(storedProfileId()).toBe("B");
  });
});
