/**
 * The client list shows the rows of its filter.
 *
 * The hook keeps the pages it has loaded in state, beside the query that
 * fetches them. Three ways the two used to disagree: data served from the
 * cache was copied in and emptied again in the same commit (a second visit
 * to the desk within the stale time showed no clients); the passport filter
 * did not start a new list, so its pages were laid on the list of the filter
 * before; and a list must never be only a later page of its filter.
 */
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useCrmClients } from "../useCrmClients";

vi.mock("@/lib/api", () => ({
  api: { crm: { getClients: vi.fn() } },
}));

type Asked = {
  status?: string;
  passport_expiring_days?: number;
  offset?: number;
};

const makeClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false } } });

const wrapperFor =
  (client: QueryClient) =>
  ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );

const row = (id: number) => ({
  id,
  full_name: `Synthetic ${id}`,
  email: `c${id}@example.test`,
  status: "active",
});

const rows = (...list: number[]) => list.map(row);
const ids = (list: Array<{ id: number }>) => list.map((c) => c.id);

/** The server: pages of two, by filter. A filter it does not know has no rows. */
const PAGES: Record<string, number[][]> = {
  active: [[1, 2], [3, 4], [5]],
  inactive: [
    [11, 12],
    [13, 14],
  ],
  "active+passport90": [[2, 4]],
  "active+passport0": [
    [21, 22],
    [23, 24],
  ],
};

const filterOf = (p: Asked) =>
  `${p.status ?? ""}${p.passport_expiring_days === undefined ? "" : `+passport${p.passport_expiring_days}`}`;

function theServerAnswers() {
  vi.mocked(api.crm.getClients).mockImplementation((async (p: Asked) =>
    rows(...(PAGES[filterOf(p)]?.[(p.offset ?? 0) / 2] ?? []))) as never);
}

/** Every request made so far, as "filter@offset". */
const asked = () =>
  vi
    .mocked(api.crm.getClients)
    .mock.calls.map(([p]) => `${filterOf(p as Asked)}@${(p as Asked).offset}`);

type Filters = { status: string; passport_expiring_days?: number };

/** The hook as the desk uses it, with every list it has shown, in order. */
function theDesk(client: QueryClient, initial: Filters) {
  const shown: number[][] = [];
  const hook = renderHook(
    (filters: Filters) => {
      const result = useCrmClients({ ...filters, limit: 2 });
      shown.push(ids(result.clients));
      return result;
    },
    { wrapper: wrapperFor(client), initialProps: initial },
  );
  const list = () => ids(hook.result.current.clients);
  const settlesOn = (expected: number[]) =>
    waitFor(() => expect(list()).toEqual(expected));
  const loadsMore = async (expected: number[]) => {
    act(() => hook.result.current.loadMore());
    await settlesOn(expected);
  };
  return { ...hook, shown, list, settlesOn, loadsMore };
}

/** The lists among `shown` that are none of the `allowed` ones. */
const otherThan = (shown: number[][], ...allowed: number[][]) => {
  const ok = new Set(allowed.map((list) => list.join(",")));
  return shown.filter((list) => !ok.has(list.join(",")));
};

beforeEach(() => {
  vi.mocked(api.crm.getClients).mockReset();
  theServerAnswers();
});

describe("data that comes from the cache", () => {
  it("is shown on mount", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "active" }, 0],
      rows(1, 2),
    );

    const desk = theDesk(client, { status: "active" });

    await desk.settlesOn([1, 2]);
    expect(asked()).toEqual([]);
  });

  it("is shown when the desk is opened again within the stale time", async () => {
    const client = makeClient();
    const first = theDesk(client, { status: "active" });
    await first.settlesOn([1, 2]);
    await first.loadsMore([1, 2, 3, 4]);
    first.unmount();

    const again = theDesk(client, { status: "active" });

    await again.settlesOn([1, 2]);
    expect(again.result.current.hasMore).toBe(true);
    await again.loadsMore([1, 2, 3, 4]);
    // Both pages were still in the cache: nothing was asked a second time.
    expect(asked()).toEqual(["active@0", "active@2"]);
  });

  it("is shown when switching back to a filter already loaded", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);

    desk.rerender({ status: "inactive" });
    await desk.settlesOn([11, 12]);
    desk.rerender({ status: "active" });

    await desk.settlesOn([1, 2]);
    expect(asked()).toEqual(["active@0", "inactive@0"]);
  });

  it("says there is no more when the cached page is a short one", async () => {
    const client = makeClient();
    client.setQueryData(["crm", "clients", { status: "active" }, 0], rows(1));

    const desk = theDesk(client, { status: "active" });

    await desk.settlesOn([1]);
    expect(desk.result.current.hasMore).toBe(false);
  });

  it("is followed by the next page when more is asked for", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "active" }, 0],
      rows(1, 2),
    );
    const desk = theDesk(client, { status: "active" });
    await desk.settlesOn([1, 2]);

    await desk.loadsMore([1, 2, 3, 4]);
    await desk.loadsMore([1, 2, 3, 4, 5]);

    expect(desk.result.current.hasMore).toBe(false);
    expect(asked()).toEqual(["active@2", "active@4"]);
  });
});

