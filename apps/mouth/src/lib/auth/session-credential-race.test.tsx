import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { AuthApi, StaleSessionError } from "@/lib/api/auth/auth.api";
import { ApiClientBase } from "@/lib/api/client";
import { getValidToken } from "@/lib/utils/token";
import {
  acceptResponse,
  readSessionIdentity,
  useSessionIdentity,
} from "./session-identity";
import { tokenFor } from "./session-test-utils";

// A pending fetch the test resolves by hand; `auths` records the bearer each
// request really carried.
interface Pending {
  auth: string | undefined;
  resolve: (body: unknown) => void;
}
let pending: Pending[];

function mockTransport() {
  pending = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => {
    const headers = (init as { headers: Record<string, string> }).headers;
    return new Promise<Response>((resolve) => {
      pending.push({
        auth: headers["Authorization"],
        resolve: (body) =>
          resolve(
            new Response(JSON.stringify(body), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          ),
      });
    });
  });
}

const flush = () => act(async () => {});

interface Card {
  team_member_id?: string;
  value: string;
}

// A page bound to the identity per SESSION.md S4, over the REAL transport.
function BoundCard({ client }: { client: ApiClientBase }) {
  const identity = useSessionIdentity();
  const [tick, setTick] = useState(0);
  const [card, setCard] = useState<Card | null>(null);

  useEffect(() => {
    setCard(null);
    if (!identity) return;
    let live = true;
    client
      .requestWithCredential<Card>("/api/hr/me/card")
      .then((res) => {
        if (!live) return;
        if (acceptResponse(res, identity, res.data.team_member_id)) {
          setCard(res.data);
        }
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [identity, tick, client]);

  return (
    <div>
      <div data-testid="card">{card ? card.value : "nothing"}</div>
      <button onClick={() => setTick((t) => t + 1)}>reload</button>
    </div>
  );
}

const text = () => screen.getByTestId("card").textContent;
const storageEvent = (key: string) =>
  window.dispatchEvent(new StorageEvent("storage", { key }));

let client: ApiClientBase;
beforeEach(() => {
  localStorage.clear();
  mockTransport();
  client = new ApiClientBase("https://api.test");
});
afterEach(() => vi.restoreAllMocks());

describe("bound page, real transport", () => {
  it("shows a card for the signed-in identity", async () => {
    const tokenA = tokenFor("A");
    localStorage.setItem("auth_token", tokenA);
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].auth).toBe(`Bearer ${tokenA}`);
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");
  });

  it("race 1: A shown, a reload is in flight, A expires and is removed alone, B's response ARRIVES -> nothing shown", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");

    fireEvent.click(screen.getByText("reload")); // request #2, bearer A
    await waitFor(() => expect(pending).toHaveLength(2));

    localStorage.setItem("auth_token", tokenFor("A", -10)); // A expires
    act(() => {
      getValidToken("auth_token", localStorage); // removed alone, signal fires
    });
    expect(text()).toBe("nothing");

    // The cookie belongs to B: the in-flight answer is B's.
    await act(async () =>
      pending[1].resolve({ team_member_id: "B", value: "B-pay" }),
    );
    expect(text()).toBe("nothing");
    expect(pending).toHaveLength(2); // no refetch through the cookie
  });

  it("race 1b: a late response for A after the removal is dropped", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    localStorage.setItem("auth_token", tokenFor("A", -10));
    act(() => {
      getValidToken("auth_token", localStorage);
    });
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("nothing");
  });

  it("BLOCKER 1: another tab swaps B in and back with its storage events delayed -> B's data is never shown under A", async () => {
    const tokenA = tokenFor("A");
    const tokenB = tokenFor("B");
    localStorage.setItem("auth_token", tokenA);
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");

    // Another tab sets B; this tab has seen NO event (they are queued).
    localStorage.setItem("auth_token", tokenB);
    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].auth).toBe(`Bearer ${tokenB}`); // B really left the tab

    // The other tab restores A, still no event delivered here.
    localStorage.setItem("auth_token", tokenA);
    expect(readSessionIdentity()?.sub).toBe("A");

    // B's answer, with NO owner field, is handled before the queued events.
    await act(async () => pending[1].resolve({ value: "B-pay" }));
    expect(text()).toBe("nothing");

    // The delayed events finally arrive: the page re-binds to A and loads A.
    act(() => {
      storageEvent("auth_token");
      storageEvent("auth_token");
    });
    await waitFor(() => expect(pending).toHaveLength(3));
    expect(pending[2].auth).toBe(`Bearer ${tokenA}`);
    await act(async () =>
      pending[2].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");
  });

  it("race 2: logout pending + stale getProfile response -> nothing shown, no refetch", async () => {
    const auth = new AuthApi(client);
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");

    const profileCall = auth.getProfile().catch((e) => e);
    await waitFor(() => expect(pending).toHaveLength(2)); // profile
    let logoutDone!: Promise<void>;
    await act(async () => {
      logoutDone = auth.logout();
      await Promise.resolve();
    });
    expect(text()).toBe("nothing");
    await waitFor(() => expect(pending).toHaveLength(3)); // logout request
    await act(async () => {
      pending[1].resolve({ id: "A", email: "a@example.test" });
      pending[2].resolve({});
      await profileCall;
      await logoutDone;
    });
    expect(localStorage.getItem("user_profile")).toBeNull();
    expect(text()).toBe("nothing");
    expect(pending).toHaveLength(3);
  });

  it("bound page: a load path that does not write the profile reloads once per profile write, then settles", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );

    act(() =>
      client.setUserProfile({ id: "A", email: "a@example.test" } as never),
    );
    await waitFor(() => expect(pending).toHaveLength(2)); // epoch moved: one reload
    await act(async () =>
      pending[1].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    await flush();
    await flush();
    expect(pending).toHaveLength(2); // and it settles: no loop
    expect(text()).toBe("A-pay");
  });
});

