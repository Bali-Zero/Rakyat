import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { KBLISearch } from "./KBLISearch";

/**
 * A search that came back with zero codes and a search that never came back
 * must not look the same (insight: zero-because-zero ≠ zero-because-failed).
 *
 *   guilt      fetch rejects -> "Couldn't load results" alert + Retry,
 *              and NO "0 KBLI codes found" count
 *   innocence  fetch resolves [] -> no alert, no Retry (empty render as before)
 *   retry      Retry re-runs the same search for the same query
 */

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const search = vi.fn();
vi.mock("@/lib/api/kbli.api", () => ({
  kbliApi: { search: (q: string) => search(q) },
  apiPmaStatusLabel: () => "Open",
  isApiPmaVerdictVerified: () => true,
}));
vi.mock("@/lib/analytics", () => ({ trackKBLISearch: vi.fn() }));
vi.mock("@/lib/logger", () => ({ logger: { error: vi.fn() } }));

function typeQuery(value: string) {
  fireEvent.change(screen.getByRole("combobox"), { target: { value } });
}

describe("KBLISearch — empty result vs failed fetch", () => {
  beforeEach(() => {
    search.mockReset();
  });

  it("guilt: a rejected fetch renders a retryable error, not a zero count", async () => {
    search.mockRejectedValue(new Error("HTTP 500"));
    render(<KBLISearch />);
    typeQuery("villa");

    const alert = await screen.findByRole("alert", undefined, {
      timeout: 2000,
    });
    expect(alert).toHaveTextContent("Couldn't load results");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(/KBLI codes found/)).not.toBeInTheDocument();
  });

  it("innocence: a successful empty response shows no error and no Retry", async () => {
    search.mockResolvedValue([]);
    render(<KBLISearch />);
    typeQuery("zzzz");

    await waitFor(() => expect(search).toHaveBeenCalledWith("zzzz"), {
      timeout: 2000,
    });
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveAttribute(
        "aria-expanded",
        "false",
      ),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("Couldn't load results")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
  });

  it("retry: re-runs the same search and shows the results once it recovers", async () => {
    search.mockRejectedValueOnce(new Error("HTTP 500")).mockResolvedValue([
      {
        code: "55130",
        title: "Villa",
        description: "Villa accommodation",
        risk_category: "MR",
      },
    ]);
    render(<KBLISearch />);
    typeQuery("villa");

    fireEvent.click(
      await screen.findByRole("button", { name: "Retry" }, { timeout: 2000 }),
    );

    expect(
      await screen.findByText("Villa", undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(search).toHaveBeenCalledTimes(2);
    expect(search).toHaveBeenNthCalledWith(2, "villa");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
