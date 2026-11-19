/**
 * A figure whose request failed reads "Data not available", never 0: Rp 0 is a valid figure.
 * All values invented.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
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
  by_month: [{ period: "2026-01", revenue: v, paid: v, target: 0 }],
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
const revenueSection = () =>
  screen
    .getByRole("heading", { name: /Revenue Analysis/ })
    .closest("section") as HTMLElement;

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
    ]) {
      expect(card(title)).toHaveTextContent(/^.*Data not available$/);
      expect(card(title).textContent).not.toMatch(/Rp/);
    }
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
    for (const title of ["New This Week", "Active Clients", "Conversion Rate"])
      expect(card(title)).toHaveTextContent("Data not available");
    expect(card("Total Revenue")).toHaveTextContent(idr(2415000));
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
});
