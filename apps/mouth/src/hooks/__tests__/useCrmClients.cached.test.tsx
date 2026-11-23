/**
 * The client list is the list of its filter, in every render.
 *
 * The hook used to keep the loaded pages in state and to empty that state in
 * an effect when a filter changed. Data served from the cache was copied in
 * and emptied in the same commit (a second visit to the desk within the stale
 * time showed no clients); the passport filter did not start a new list; and
 * between a filter change and the effect, the rows of the filter before were
 * still shown. The list is now worked out in the render from the filter it is
 * for. These tests hold every list the hook shows against the filter of that
 * same render.
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
type Filters = { status: string; passport_expiring_days?: number };
type Name = "active" | "inactive" | "passport" | "today" | "nobody" | "overlap";

/** The server's book: pages of two, by filter. After its pages a filter has no rows. */
const BOOK: Record<Name, { filters: Filters; pages: number[][] }> = {
  active: { filters: { status: "active" }, pages: [[1, 2], [3, 4], [5]] },
  inactive: {
    filters: { status: "inactive" },
    pages: [
      [11, 12],
      [13, 14],
    ],
  },
  passport: {
    filters: { status: "active", passport_expiring_days: 90 },
    pages: [[2, 4]],
  },
  today: {
    filters: { status: "active", passport_expiring_days: 0 },
    pages: [
      [21, 22],
      [23, 24],
    ],
  },
  nobody: { filters: { status: "closed" }, pages: [] },
  // A row that moved down while the user scrolled: it is on both pages.
  overlap: {
    filters: { status: "moving" },
    pages: [
      [31, 32],
      [32, 33],
    ],
  },
};
const NAMES = Object.keys(BOOK) as Name[];

const nameOf = (p: Asked): Name =>
  NAMES.find(
    (name) =>
      BOOK[name].filters.status === p.status &&
      BOOK[name].filters.passport_expiring_days === p.passport_expiring_days,
  ) as Name;
const pageOf = (name: Name, index: number) => BOOK[name].pages[index] ?? [];
const wholeBookOf = (name: Name) => [...new Set(BOOK[name].pages.flat())];
const keyOf = (name: Name, page: number) => `${name}@${page * 2}`;

const row = (id: number) => ({
  id,
  full_name: `Synthetic ${id}`,
  email: `c${id}@example.test`,
  status: "active",
});
const rows = (list: number[]) => list.map(row);
const ids = (list: Array<{ id: number }>) => list.map((c) => c.id);
const same = (a: number[], b: number[]) => a.join(",") === b.join(",");

/** A request the server has not answered yet. */
type Waiting = { key: string; answer: () => void; fail: () => void };
let waiting: Waiting[] = [];
/** When true the server answers only when a test tells it to. */
let serverWaits = false;

function theServer() {
  vi.mocked(api.crm.getClients).mockImplementation(((p: Asked) => {
    const name = nameOf(p);
    const page = (p.offset ?? 0) / 2;
    const list = rows(pageOf(name, page));
    if (!serverWaits) return Promise.resolve(list);
    return new Promise((resolve, reject) => {
      waiting.push({
        key: keyOf(name, page),
        answer: () => resolve(list),
        fail: () => reject(new Error("the server failed")),
      });
    });
  }) as never);
}

/** Every request made so far, as "filter@offset". */
const asked = () =>
  vi
    .mocked(api.crm.getClients)
    .mock.calls.map(([p]) =>
      keyOf(nameOf(p as Asked), ((p as Asked).offset ?? 0) / 2),
    );

/** Let one waiting request through (or make it fail), and let the hook take it in. */
async function theServerAnswers(
  key: string,
  how: "answer" | "fail" = "answer",
) {
  const index = waiting.findIndex((w) => w.key === key);
  expect(index, `nothing is waiting for ${key}`).toBeGreaterThan(-1);
  const [request] = waiting.splice(index, 1);
  await act(async () => request[how]());
}

const makeClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false } } });

const wrapperFor =
  (client: QueryClient) =>
  ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );

/** What one render of the hook showed, with the filter that render was for. */
type Seen = { name: Name; list: number[]; hasMore: boolean };

/** The hook as the desk uses it, with every render it has made, in order. */
function theDesk(client: QueryClient, first: Name) {
  const seen: Seen[] = [];
  const hook = renderHook(
    ({ name }: { name: Name }) => {
      const result = useCrmClients({ ...BOOK[name].filters, limit: 2 });
      seen.push({ name, list: ids(result.clients), hasMore: result.hasMore });
      return result;
    },
    { wrapper: wrapperFor(client), initialProps: { name: first } },
  );
  const now = () => hook.result.current;
  const list = () => ids(now().clients);
  return {
    seen,
    now,
    list,
    unmount: hook.unmount,
    shows: (expected: number[]) =>
      waitFor(() => expect(list()).toEqual(expected)),
    switchesTo: (name: Name) => hook.rerender({ name }),
    asksForMore: () => act(() => now().loadMore()),
  };
}

