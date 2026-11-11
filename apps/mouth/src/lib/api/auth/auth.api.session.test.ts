// Deliberately imports no session module: it must run unchanged against the
// pre-fix tree, where the stale getProfile() write (race 2) is reproduced.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { AuthApi } from "./auth.api";
import { ApiClientBase } from "../client";

function jwt(sub: string): string {
  const body = btoa(
    JSON.stringify({ sub, exp: Math.floor(Date.now() / 1000) + 3600 }),
  )
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
  return `h.${body}.s`;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PROFILE_A = { id: "11", email: "a@example.test", name: "A" };

describe("AuthApi.getProfile against the session (real AuthApi + ApiClient)", () => {
  let client: ApiClientBase;
  let auth: AuthApi;

  beforeEach(() => {
    localStorage.clear();
    client = new ApiClientBase("https://api.test");
    auth = new AuthApi(client);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normal case: still writes the profile it received", async () => {
    client.setToken(jwt("11"));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(PROFILE_A));

    await expect(auth.getProfile()).resolves.toMatchObject({ id: "11" });
    expect(JSON.parse(localStorage.getItem("user_profile") ?? "null")).toEqual(
      PROFILE_A,
    );
  });

  it("race 2: a getProfile issued before logout and resolved after it does NOT restore the profile", async () => {
    client.setToken(jwt("11"));
    let resolveProfile!: (r: Response) => void;
    const pendingProfile = new Promise<Response>((r) => {
      resolveProfile = r;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/auth/profile")) return pendingProfile;
      if (url.endsWith("/api/auth/logout"))
        return new Response(null, { status: 204 });
      throw new Error(`unexpected ${url}`);
    });

    const profileCall = auth.getProfile(); // issued while A is signed in
    await Promise.resolve();
    await auth.logout(); // local state cleared at once
    expect(localStorage.getItem("user_profile")).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();

    resolveProfile(json(PROFILE_A)); // A's answer arrives after the logout
    await profileCall;

    expect(localStorage.getItem("user_profile")).toBeNull();
    expect(client.getUserProfile()).toBeNull();
  });

  it("a getProfile issued before a re-login as B does not overwrite B's profile with A's", async () => {
    client.setToken(jwt("11"));
    let resolveProfile!: (r: Response) => void;
    vi.spyOn(globalThis, "fetch").mockReturnValue(
      new Promise<Response>((r) => {
        resolveProfile = r;
      }),
    );
    const profileCall = auth.getProfile();
    await Promise.resolve();

    client.setToken(jwt("22"));
    client.setUserProfile({ id: "22", email: "b@example.test" } as never);

    resolveProfile(json(PROFILE_A));
    await profileCall;

    expect(JSON.parse(localStorage.getItem("user_profile") ?? "{}").id).toBe(
      "22",
    );
  });
});
