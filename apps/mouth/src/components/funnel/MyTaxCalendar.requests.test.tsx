import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MyTaxCalendar } from "./MyTaxCalendar";

function calendarFor(id: string) {
  return {
    obligations: [
      {
        authority: "Synthetic Authority",
        frequency: "monthly",
        id,
        name: `Synthetic obligation ${id}`,
        reviewed_on: "2026-09-20",
        upcoming_due_dates: [{ due_date: "2026-10-01", period_key: "2026-09" }],
      },
    ],
    withheld_count: 0,
  };
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

function deferred() {
  let resolve!: (value: Response) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<Response>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

function chooseCompanyProfile() {
  fireEvent.click(screen.getByLabelText("Company"));
  fireEvent.click(screen.getByLabelText("PT PMA"));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(
    screen.getByLabelText("No", { selector: 'input[name="employees"]' }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(
    screen.getByLabelText("No", { selector: 'input[name="pkp"]' }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(
    screen.getByLabelText("No", { selector: 'input[name="online"]' }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByLabelText("Construction or pre-operational"));
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
}

describe("MyTaxCalendar request lifecycle", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("shows the latest profile when an earlier request answers last", async () => {
    const first = deferred();
    const second = deferred();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    vi.stubGlobal("fetch", fetchMock);
    render(<MyTaxCalendar />);

    fireEvent.click(screen.getByLabelText("Individual"));
    fireEvent.click(screen.getByRole("button", { name: "Start over" }));
    chooseCompanyProfile();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve(jsonResponse(calendarFor("company")));
    });
    await screen.findByText("Synthetic obligation company");
    await act(async () => {
      first.resolve(jsonResponse(calendarFor("individual")));
    });

    expect(screen.getByText("Synthetic obligation company")).toBeTruthy();
    expect(screen.queryByText("Synthetic obligation individual")).toBeNull();
  });

  it("ignores an earlier answer that arrives while the latest is loading", async () => {
    const first = deferred();
    const second = deferred();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    vi.stubGlobal("fetch", fetchMock);
    render(<MyTaxCalendar />);

    fireEvent.click(screen.getByLabelText("Individual"));
    fireEvent.click(screen.getByRole("button", { name: "Start over" }));
    chooseCompanyProfile();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      first.resolve(jsonResponse(calendarFor("individual")));
    });

    expect(screen.queryByText("Synthetic obligation individual")).toBeNull();
    expect(screen.getByText("Loading your calendar…")).toBeTruthy();
  });

  it("aborts the superseded request and shows no error for it", async () => {
    const first = deferred();
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn((_url: string, init: RequestInit) => {
      signals.push(init.signal as AbortSignal);
      return first.promise;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MyTaxCalendar />);

    fireEvent.click(screen.getByLabelText("Individual"));
    fireEvent.click(screen.getByRole("button", { name: "Start over" }));
    expect(signals[0].aborted).toBe(true);

    await act(async () => {
      first.reject(new DOMException("aborted", "AbortError"));
    });

    expect(screen.queryByText(/could not load your calendar/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("reaches the retry state when the backend never answers", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    render(<MyTaxCalendar />);

    fireEvent.click(screen.getByLabelText("Individual"));
    expect(screen.getByText("Loading your calendar…")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(screen.getByText(/could not load your calendar/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("renders a single normal request and stops the timer", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(calendarFor("individual"))),
    );
    render(<MyTaxCalendar />);

    fireEvent.click(screen.getByLabelText("Individual"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Synthetic obligation individual")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByText("Synthetic obligation individual")).toBeTruthy();
    expect(screen.queryByText(/could not load your calendar/)).toBeNull();
  });
});
