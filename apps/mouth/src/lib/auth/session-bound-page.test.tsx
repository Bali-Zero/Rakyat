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

import { ApiClientBase } from "@/lib/api/client";
import { acceptResponse, useSessionIdentity } from "./session-identity";
import { tokenFor } from "./session-test-utils";

// A pending fetch the test resolves by hand; `auth` records the bearer the
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

interface Card {
  team_member_id?: string;
  value: string;
}

// A page bound to the identity per SESSION.md S4, over the REAL ApiClient.
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
const settle = () => act(async () => {});

async function showCardFor(sub: string, token = tokenFor(sub)) {
  localStorage.setItem("auth_token", token);
  await waitFor(() => expect(pending).toHaveLength(1));
  expect(pending[0].auth).toBe(`Bearer ${token}`);
  await act(async () =>
    pending[0].resolve({ team_member_id: sub, value: `${sub}-pay` }),
  );
  expect(text()).toBe(`${sub}-pay`);
}

let client: ApiClientBase;
beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
  mockTransport();
  client = new ApiClientBase("https://api.test");
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("a page bound with useSessionIdentity + requestWithCredential + acceptResponse", () => {
  it("shows the card for the signed-in identity", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await showCardFor("A", localStorage.getItem("auth_token")!);
  });

  it("(1) another tab sets B: A's data is cleared on the event; B's response is accepted only once the page displays B", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await showCardFor("A", localStorage.getItem("auth_token")!);

    // Another tab writes B straight to storage; this tab has seen no event.
    const tokenB = tokenFor("B");
    localStorage.setItem("auth_token", tokenB);
    expect(text()).toBe("A-pay"); // nothing told this tab yet

    // A reload now sends B's bearer but the page still displays A.
    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].auth).toBe(`Bearer ${tokenB}`);
    await act(async () =>
      pending[1].resolve({ team_member_id: "B", value: "B-pay" }),
    );
    expect(text()).toBe("nothing"); // B's data is not shown under A

    // The delayed storage event arrives: A's data cleared, page re-binds to B.
    act(() => storageEvent("auth_token"));
    expect(text()).toBe("nothing");
    await waitFor(() => expect(pending).toHaveLength(3));
    expect(pending[2].auth).toBe(`Bearer ${tokenB}`);
    await act(async () =>
      pending[2].resolve({ team_member_id: "B", value: "B-pay" }),
    );
    expect(text()).toBe("B-pay");
  });

  it("(2) A -> B -> A in another tab, response handled BEFORE the events: B's response is rejected under A (round-1 repro)", async () => {
    const tokenA = tokenFor("A");
    const tokenB = tokenFor("B");
    localStorage.setItem("auth_token", tokenA);
    render(<BoundCard client={client} />);
    await showCardFor("A", tokenA);

    localStorage.setItem("auth_token", tokenB); // other tab sets B, no event
    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].auth).toBe(`Bearer ${tokenB}`); // B really left the tab

    localStorage.setItem("auth_token", tokenA); // other tab restores A, no event

    // B's answer, with NO owner field, is handled before the queued events.
    await act(async () => pending[1].resolve({ value: "B-pay" }));
    expect(text()).toBe("nothing");
    // ... and with an owner field naming B, too.
    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(3));
    await act(async () =>
      pending[2].resolve({ team_member_id: "B", value: "B-pay" }),
    );
    expect(text()).toBe("nothing");

    // The queued events finally arrive: the page re-binds to A and loads A.
    act(() => {
      storageEvent("auth_token");
      storageEvent("auth_token");
    });
    await waitFor(() => expect(pending.length).toBeGreaterThanOrEqual(4));
    const last = pending[pending.length - 1];
    expect(last.auth).toBe(`Bearer ${tokenA}`);
    await act(async () =>
      last.resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");
  });

  it("(3) same-tab logout clears the page at once, and a late response is dropped", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await showCardFor("A", localStorage.getItem("auth_token")!);

    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(2));

    act(() => client.clearToken());
    expect(text()).toBe("nothing");

    await act(async () =>
      pending[1].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("nothing");
    expect(pending).toHaveLength(2); // nothing refetched without an identity
  });

  it("(4) token expiry clears the page at expiry, with no removal and no signal", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    const token = tokenFor("A", 120);
    localStorage.setItem("auth_token", token);
    render(<BoundCard client={client} />);
    // waitFor() polls on timers, which are fake here: flush by hand.
    for (let i = 0; i < 5 && pending.length === 0; i++) await settle();
    expect(pending).toHaveLength(1);
    await act(async () =>
      pending[0].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("A-pay");

    await act(async () => {
      vi.advanceTimersByTime(200_000);
    });
    expect(text()).toBe("nothing");
    expect(localStorage.getItem("auth_token")).toBe(token); // nobody removed it
    expect(pending).toHaveLength(1);
  });

  it("(5) a response whose request went out cookie-only (no bearer) is rejected", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await showCardFor("A", localStorage.getItem("auth_token")!);

    // Another tab logged out; this tab has seen no event. The next request
    // leaves with no bearer and the cookie (possibly someone else's) decides.
    localStorage.removeItem("auth_token");
    fireEvent.click(screen.getByText("reload"));
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].auth).toBeUndefined();
    await act(async () =>
      pending[1].resolve({ team_member_id: "A", value: "A-pay" }),
    );
    expect(text()).toBe("nothing");
  });

  it("(6) a user_profile write does not reload the bound page", async () => {
    localStorage.setItem("auth_token", tokenFor("A"));
    render(<BoundCard client={client} />);
    await showCardFor("A", localStorage.getItem("auth_token")!);

    act(() =>
      client.setUserProfile({ id: "A", email: "a@example.test" } as never),
    );
    await settle();
    await settle();
    expect(pending).toHaveLength(1);
    expect(text()).toBe("A-pay");

    // A storage event for the profile key from another tab does not either.
    act(() => storageEvent("user_profile"));
    await settle();
    expect(pending).toHaveLength(1);
    expect(text()).toBe("A-pay");
  });
});
