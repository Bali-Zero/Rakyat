import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrderTracker } from "./OrderTracker";
import type { OrderState, OrderView } from "./types";

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
const RESULT_ID = "result-fixture-0000000001";

function serve(state: OrderState, resultId: string = RESULT_ID) {
  const body: OrderView = {
    order_id: "order-again-1",
    result_id: resultId,
    order_state: state,
    price_idr: 850000,
    browser_observation: "browser_not_returned",
    practice: null,
  };
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
  } as Response);
}

describe("OrderTracker — Pay again", () => {
  beforeEach(() => fetchMock.mockReset());
  afterEach(cleanup);

  it.each(["expired", "failed"] as const)(
    "links %s to its own checkout page, encoded",
    async (state) => {
      serve(state);
      render(<OrderTracker orderId="order-again-1" />);
      const link = await screen.findByRole("link", { name: /pay again/i });
      expect(link.getAttribute("href")).toBe(
        `/visa/voa/checkout/${encodeURIComponent(RESULT_ID)}`,
      );
      expect(document.body.textContent).toMatch(
        /verify your payment status before you try again/i,
      );
      expect(
        screen.getByRole("link", { name: /continue on whatsapp/i }),
      ).toBeInTheDocument();
    },
  );

  it.each(["created", "awaiting_payment", "paid", "refunded"] as const)(
    "offers no Pay again link on %s",
    async (state) => {
      serve(state);
      render(<OrderTracker orderId="order-again-1" />);
      await screen.findByText(/Rp/);
      expect(screen.queryByRole("link", { name: /pay again/i })).toBeNull();
    },
  );

  it.each(["short", "has space and/slash-0000000", "x".repeat(129), ""])(
    "renders no link when result_id %j fails the result-id pattern",
    async (bad) => {
      serve("expired", bad);
      render(<OrderTracker orderId="order-again-1" />);
      await screen.findByText(/Rp/);
      expect(screen.queryByRole("link", { name: /pay again/i })).toBeNull();
      expect(
        screen.getByRole("link", { name: /continue on whatsapp/i }),
      ).toBeInTheDocument();
    },
  );
});