describe("a later page is never the list", () => {
  it("when both pages of the filter switched to are in the cache", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 0],
      rows(11, 12),
    );
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 2],
      rows(13, 14),
    );
    const desk = theDesk(client, { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);
    desk.shown.length = 0;

    desk.rerender({ status: "inactive" });

    await desk.settlesOn([11, 12]);
    // The old list for the one render the change is noticed in, then
    // nothing, then the first page: never a later page, alone or laid on
    // the old rows.
    expect(otherThan(desk.shown, [1, 2, 3, 4], [], [11, 12])).toEqual([]);
    expect(desk.result.current.hasMore).toBe(true);
    await desk.loadsMore([11, 12, 13, 14]);
  });

  it("when only the later page of the filter switched to is in the cache", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 2],
      rows(13, 14),
    );
    const desk = theDesk(client, { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);
    desk.shown.length = 0;

    desk.rerender({ status: "inactive" });

    await desk.settlesOn([11, 12]);
    expect(otherThan(desk.shown, [1, 2, 3, 4], [], [11, 12])).toEqual([]);
    await desk.loadsMore([11, 12, 13, 14]);
  });
});

describe("a filter that is not in the cache", () => {
  it("replaces the list, and its pages are added without repeats", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);

    desk.rerender({ status: "inactive" });

    await desk.settlesOn([11, 12]);
    await desk.loadsMore([11, 12, 13, 14]);
  });

  it("shows no rows while it loads, not the rows of the filter before", async () => {
    let answer: (list: ReturnType<typeof rows>) => void = () => {};
    vi.mocked(api.crm.getClients).mockImplementation((async (p: Asked) =>
      p.status === "inactive"
        ? new Promise((resolve) => {
            answer = resolve;
          })
        : rows(...(PAGES[filterOf(p)]?.[(p.offset ?? 0) / 2] ?? []))) as never);
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);
    await desk.loadsMore([1, 2, 3, 4, 5]);
    expect(desk.result.current.hasMore).toBe(false);

    desk.rerender({ status: "inactive" });

    await desk.settlesOn([]);
    expect(desk.result.current.isLoading).toBe(true);
    // The end of the old list is not the end of this one.
    expect(desk.result.current.hasMore).toBe(true);
    await act(async () => answer(rows(11, 12)));
    await desk.settlesOn([11, 12]);
  });

  it("with no rows empties the list", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);

    desk.rerender({ status: "closed" });

    await desk.settlesOn([]);
    await waitFor(() => expect(desk.result.current.hasMore).toBe(false));
  });
});

describe("the passport filter starts a new list like every other filter", () => {
  it("after more was loaded, the list is the filter's own first page", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);

    desk.rerender({ status: "active", passport_expiring_days: 90 });

    await desk.settlesOn([2, 4]);
    expect(desk.result.current.hasMore).toBe(true);
    await desk.loadsMore([2, 4]);
    expect(desk.result.current.hasMore).toBe(false);
  });

  it("zero days is a filter too", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);

    desk.rerender({ status: "active", passport_expiring_days: 0 });

    await desk.settlesOn([21, 22]);
    await desk.loadsMore([21, 22, 23, 24]);
  });

  it("taking the filter away brings the whole list back from its first page", async () => {
    const desk = theDesk(makeClient(), {
      status: "active",
      passport_expiring_days: 0,
    });
    await desk.settlesOn([21, 22]);
    await desk.loadsMore([21, 22, 23, 24]);

    desk.rerender({ status: "active" });

    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);
  });

  it("no list shown after the change mixes the rows of the two filters", async () => {
    const desk = theDesk(makeClient(), { status: "active" });
    await desk.settlesOn([1, 2]);
    await desk.loadsMore([1, 2, 3, 4]);

    desk.rerender({ status: "active", passport_expiring_days: 0 });
    await desk.settlesOn([21, 22]);
    await desk.loadsMore([21, 22, 23, 24]);

    const old = new Set([1, 2, 3, 4]);
    const mixed = desk.shown.filter(
      (list) => list.some((id) => old.has(id)) && list.some((id) => id > 20),
    );
    expect(mixed).toEqual([]);
  });
});