/** A list is a filter's own when it is that filter's pages from the first, whole and in order. */
const isOf = (name: Name, list: number[]) => {
  if (list.length === 0) return true;
  let upTo: number[] = [];
  return BOOK[name].pages.some((page) => {
    upTo = [...new Set([...upTo, ...page])];
    return same(upTo, list);
  });
};
/** Renders that showed something other than their own filter's pages. */
const rendersOfAnotherList = (seen: Seen[]) =>
  seen.filter((s) => !isOf(s.name, s.list));
/** Renders that said the list had ended while it had not shown the filter's whole book. */
const rendersThatEndedEarly = (seen: Seen[]) =>
  seen.filter((s) => !s.hasMore && !same(s.list, wholeBookOf(s.name)));

const heldToItsFilter = (seen: Seen[]) => {
  expect(rendersOfAnotherList(seen)).toEqual([]);
  expect(rendersThatEndedEarly(seen)).toEqual([]);
};

beforeEach(() => {
  vi.mocked(api.crm.getClients).mockReset();
  waiting = [];
  serverWaits = false;
  theServer();
});

describe("data that comes from the cache", () => {
  it("is shown in the first render on mount", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "active" }, 0],
      rows([1, 2]),
    );

    const desk = theDesk(client, "active");

    expect(desk.seen[0].list).toEqual([1, 2]);
    await desk.shows([1, 2]);
    expect(asked()).toEqual([]);
    heldToItsFilter(desk.seen);
  });

  it("is shown when the desk is opened again within the stale time", async () => {
    const client = makeClient();
    const first = theDesk(client, "active");
    await first.shows([1, 2]);
    first.asksForMore();
    await first.shows([1, 2, 3, 4]);
    first.unmount();

    const again = theDesk(client, "active");

    expect(again.seen[0].list).toEqual([1, 2]);
    expect(again.now().hasMore).toBe(true);
    again.asksForMore();
    await again.shows([1, 2, 3, 4]);
    // Both pages were still in the cache: nothing was asked a second time.
    expect(asked()).toEqual(["active@0", "active@2"]);
    heldToItsFilter(again.seen);
  });

  it("is shown in the first render after switching back to a filter already loaded", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.switchesTo("inactive");
    await desk.shows([11, 12]);
    const before = desk.seen.length;

    desk.switchesTo("active");

    expect(desk.seen[before].list).toEqual([1, 2]);
    await desk.shows([1, 2]);
    expect(asked()).toEqual(["active@0", "inactive@0"]);
    heldToItsFilter(desk.seen);
  });

  it("says there is no more when the cached page is a short one", async () => {
    const client = makeClient();
    client.setQueryData(["crm", "clients", { status: "active" }, 0], rows([1]));

    const desk = theDesk(client, "active");

    expect(desk.seen[0]).toEqual({ name: "active", list: [1], hasMore: false });
  });

  it("is followed by the next pages when more is asked for", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "active" }, 0],
      rows([1, 2]),
    );
    const desk = theDesk(client, "active");

    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4, 5]);

    expect(desk.now().hasMore).toBe(false);
    expect(asked()).toEqual(["active@2", "active@4"]);
    heldToItsFilter(desk.seen);
  });
});

