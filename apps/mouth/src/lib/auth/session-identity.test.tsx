import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { ApiClientBase } from "@/lib/api/client";
import { AuthApi } from "@/lib/api/auth/auth.api";
import { getValidToken } from "@/lib/utils/token";
import {
  acceptForIdentity,
  readSessionIdentity,
  subscribeSessionChange,
  useSessionIdentity,
} from "./session-identity";
import { getSessionEpoch } from "./session-signal";
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

  it("fires on a storage event for the token key, not for other keys", () => {
    const cb = vi.fn();
    const off = subscribeSessionChange(cb);
    window.dispatchEvent(new StorageEvent("storage", { key: "theme" }));
    expect(cb).not.toHaveBeenCalled();
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

describe("acceptForIdentity", () => {
  const a = { sub: "1", epoch: 5 };
  it("needs both identities, equal sub AND epoch", () => {
    expect(acceptForIdentity(a, a)).toBe(true);
    expect(acceptForIdentity(a, null)).toBe(false);
    expect(acceptForIdentity(null, a)).toBe(false);
    expect(acceptForIdentity(a, { sub: "1", epoch: 6 })).toBe(false);
    expect(acceptForIdentity(a, { sub: "2", epoch: 5 })).toBe(false);
  });
  it("requires a named owner to be the sub", () => {
    expect(acceptForIdentity(a, a, "1")).toBe(true);
    expect(acceptForIdentity(a, a, 1)).toBe(true);
    expect(acceptForIdentity(a, a, "2")).toBe(false);
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

// ---------------------------------------------------------------------------
// A page bound to the identity, following SESSION.md S4. `load` stands for the
// page's own-data request; the response names its owner (team_member_id).
// ---------------------------------------------------------------------------
interface Card {
  team_member_id: string;
  value: string;
}

function BoundCard({ load }: { load: () => Promise<Card> }) {
  const identity = useSessionIdentity();
  const [card, setCard] = useState<Card | null>(null);

  useEffect(() => {
    setCard(null); // identity changed or disappeared: clear at once
    if (!identity) return; // nobody signed in: issue nothing
    let live = true;
    load().then((res) => {
      if (!live) return;
      if (
        acceptForIdentity(identity, readSessionIdentity(), res.team_member_id)
      ) {
        setCard(res);
      }
    });
    return () => {
      live = false;
    };
  }, [identity, load]);

  return <div data-testid="card">{card ? card.value : "nothing"}</div>;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("a page bound to the identity", () => {
  it("race 1: A's card shown, token A expires, a response owned by B arrives -> nothing shown", async () => {
    localStorage.setItem("auth_token", tokenFor("A", 3600));
    const first = deferred<Card>();
    const second = deferred<Card>();
    const load = vi
      .fn<() => Promise<Card>>()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<BoundCard load={load} />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    await act(async () =>
      first.resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(screen.getByTestId("card").textContent).toBe("A-pay");

    // A expires and is removed alone; the cookie meanwhile belongs to B.
    localStorage.setItem("auth_token", tokenFor("A", -10));
    act(() => {
      getValidToken("auth_token", localStorage);
    });
    expect(screen.getByTestId("card").textContent).toBe("nothing");
    expect(load).toHaveBeenCalledTimes(1); // no refetch through the cookie
    await act(async () =>
      second.resolve({ team_member_id: "B", value: "B-pay" }),
    );
    expect(screen.getByTestId("card").textContent).toBe("nothing");
  });

  it("race 1b: a late response for A after the removal is dropped", async () => {
    localStorage.setItem("auth_token", tokenFor("A", 3600));
    const late = deferred<Card>();
    const load = vi.fn<() => Promise<Card>>().mockReturnValue(late.promise);
    render(<BoundCard load={load} />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));

    localStorage.setItem("auth_token", tokenFor("A", -10));
    act(() => {
      getValidToken("auth_token", localStorage);
    });
    await act(async () =>
      late.resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(screen.getByTestId("card").textContent).toBe("nothing");
  });

  it("a response whose owner is not the signed-in sub is refused even with a live identity", async () => {
    localStorage.setItem("auth_token", tokenFor("A", 3600));
    const load = vi
      .fn<() => Promise<Card>>()
      .mockResolvedValue({ team_member_id: "B", value: "B-pay" });
    render(<BoundCard load={load} />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.getByTestId("card").textContent).toBe("nothing");
  });

  it("race 2: logout pending + stale getProfile response -> nothing shown, no refetch", async () => {
    const client = new ApiClientBase("https://api.test");
    const auth = new AuthApi(client);
    client.setToken(tokenFor("A", 3600));
    const profileGate = deferred<Response>();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/auth/profile")) return profileGate.promise;
      return new Response(null, { status: 204 });
    });
    const load = vi
      .fn<() => Promise<Card>>()
      .mockResolvedValue({ team_member_id: "A", value: "A-pay" });
    render(<BoundCard load={load} />);
    await waitFor(() =>
      expect(screen.getByTestId("card").textContent).toBe("A-pay"),
    );
    expect(load).toHaveBeenCalledTimes(1);

    const profileCall = auth.getProfile(); // like the HR layout on mount
    await act(async () => {
      await auth.logout();
    });
    expect(screen.getByTestId("card").textContent).toBe("nothing");

    await act(async () => {
      profileGate.resolve(
        new Response(JSON.stringify({ id: "A", email: "a@example.test" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
      await profileCall;
    });
    expect(localStorage.getItem("user_profile")).toBeNull();
    expect(screen.getByTestId("card").textContent).toBe("nothing");
    expect(load).toHaveBeenCalledTimes(1); // the cookie is not an identity
  });
});
