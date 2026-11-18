import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrderTracker } from "./OrderTracker";
import {
  checkoutNavigation,
  isProviderCheckoutUrl,
} from "./provider-checkout-url";
import type { OrderState, OrderView } from "./types";

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
const XENDIT_URL = "https://checkout.xendit.co/web/inv-fake-0003";

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function order(state: OrderState): OrderView {
  return {
    order_id: "order-pay-1",
    result_id: "result-fixture-0000000001",
    order_state: state,
    price_idr: 850000,
    browser_observation: "browser_not_returned",
    practice: null,
  };
}

function serve(state: OrderState, resume: () => Response) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (String(url).endsWith("/checkout-session") && init?.method === "POST") {
      return resume();
    }
    return jsonResponse(200, order(state));
  });
}

function resumeCalls() {
  return fetchMock.mock.calls.filter(([url]) =>
    String(url).endsWith("/checkout-session"),
  );
}

describe("OrderTracker — Pay now", () => {
  let assign: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchMock.mockReset();
    assign = vi
      .spyOn(checkoutNavigation, "assign")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    assign.mockRestore();
  });

  it("hands off to the provider checkout re-read for this order", async () => {
    serve("awaiting_payment", () =>
      jsonResponse(200, { checkout_url: XENDIT_URL }),
    );
    render(<OrderTracker orderId="order-pay-1" />);

    fireEvent.click(await screen.findByRole("button", { name: /pay now/i }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith(XENDIT_URL));
    const [[url, init]] = resumeCalls();
    expect(url).toBe("/api/visa/voa/orders/order-pay-1/checkout-session");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
  });

  it.each(["created", "paid", "failed", "expired", "refunded"] as OrderState[])(
    "renders no Pay now button for a %s order",
    async (state) => {
      serve(state, () => jsonResponse(200, { checkout_url: XENDIT_URL }));
      render(<OrderTracker orderId="order-pay-1" />);
      await screen.findByText(/Rp/);
      expect(screen.queryByRole("button", { name: /pay now/i })).toBeNull();
      expect(resumeCalls()).toHaveLength(0);
    },
  );

  it.each([
    "http://checkout.xendit.co/web/inv-1",
    "https://checkout.xendit.co.example.net/web/inv-1",
    "https://checkout.xendit.co@example.net/web/inv-1",
    "javascript:alert(1)",
  ])("never follows %s", async (badUrl) => {
    serve("awaiting_payment", () =>
      jsonResponse(200, { checkout_url: badUrl }),
    );
    render(<OrderTracker orderId="order-pay-1" />);

    fireEvent.click(await screen.findByRole("button", { name: /pay now/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /couldn't open the payment page/i,
    );
    expect(assign).not.toHaveBeenCalled();
  });

  it("explains a closed session and keeps the consultant link", async () => {
    serve("awaiting_payment", () =>
      jsonResponse(409, {
        code: "CHECKOUT_NOT_RESUMABLE",
        retryable: false,
        message_key: "garuda_voa.error.checkout_not_resumable",
      }),
    );
    render(<OrderTracker orderId="order-pay-1" />);

    fireEvent.click(await screen.findByRole("button", { name: /pay now/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /no longer available/i,
    );
    expect(assign).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", {
        name: /consultant can help you complete it/i,
      }),
    ).toBeTruthy();
  });

  it("sends one request per click while a request is in flight", async () => {
    let release: (value: Response) => void = () => {};
    serve("awaiting_payment", () =>
      jsonResponse(200, { checkout_url: XENDIT_URL }),
    );
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (
        String(url).endsWith("/checkout-session") &&
        init?.method === "POST"
      ) {
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      return jsonResponse(200, order("awaiting_payment"));
    });
    render(<OrderTracker orderId="order-pay-1" />);

    const button = await screen.findByRole("button", { name: /pay now/i });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(resumeCalls()).toHaveLength(1));
    release(jsonResponse(200, { checkout_url: XENDIT_URL }));
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
  });
});

describe("isProviderCheckoutUrl", () => {
  it.each([
    ["https://checkout.xendit.co/web/inv-1", true],
    ["https://checkout-staging.xendit.co/web/inv-1", true],
    ["http://checkout.xendit.co/web/inv-1", false],
    ["https://checkout.xendit.co:8443/web/inv-1", false],
    ["https://user:pw@checkout.xendit.co/web/inv-1", false], // pragma: allowlist secret
    ["https://evil.example/checkout.xendit.co", false],
    ["https://app.midtrans.com/snap/v4/redirection/tok", false],
    ["", false],
    [null, false],
    ["not a url", false],
  ])("%s -> %s", (url, expected) => {
    expect(isProviderCheckoutUrl(url)).toBe(expected);
  });
});