describe("no render shows the rows of another filter", () => {
  it("while the filter switched to is still loading", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4, 5]);
    expect(desk.now().hasMore).toBe(false);
    serverWaits = true;
    const before = desk.seen.length;

    desk.switchesTo("inactive");

    // From the very render that sees the change: nothing, loading, and the
    // end of the old list is not the end of this one.
    expect(desk.seen[before]).toEqual({
      name: "inactive",
      list: [],
      hasMore: true,
    });
    expect(desk.now().isLoading).toBe(true);
    await theServerAnswers("inactive@0");
    await desk.shows([11, 12]);
    expect(desk.seen.slice(before).map((s) => s.list.join())).not.toContain(
      "1,2,3,4,5",
    );
    heldToItsFilter(desk.seen);
  });

  it("when both pages of the filter switched to are in the cache", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 0],
      rows([11, 12]),
    );
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 2],
      rows([13, 14]),
    );
    const desk = theDesk(client, "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    const before = desk.seen.length;

    desk.switchesTo("inactive");

    expect(desk.seen[before].list).toEqual([11, 12]);
    await desk.shows([11, 12]);
    expect(desk.now().hasMore).toBe(true);
    desk.asksForMore();
    await desk.shows([11, 12, 13, 14]);
    heldToItsFilter(desk.seen);
  });

  it("when a later page of the filter switched to is cached, empty, and its first page is still loading", async () => {
    const client = makeClient();
    client.setQueryData(["crm", "clients", { status: "inactive" }, 2], []);
    const desk = theDesk(client, "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    serverWaits = true;
    const before = desk.seen.length;

    desk.switchesTo("inactive");

    // The cached later page is not this list's page: it neither becomes the
    // list nor says the list has ended.
    await waitFor(() => expect(desk.now().isLoading).toBe(true));
    expect(desk.list()).toEqual([]);
    expect(desk.now().hasMore).toBe(true);
    expect(desk.seen.slice(before).every((s) => s.hasMore)).toBe(true);
    await theServerAnswers("inactive@0");
    await desk.shows([11, 12]);
    expect(desk.now().hasMore).toBe(true);
    heldToItsFilter(desk.seen);
  });

  it("when only a later page of the filter switched to is in the cache", async () => {
    const client = makeClient();
    client.setQueryData(
      ["crm", "clients", { status: "inactive" }, 2],
      rows([13, 14]),
    );
    const desk = theDesk(client, "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);

    desk.switchesTo("inactive");

    await desk.shows([11, 12]);
    desk.asksForMore();
    await desk.shows([11, 12, 13, 14]);
    // The first page was asked for; the page at the old offset was not.
    expect(asked()).toEqual(["active@0", "active@2", "inactive@0"]);
    heldToItsFilter(desk.seen);
  });

  it("when an answer for the filter before arrives after the switch", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    serverWaits = true;
    desk.asksForMore();
    await waitFor(() =>
      expect(waiting.map((w) => w.key)).toEqual(["active@2"]),
    );

    desk.switchesTo("inactive");
    await theServerAnswers("active@2");

    expect(desk.list()).toEqual([]);
    await theServerAnswers("inactive@0");
    await desk.shows([11, 12]);
    heldToItsFilter(desk.seen);
  });

  it("with no rows the list is empty and ended", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);

    desk.switchesTo("nobody");

    await waitFor(() => expect(desk.now().hasMore).toBe(false));
    expect(desk.list()).toEqual([]);
    heldToItsFilter(desk.seen);
  });

  it("coming back to a filter starts from its first page again", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);

    desk.switchesTo("inactive");
    await desk.shows([11, 12]);
    desk.switchesTo("active");

    await desk.shows([1, 2]);
    expect(desk.now().hasMore).toBe(true);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    heldToItsFilter(desk.seen);
  });
});

describe("the passport filter starts a new list like every other filter", () => {
  it("after more was loaded, the list is the filter's own first page", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);

    desk.switchesTo("passport");

    await desk.shows([2, 4]);
    expect(desk.now().hasMore).toBe(true);
    desk.asksForMore();
    await waitFor(() => expect(desk.now().hasMore).toBe(false));
    expect(desk.list()).toEqual([2, 4]);
    heldToItsFilter(desk.seen);
  });

  it("zero days is a filter too", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);

    desk.switchesTo("today");

    await desk.shows([21, 22]);
    desk.asksForMore();
    await desk.shows([21, 22, 23, 24]);
    heldToItsFilter(desk.seen);
  });

  it("taking the filter away brings the whole list back from its first page", async () => {
    const desk = theDesk(makeClient(), "today");
    await desk.shows([21, 22]);
    desk.asksForMore();
    await desk.shows([21, 22, 23, 24]);

    desk.switchesTo("active");

    await desk.shows([1, 2]);
    desk.asksForMore();
    await desk.shows([1, 2, 3, 4]);
    heldToItsFilter(desk.seen);
  });
});