describe("getProfile against delayed cross-tab storage events", () => {
  const PROFILE_A = { id: "A", email: "a@example.test", name: "A" };

  it("BLOCKER 2: another tab removes token+profile mid-flight, response handled before the storage events -> not written, StaleSessionError", async () => {
    const auth = new AuthApi(client);
    client.setToken(tokenFor("A"));
    client.setUserProfile(PROFILE_A as never);

    const call = auth.getProfile().catch((e) => e);
    await waitFor(() => expect(pending).toHaveLength(1));

    // Other tab logs out: storage changes, this tab's events are still queued.
    localStorage.removeItem("auth_token");
    localStorage.removeItem("user_profile");

    pending[0].resolve(PROFILE_A);
    const result = await call;

    expect(result).toBeInstanceOf(StaleSessionError);
    expect(localStorage.getItem("user_profile")).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();
    storageEvent("auth_token"); // delayed events change nothing now
    expect(localStorage.getItem("user_profile")).toBeNull();
  });

  it("another tab swaps B in and A back: B's profile is neither written nor returned; A's stored profile is returned", async () => {
    const auth = new AuthApi(client);
    const tokenA = tokenFor("A");
    client.setToken(tokenA);
    client.setUserProfile(PROFILE_A as never);

    localStorage.setItem("auth_token", tokenFor("B")); // other tab, no event
    const call = auth.getProfile();
    await waitFor(() => expect(pending).toHaveLength(1));
    localStorage.setItem("auth_token", tokenA); // restored, no event

    pending[0].resolve({ id: "B", email: "b@example.test" });
    const result = await call;

    expect(result).toEqual(PROFILE_A);
    expect(JSON.parse(localStorage.getItem("user_profile") ?? "{}").id).toBe(
      "A",
    );
  });

  it("same token still stored: profile is written and returned", async () => {
    const auth = new AuthApi(client);
    client.setToken(tokenFor("A"));
    const call = auth.getProfile();
    await waitFor(() => expect(pending).toHaveLength(1));
    pending[0].resolve(PROFILE_A);
    await expect(call).resolves.toEqual(PROFILE_A);
    expect(JSON.parse(localStorage.getItem("user_profile") ?? "{}").id).toBe(
      "A",
    );
  });

  it("cookie-only request: response returned, never written", async () => {
    const auth = new AuthApi(client);
    const call = auth.getProfile();
    await waitFor(() => expect(pending).toHaveLength(1));
    pending[0].resolve(PROFILE_A);
    await expect(call).resolves.toEqual(PROFILE_A);
    expect(localStorage.getItem("user_profile")).toBeNull();
  });
});
