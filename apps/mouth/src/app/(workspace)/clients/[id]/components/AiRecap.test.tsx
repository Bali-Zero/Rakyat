import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AiRecap } from "./AiRecap";

const { getClientRecap } = vi.hoisted(() => ({ getClientRecap: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: { crm: { getClientRecap } } }));

afterEach(() => vi.clearAllMocks());

const ok = (over: Record<string, unknown> = {}) => ({
  scope: "immigration",
  text: "Synthetic summary line one. Line two.",
  generated_at: "2026-11-03T09:00:00Z",
  model: "synthetic-model",
  stale: false,
  ...over,
});

describe("AiRecap", () => {
  it("renders the text, the AI summary label and 'Updated <d MMM>'", async () => {
    getClientRecap.mockResolvedValueOnce(ok());
    render(<AiRecap clientId={7} scope="immigration" />);
    expect(
      await screen.findByText("Synthetic summary line one. Line two."),
    ).toBeInTheDocument();
    expect(screen.getByText(/AI summary/)).toBeInTheDocument();
    expect(screen.getByText(/Updated 3 Nov/)).toBeInTheDocument();
  });

  it("shows a skeleton while the request is pending, then replaces it", async () => {
    let resolve!: (v: unknown) => void;
    getClientRecap.mockReturnValueOnce(new Promise((r) => (resolve = r)));
    render(<AiRecap clientId={7} scope="immigration" />);
    expect(screen.getByTestId("ai-recap-skeleton")).toBeInTheDocument();
    resolve(ok());
    await screen.findByText(/Synthetic summary/);
    expect(screen.queryByTestId("ai-recap-skeleton")).not.toBeInTheDocument();
  });

  it("GUILT: renders nothing for null text, empty text, a 404 and any error", async () => {
    for (const result of [
      () => Promise.resolve(ok({ text: null })),
      () => Promise.resolve(ok({ text: "   " })),
      () =>
        Promise.reject(
          Object.assign(new Error("Not Found"), { statusCode: 404 }),
        ),
      () => Promise.reject(new Error("boom")),
    ]) {
      getClientRecap.mockImplementationOnce(result);
      const { container, unmount } = render(
        <AiRecap clientId={7} scope="company" companyId={3} />,
      );
      await waitFor(() =>
        expect(
          screen.queryByTestId("ai-recap-skeleton"),
        ).not.toBeInTheDocument(),
      );
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });

  it("passes the client id, scope and company_id through; fetches once per mount (no polling)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getClientRecap.mockResolvedValue(ok({ scope: "company" }));
    render(<AiRecap clientId={7} scope="company" companyId={3} />);
    await screen.findByText(/Synthetic summary/);
    expect(getClientRecap).toHaveBeenCalledWith(7, "company", 3);
    vi.advanceTimersByTime(10 * 60_000);
    expect(getClientRecap).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
    getClientRecap.mockClear();
    render(<AiRecap clientId={9} scope="immigration" />);
    await waitFor(() => expect(getClientRecap).toHaveBeenCalledTimes(1));
    expect(getClientRecap).toHaveBeenCalledWith(9, "immigration", undefined);
  });
});

describe("AiRecap — full text, clamped only past six lines", () => {
  afterEach(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)
      .scrollHeight;
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)
      .clientHeight;
  });

  it("shows the whole text with no 3-line clamp and no toggle when it fits", async () => {
    getClientRecap.mockResolvedValueOnce(ok());
    render(<AiRecap clientId={7} scope="immigration" />);
    const p = await screen.findByText(/Synthetic summary/);
    expect(p.className).not.toContain("line-clamp-3");
    expect(screen.queryByRole("button", { name: /show more/i })).toBeNull();
  });

  it("a text taller than six lines is clamped at six with a Show more toggle that expands and collapses", async () => {
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => 300,
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get: () => 120,
    });
    getClientRecap.mockResolvedValueOnce(ok());
    render(<AiRecap clientId={7} scope="immigration" />);
    const p = await screen.findByText(/Synthetic summary/);
    expect(p.className).toContain("line-clamp-6");
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(p.className).not.toContain("line-clamp");
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(p.className).toContain("line-clamp-6");
  });
});
