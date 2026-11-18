import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
  act,
} from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { DoneTodaySection } from "../_components/DoneTodaySection";
import {
  accountingApi,
  type DoneTodayItem,
  type DoneTodayResponse,
} from "@/lib/api/workspace/accounting.api";
import { logger } from "@/lib/logger";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));
vi.mock("@/lib/api/workspace/accounting.api", () => ({
  accountingApi: {
    getDoneToday: vi.fn(),
    getSummary: vi.fn(),
    getCashout: vi.fn(),
    getBankTransactions: vi.fn(),
    getReconciliationInbox: vi.fn(),
    linkRecorded: vi.fn(),
    ignoreTransaction: vi.fn(),
    reopenTransaction: vi.fn(),
    getMatchCandidates: vi.fn(),
    confirm: vi.fn(),
    exportSheet: vi.fn(),
    importCashout: vi.fn(),
    previewBankStatement: vi.fn(),
    importBankStatement: vi.fn(),
  },
}));

const api = vi.mocked(accountingApi);
const log = vi.mocked(logger.error);
const source = readFileSync(
  join(
    process.cwd(),
    "src/app/(workspace)/accounting/_components/DoneTodaySection.tsx",
  ),
  "utf8",
);
const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

const item = (over: Partial<DoneTodayItem> = {}): DoneTodayItem => ({
  txn_id: 1,
  action: "confirmed",
  done_at: "2026-11-18T11:15:42+08:00",
  txn_date: "2026-11-17",
  amount_idr: 4000000,
  description: "TRF PT CONTOH SATU",
  invoice_number: "INV-000000-00001",
  client_name: "PT CONTOH SATU",
  ...over,
});
const body = (
  items: DoneTodayItem[] = [item()],
  over: Partial<DoneTodayResponse> = {},
): DoneTodayResponse => ({
  day: "2026-11-18",
  timezone: "Asia/Makassar",
  counts: {
    confirmed: items.length,
    linked: 0,
    ignored: 0,
    total: items.length,
  },
  items,
  truncated: false,
  ...over,
});
const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};
const notFound = () =>
  Object.assign(new Error("Not Found"), { statusCode: 404 });
const header = () => screen.getByRole("button", { name: /^done today/i });
const open = () => fireEvent.click(header());
const loaded = async (total: number) =>
  waitFor(() => expect(header()).toHaveTextContent(`Done today · ${total}`));

beforeEach(() => {
  vi.clearAllMocks();
  api.getDoneToday.mockResolvedValue(body());
});