describe("pages are added one at a time, each once", () => {
  it("asking twice at once asks for one page", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);

    act(() => {
      desk.now().loadMore();
      desk.now().loadMore();
    });

    await desk.shows([1, 2, 3, 4]);
    expect(asked()).toEqual(["active@0", "active@2"]);
  });

  it("asking again while a page loads asks for nothing, and says a page is loading", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    serverWaits = true;
    desk.asksForMore();
    await waitFor(() => expect(desk.now().isLoadingMore).toBe(true));
    expect(desk.now().isLoading).toBe(false);

    desk.asksForMore();
    desk.asksForMore();

    expect(asked()).toEqual(["active@0", "active@2"]);
    expect(desk.list()).toEqual([1, 2]);
    await theServerAnswers("active@2");
    await desk.shows([1, 2, 3, 4]);
    expect(desk.now().isLoadingMore).toBe(false);
    heldToItsFilter(desk.seen);
  });

  it("asking after the list has ended asks for nothing", async () => {
    const desk = theDesk(makeClient(), "passport");
    await desk.shows([2, 4]);
    desk.asksForMore();
    await waitFor(() => expect(desk.now().hasMore).toBe(false));

    desk.asksForMore();
    desk.asksForMore();

    expect(asked()).toEqual(["passport@0", "passport@2"]);
    expect(desk.list()).toEqual([2, 4]);
  });

  it("a page that failed is not skipped: the list keeps what it had and asks for nothing more", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    serverWaits = true;
    desk.asksForMore();
    await waitFor(() =>
      expect(waiting.map((w) => w.key)).toEqual(["active@2"]),
    );

    await theServerAnswers("active@2", "fail");

    await waitFor(() => expect(desk.now().isError).toBe(true));
    expect(desk.list()).toEqual([1, 2]);
    expect(desk.now().isLoadingMore).toBe(false);
    desk.asksForMore();
    desk.asksForMore();
    expect(asked()).toEqual(["active@0", "active@2"]);
    expect(waiting).toEqual([]);
    heldToItsFilter(desk.seen);
  });

  it("a row that is on two pages is shown once", async () => {
    const desk = theDesk(makeClient(), "overlap");
    await desk.shows([31, 32]);

    desk.asksForMore();

    await desk.shows([31, 32, 33]);
  });

  it("settles after a change of filter instead of rendering on and on", async () => {
    const desk = theDesk(makeClient(), "active");
    await desk.shows([1, 2]);
    desk.switchesTo("inactive");
    await desk.shows([11, 12]);
    const settled = desk.seen.length;

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(desk.seen.length).toBe(settled);
    expect(settled).toBeLessThan(12);
  });
});

/** A small seeded generator, so a failing day can be run again. */
function seeded(seed: number) {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const ANY_FILTER: Name[] = [
  "active",
  "inactive",
  "passport",
  "today",
  "nobody",
];

/**
 * One day at the desk, in random order: scrolling, changing filter, leaving
 * and coming back, with the server answering late and out of order. After
 * every step the list and its end are what a plain model of the desk says,
 * and every render made on the way showed only its own filter's pages.
 */
async function aDayAtTheDesk(seed: number, steps: number) {
  const random = seeded(seed);
  const pick = <T,>(list: T[]) => list[Math.floor(random() * list.length)];
  const client = makeClient();
  const cached = new Set<string>();
  let at = { name: pick(ANY_FILTER), page: 0 };
  const story = [`open ${at.name}`];
  const desks = [theDesk(client, at.name)];
  const desk = () => desks[desks.length - 1];
  const inTheCache = () => cached.has(keyOf(at.name, at.page));
  const expectedList = () => {
    const before = BOOK[at.name].pages.slice(0, at.page).flat();
    return inTheCache() ? [...before, ...pageOf(at.name, at.page)] : before;
  };
  const expectedMore = () =>
    inTheCache() ? pageOf(at.name, at.page).length === 2 : true;
  const holds = () =>
    waitFor(() => {
      expect(desk().list()).toEqual(expectedList());
      expect(desk().now().hasMore).toBe(expectedMore());
    });

  try {
    await holds();
    for (let step = 0; step < steps; step += 1) {
      const roll = random();
      if (roll < 0.35 && waiting.length > 0) {
        const request = pick(waiting);
        story.push(`answer ${request.key}`);
        cached.add(request.key);
        await theServerAnswers(request.key);
      } else if (roll < 0.65) {
        story.push("more");
        if (inTheCache() && pageOf(at.name, at.page).length === 2) {
          at = { name: at.name, page: at.page + 1 };
        }
        desk().asksForMore();
      } else if (roll < 0.9) {
        const name = pick(ANY_FILTER.filter((n) => n !== at.name));
        story.push(`switch to ${name}`);
        at = { name, page: 0 };
        desk().switchesTo(name);
      } else {
        story.push("leave and come back");
        desk().unmount();
        at = { name: at.name, page: 0 };
        desks.push(theDesk(client, at.name));
      }
      await holds();
    }
    for (const one of desks) heldToItsFilter(one.seen);
  } catch (error) {
    throw new Error(
      `seed ${seed}, after: ${story.join(" > ")}\n${(error as Error).message}`,
    );
  } finally {
    desk().unmount();
  }
}

describe("any order of scroll, filter change, late answer and return", () => {
  it.each([0, 1, 2, 3, 4, 5])(
    "holds the list to its filter (thirty days, batch %i)",
    async (batch) => {
      serverWaits = true;
      for (let day = 0; day < 30; day += 1) {
        waiting = [];
        await aDayAtTheDesk(batch * 1000 + day, 14);
      }
    },
    120_000,
  );
});
