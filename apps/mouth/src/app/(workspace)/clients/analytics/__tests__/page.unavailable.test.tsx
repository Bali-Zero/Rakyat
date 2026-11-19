/**
 * A figure whose request failed reads "Data not available", never 0: Rp 0 is a valid figure.
 * All values invented.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { formatIDR } from "@balizero/core/utils";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
const get = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/lib/logger");
vi.mock("@/lib/api", () => ({ api: { get } }));

import ClientAnalyticsPage from "../page";

// The amount as the page writes it, whitespace collapsed as toHaveTextContent collapses it.
const idr = (v: number) => formatIDR(v).replace(/\s+/g, " ");

const OVERVIEW = {
  total_clients: 7,
  new_this_month: 2,
  new_this_week: 1,
  by_status: { lead: 3, active: 4, completed: 2 },
  by_nationality: { Contoh: 5 },
};
const REVENUE = (v: number) => ({
  total_quoted: v,
  total_actual: v,
  total_paid: v,
  total_outstanding: 0,
  avg_per_client: v,
  by_month: [
    {
      period: "2026-01",
      status: "complete",
      collected: v,
      payments: v ? 1 : 0,
      to_reconcile: 0,
    },
  ],
  forecast_next_month: 0,
});

type Answers = Partial<
  Record<"overview" | "team" | "revenue" | "processes" | "trend", unknown>
>;

function answer(ok: Answers, failing: string[]) {
  get.mockImplementation((url: string) => {
    const key = url.includes("/clients/overview")
      ? "overview"
      : url.includes("/team/performance")
        ? "team"
        : url.includes("/revenue/summary")
          ? "revenue"
          : url.includes("/processes/by-type")
            ? "processes"
            : "trend";
    if (failing.includes(key)) return Promise.reject(new Error(`${key} down`));
    return Promise.resolve(ok[key as keyof Answers] ?? []);
  });
}

const card = (title: string) =>
  screen.getByText(title, { selector: "p" }).parentElement as HTMLElement;
// A failed card holds its title and "Data not available", nothing else: no subtitle beside it.
const expectUnavailable = (title: string) =>
  expect(card(title).textContent).toBe(`${title}Data not available`);
const section = (name: RegExp) =>
  screen.getByRole("heading", { name }).closest("section") as HTMLElement;
const revenueSection = () => section(/Revenue Analysis/);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("a failed request is not a zero", () => {
  it("revenue down: the four revenue figures and the monthly table read Data not available (guilt for Rp 0)", async () => {
    answer({ overview: OVERVIEW }, ["revenue"]);
    render(<ClientAnalyticsPage />);
    await waitFor(() =>
      expect(card("Total Revenue")).toHaveTextContent("Data not available"),
    );
    for (const title of [
      "Total Revenue",
      "Paid",
      "Outstanding",
      "Avg per Client",
    ])
      expectUnavailable(title);
    const section = revenueSection();
    expect(within(section).getByRole("status")).toHaveTextContent(
      "Data not available: this part could not be loaded.",
    );
    expect(within(section).queryByRole("table")).toBeNull();
    // the other requests still show their figures
    expect(card("Total Clients")).toHaveTextContent("7");
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it("revenue answered with zeros: the figures read Rp 0, which is a real value (innocence)", async () => {
    answer({ overview: OVERVIEW, revenue: REVENUE(0) }, []);
    render(<ClientAnalyticsPage />);
    await waitFor(() =>
      expect(card("Total Revenue")).toHaveTextContent(idr(0)),
    );
    expect(screen.queryByText(/Data not available/)).toBeNull();
    expect(within(revenueSection()).getByRole("table")).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("revenue answered with an amount shows it (innocence)", async () => {
    answer({ overview: OVERVIEW, revenue: REVENUE(2415000) }, []);
    render(<ClientAnalyticsPage />);
    await waitFor(() =>
      expect(card("Total Revenue")).toHaveTextContent(idr(2415000)),
    );
  });

  it("overview down: client counts read Data not available, revenue still shows", async () => {
    answer({ revenue: REVENUE(2415000) }, ["overview"]);
    render(<ClientAnalyticsPage />);
    await waitFor(() =>
      expect(card("Total Clients")).toHaveTextContent("Data not available"),
    );
    for (const title of [
      "Total Clients",
      "New This Week",
      "Active Clients",
      "Conversion Rate",
    ])
      expectUnavailable(title);
    expect(card("Total Revenue")).toHaveTextContent(idr(2415000));
    expect(card("Total Revenue")).toHaveTextContent("Actual collected");
  });

  it("refresh after a failure: a recovered request shows its figures, a new failure marks only itself", async () => {
    answer({ overview: OVERVIEW }, ["revenue"]);
    render(<ClientAnalyticsPage />);
    await waitFor(() => expectUnavailable("Total Revenue"));

    answer({ overview: OVERVIEW, revenue: REVENUE(0) }, ["team"]);
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    await waitFor(() =>
      expect(card("Total Revenue")).toHaveTextContent(idr(0)),
    );
    expect(within(revenueSection()).queryByRole("status")).toBeNull();
    expect(
      within(section(/Team Performance/)).getByRole("status"),
    ).toHaveTextContent("Data not available: this part could not be loaded.");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(card("Total Clients")).toHaveTextContent("7");
  });

  it("every request down: no figure on the page reads 0 or Rp", async () => {
    answer({}, ["overview", "team", "revenue", "processes", "trend"]);
    render(<ClientAnalyticsPage />);
    await waitFor(() =>
      expect(card("Total Revenue")).toHaveTextContent("Data not available"),
    );
    expect(screen.getAllByRole("status")).toHaveLength(6);
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/Rp/);
    expect(text).not.toMatch(/\b0%/);
  });

  it("the monthly table: no statement and transfers still to reconcile never read as a known Rp 0", async () => {
    const month = (
      period: string,
      status: string,
      collected: number | null,
      payments: number | null,
      to_reconcile: number,
    ) => ({ period, status, collected, payments, to_reconcile });
    answer(
      {
        overview: OVERVIEW,
        revenue: {
          ...REVENUE(2415000),
          by_month: [
            month("2026-07", "no_statement", null, null, 0),
            month("2026-08", "partial", 0, 0, 3),
            month("2026-09", "partial", 700000, 1, 2),
            month("2026-10", "complete", 0, 0, 0),
            month("2026-11", "complete", 2415000, 1, 0),
          ],
        },
      },
      [],
    );
    render(<ClientAnalyticsPage />);
    const table = await within(revenueSection()).findByRole("table");
    const row = (period: string) =>
      within(table).getByText(period).closest("tr") as HTMLElement;
    const cells = (period: string) =>
      Array.from(row(period).querySelectorAll("td")).map((td) =>
        (td.textContent ?? "").replace(/\s+/g, " "),
      );
    expect(
      within(revenueSection()).getByText(/Money received, by transfer date/),
    ).toBeInTheDocument();
    expect(cells("2026-07")).toEqual(["2026-07", "No bank statement"]);
    expect(cells("2026-08")).toEqual(["2026-08", "Not reconciled", "0", "3"]);
    expect(cells("2026-09")).toEqual([
      "2026-09",
      `${idr(700000)}(partial)`,
      "1",
      "2",
    ]);
    expect(cells("2026-10")).toEqual(["2026-10", idr(0), "0", "0"]);
    expect(cells("2026-11")).toEqual(["2026-11", idr(2415000), "1", "0"]);
  });
});