describe("DoneTodaySection", () => {
  it("F1: mount sends exactly one load and no other request", async () => {
    render(<DoneTodaySection version={0} />);
    await loaded(1);
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    open();
    open();
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
  });

  it("F2/F3: collapsed on mount, the count shows after the load, the list only when open", async () => {
    render(<DoneTodaySection version={0} />);
    expect(header()).toHaveAttribute("aria-expanded", "false");
    await loaded(1);
    expect(screen.queryByTestId("done-today-item-1")).toBeNull();
    expect(screen.queryByText(/^Today, /)).toBeNull();
    open();
    expect(header()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("done-today-item-1")).toBeInTheDocument();
    expect(screen.getByText("Today, 2026-11-18 (WITA)")).toBeInTheDocument();
    open();
    expect(screen.queryByTestId("done-today-item-1")).toBeNull();
  });

  it("F4: entries keep their order and show time, label, memo, date, amount, invoice and client", async () => {
    api.getDoneToday.mockResolvedValue(
      body([
        item({
          txn_id: 3,
          done_at: "2026-11-18T23:45:10+08:00",
          client_name: "PT KLIEN DUA",
        }),
        item({
          txn_id: 2,
          action: "linked",
          description: "TRF LINK",
          invoice_number: "   ",
          client_name: "  ",
        }),
        item({
          txn_id: 1,
          action: "ignored",
          description: "TRF IGNORE",
          invoice_number: "INV-IGNORED",
          client_name: "PT IGNORED",
        }),
      ]),
    );
    render(<DoneTodaySection version={0} />);
    await loaded(3);
    open();
    const entries = screen.getAllByTestId(/^done-today-item-/);
    expect(entries.map((e) => e.dataset.testid)).toEqual([
      "done-today-item-3",
      "done-today-item-2",
      "done-today-item-1",
    ]);
    const first = within(entries[0]);
    expect(first.getByText("Confirmed")).toBeInTheDocument();
    expect(entries[0].textContent).toContain("23:45 · Confirmed");
    expect(first.getByText("TRF PT CONTOH SATU")).toBeInTheDocument();
    expect(first.getByText(/2026-11-17/)).toBeInTheDocument();
    expect(first.getByText(/4\.000\.000|4,000,000/)).toBeInTheDocument();
    expect(first.getByText(/INV-000000-00001/)).toBeInTheDocument();
    expect(first.getByText(/PT KLIEN DUA$/)).toBeInTheDocument();
    const second = within(entries[1]);
    expect(second.getByText("Linked")).toBeInTheDocument();
    expect(second.getByText("No invoice number")).toBeInTheDocument();
    expect(entries[1].textContent).not.toMatch(/·\s*$/m);
    const third = within(entries[2]);
    expect(third.getByText("Ignored")).toBeInTheDocument();
    expect(entries[2].textContent).not.toContain("INV-IGNORED");
    expect(entries[2].textContent).not.toContain("PT IGNORED");
    expect(entries[2].textContent).not.toContain("No invoice number");
  });

  it.each([
    "weird",
    "toString",
    "constructor",
    "__proto__",
    "valueOf",
    "hasOwnProperty",
  ])(
    "F4: the unknown action %s reads Done and shows no invoice or client line",
    async (action) => {
      api.getDoneToday.mockResolvedValue(
        body([
          item({
            txn_id: 5,
            action: action as never,
            description: "TRF MEMO SATU",
          }),
        ]),
      );
      render(<DoneTodaySection version={0} />);
      await loaded(1);
      open();
      const entry = screen.getByTestId("done-today-item-5");
      expect(within(entry).getByText("Done")).toBeInTheDocument();
      expect(entry.textContent).toContain("11:15 · Done · 2026-11-17");
      expect(entry.textContent).not.toContain("INV-000000-00001");
      expect(entry.textContent).not.toContain("No invoice number");
      expect(entry.textContent).not.toContain("PT CONTOH SATU");
    },
  );

  it.each([
    "yesterday",
    "T10:20",
    "18/11/2026T10:20",
    "2026-11-18 10:20:00+08:00",
    "x2026-11-18T10:20",
    "2026-11-18T1:20",
    "2026-11-18T10:2",
    "2026-11-18T100:20",
    "2026-111-18T10:20",
    "12026-11-18T10:20",
  ])("F4: done_at %j has no pattern, so no time shows", async (doneAt) => {
    api.getDoneToday.mockResolvedValue(
      body([item({ txn_id: 5, done_at: doneAt, description: "" })]),
    );
    render(<DoneTodaySection version={0} />);
    await loaded(1);
    open();
    const entry = screen.getByTestId("done-today-item-5");
    expect(within(entry).getByText("(no memo)")).toBeInTheDocument();
    expect(entry.textContent).not.toContain(":");
  });

  it("F4: confirmed and linked entries show their populated invoice number and client name", async () => {
    api.getDoneToday.mockResolvedValue(
      body([
        item({
          txn_id: 2,
          action: "linked",
          description: "TRF LINK",
          invoice_number: "INV-000000-00042",
          client_name: "PT KLIEN TIGA",
        }),
        item({
          txn_id: 1,
          action: "confirmed",
          description: "TRF CONFIRM",
          invoice_number: "INV-000000-00007",
          client_name: "PT KLIEN EMPAT",
        }),
      ]),
    );
    render(<DoneTodaySection version={0} />);
    await loaded(2);
    open();
    const linked = screen.getByTestId("done-today-item-2");
    expect(linked.textContent).toContain("INV-000000-00042 · PT KLIEN TIGA");
    expect(linked.textContent).not.toContain("No invoice number");
    const confirmed = screen.getByTestId("done-today-item-1");
    expect(confirmed.textContent).toContain(
      "INV-000000-00007 · PT KLIEN EMPAT",
    );
  });

  it("F3: the loading, empty and truncated texts", async () => {
    const d = deferred<DoneTodayResponse>();
    api.getDoneToday.mockReturnValueOnce(d.promise);
    render(<DoneTodaySection version={0} />);
    open();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await act(async () => d.resolve(body([])));
    expect(screen.getByText("Nothing done yet today.")).toBeInTheDocument();
    expect(screen.queryByText(/^Showing the latest/)).toBeNull();
  });

  it("F3: a truncated body says how many of how many", async () => {
    api.getDoneToday.mockResolvedValue(
      body([item({ txn_id: 1 }), item({ txn_id: 2 })], {
        truncated: true,
        counts: { confirmed: 7, linked: 0, ignored: 0, total: 7 },
      }),
    );
    render(<DoneTodaySection version={0} />);
    await loaded(7);
    open();
    expect(screen.getByText("Showing the latest 2 of 7.")).toBeInTheDocument();
  });

  it("F3/F6: a failed load shows the failure text and Try again sends exactly one load", async () => {
    api.getDoneToday.mockRejectedValueOnce(new Error("boom SENTINEL"));
    render(<DoneTodaySection version={0} />);
    open();
    expect(
      await screen.findByText("Could not load what was done today."),
    ).toBeInTheDocument();
    expect(header()).toHaveTextContent(/^Done today$/);
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
    const retry = deferred<DoneTodayResponse>();
    api.getDoneToday.mockReturnValueOnce(retry.promise);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(
      screen.queryByText("Could not load what was done today."),
    ).toBeNull();
    await act(async () => retry.resolve(body()));
    await loaded(1);
    expect(api.getDoneToday).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Today, 2026-11-18 (WITA)")).toBeInTheDocument();
  });

  it("F6: each failed load logs once with a fixed message and the error; no toast, no alert", async () => {
    const err = new Error("boom");
    api.getDoneToday.mockRejectedValueOnce(err);
    render(<DoneTodaySection version={0} />);
    open();
    await screen.findByText("Could not load what was done today.");
    expect(log).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith(
      "accounting done-today load failed",
      {},
      err,
    );
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("F1: a version change sends exactly one load", async () => {
    const { rerender } = render(<DoneTodaySection version={0} />);
    await loaded(1);
    rerender(<DoneTodaySection version={1} />);
    await waitFor(() => expect(api.getDoneToday).toHaveBeenCalledTimes(2));
    rerender(<DoneTodaySection version={1} />);
    expect(api.getDoneToday).toHaveBeenCalledTimes(2);
  });

  it("an older answer arriving after a newer one never replaces the data", async () => {
    const first = deferred<DoneTodayResponse>();
    const second = deferred<DoneTodayResponse>();
    api.getDoneToday
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { rerender } = render(<DoneTodaySection version={0} />);
    rerender(<DoneTodaySection version={1} />);
    await act(async () => second.resolve(body([item({ txn_id: 2 })])));
    await loaded(1);
    await act(async () =>
      first.resolve(body([item({ txn_id: 8 }), item({ txn_id: 9 })])),
    );
    expect(header()).toHaveTextContent("Done today · 1");
    open();
    expect(screen.getByTestId("done-today-item-2")).toBeInTheDocument();
    expect(screen.queryByTestId("done-today-item-8")).toBeNull();
  });

  it("an older failure arriving after a newer success changes nothing", async () => {
    const first = deferred<DoneTodayResponse>();
    api.getDoneToday
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(body());
    const { rerender } = render(<DoneTodaySection version={0} />);
    rerender(<DoneTodaySection version={1} />);
    await loaded(1);
    open();
    await act(async () => first.reject(notFound()));
    expect(screen.getByTestId("done-today-item-1")).toBeInTheDocument();
  });

  it("the data stays on screen during a reload and after a failed one", async () => {
    const { rerender } = render(<DoneTodaySection version={0} />);
    await loaded(1);
    open();
    const reload = deferred<DoneTodayResponse>();
    api.getDoneToday.mockReturnValueOnce(reload.promise);
    rerender(<DoneTodaySection version={1} />);
    await waitFor(() => expect(api.getDoneToday).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.getByTestId("done-today-item-1")).toBeInTheDocument();
    await act(async () => reload.reject(new Error("boom")));
    expect(
      screen.queryByText("Could not load what was done today."),
    ).toBeNull();
    expect(screen.getByTestId("done-today-item-1")).toBeInTheDocument();
    expect(header()).toHaveTextContent("Done today · 1");
  });

  it("F5: a 404 renders nothing and no later version loads again", async () => {
    api.getDoneToday.mockRejectedValue(notFound());
    const { container, rerender } = render(<DoneTodaySection version={0} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    rerender(<DoneTodaySection version={1} />);
    rerender(<DoneTodaySection version={2} />);
    expect(container).toBeEmptyDOMElement();
    expect(api.getDoneToday).toHaveBeenCalledTimes(1);
  });

  it("F5: a 404 on a reload removes data already shown", async () => {
    const { container, rerender } = render(<DoneTodaySection version={0} />);
    await loaded(1);
    api.getDoneToday.mockRejectedValueOnce(notFound());
    rerender(<DoneTodaySection version={1} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("F3: a body whose items is not an array, or whose total is not a number, is a failed load", async () => {
    api.getDoneToday.mockResolvedValueOnce({ ...body(), items: "x" } as never);
    const { rerender } = render(<DoneTodaySection version={0} />);
    open();
    await screen.findByText("Could not load what was done today.");
    api.getDoneToday.mockResolvedValueOnce({
      ...body(),
      counts: { total: "3" },
    } as never);
    rerender(<DoneTodaySection version={1} />);
    await waitFor(() => expect(api.getDoneToday).toHaveBeenCalledTimes(2));
    await screen.findByText("Could not load what was done today.");
    expect(header()).toHaveTextContent(/^Done today$/);
  });

  it("F7: no write method is called and no control is disabled", async () => {
    render(<DoneTodaySection version={0} />);
    await loaded(1);
    open();
    for (const [name, fn] of Object.entries(api)) {
      if (name !== "getDoneToday") expect(fn, name).not.toHaveBeenCalled();
    }
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    for (const b of screen.getAllByRole("button")) expect(b).toBeEnabled();
    expect(source).not.toContain("useWriteLock");
    expect(source).not.toContain("useToast");
  });

  it("F8: action labels use the state colours the spec names", async () => {
    api.getDoneToday.mockResolvedValue(
      body([
        item({ txn_id: 1, action: "confirmed" }),
        item({ txn_id: 2, action: "linked" }),
        item({ txn_id: 3, action: "ignored" }),
      ]),
    );
    render(<DoneTodaySection version={0} />);
    await loaded(3);
    open();
    expect(screen.getByText("Confirmed").className).toContain(
      "text-[var(--state-success)]",
    );
    expect(screen.getByText("Linked").className).toContain(
      "text-[var(--state-info)]",
    );
    expect(screen.getByText("Ignored").className).toContain(
      "text-[var(--bz-text-2)]",
    );
  });

  it("F8: every variable the source uses is allowed and declared in the theme; no family utility", () => {
    const allowed = new Set([
      "--bz-card",
      "--bz-card-hover",
      "--bz-text-1",
      "--bz-text-2",
      "--bz-border",
      "--state-success",
      "--state-info",
      "--state-danger",
    ]);
    const used = Array.from(source.matchAll(/var\((--[\w-]+)\)/g)).map(
      (m) => m[1],
    );
    expect(used.length).toBeGreaterThan(0);
    for (const name of used) {
      expect(allowed.has(name), `${name} is not an allowed variable`).toBe(
        true,
      );
      expect(css, name).toMatch(new RegExp(`^\\s*${name}\\s*:`, "m"));
    }
    expect(source).not.toMatch(
      /(?<![\w-])(?:bg|text|border|ring|divide|fill|stroke)-(?:card|muted|muted-foreground|foreground|background|popover|primary|secondary|accent|destructive|border|input)(?![\w-])/,
    );
  });

  it("F8: in every state, every variable a rendered class uses is one of the allowed ones", async () => {
    const allowed = new Set([
      "--bz-card",
      "--bz-card-hover",
      "--bz-text-1",
      "--bz-text-2",
      "--bz-border",
      "--state-success",
      "--state-info",
      "--state-danger",
    ]);
    const varsIn = (root: HTMLElement) => {
      const classes = Array.from(root.querySelectorAll("[class]"))
        .map((e) => e.getAttribute("class") ?? "")
        .join(" ");
      return Array.from(classes.matchAll(/var\((--[\w-]+)\)/g)).map(
        (m) => m[1],
      );
    };
    const check = (root: HTMLElement) => {
      const used = varsIn(root);
      expect(used.length).toBeGreaterThan(0);
      for (const name of used) {
        expect(allowed.has(name), `${name} is not allowed`).toBe(true);
      }
    };
    // failed initial load, expanded: the retry control
    api.getDoneToday.mockRejectedValueOnce(
      Object.assign(new Error("boom"), { statusCode: 500 }),
    );
    const failed = render(<DoneTodaySection version={0} />);
    open();
    await screen.findByRole("button", { name: "Try again" });
    check(failed.container);
    failed.unmount();
    // loading, expanded
    api.getDoneToday.mockReturnValueOnce(new Promise(() => {}));
    const loading = render(<DoneTodaySection version={0} />);
    open();
    check(loading.container);
    loading.unmount();
    // loaded with every action and a truncation line
    api.getDoneToday.mockResolvedValueOnce(
      body(
        [
          item({ txn_id: 1, action: "confirmed" }),
          item({ txn_id: 2, action: "linked" }),
          item({ txn_id: 3, action: "ignored" }),
          item({ txn_id: 4, action: "weird" as never }),
        ],
        { truncated: true },
      ),
    );
    const done = render(<DoneTodaySection version={0} />);
    await loaded(4);
    open();
    check(done.container);
  });
});
