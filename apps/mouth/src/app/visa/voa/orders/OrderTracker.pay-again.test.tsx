import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrderTracker } from "./OrderTracker";
import type { OrderState, OrderView } from "./types";

const searchParams = vi.hoisted(() => ({ current: new URLSearchParams() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/visa/voa/orders/order-again-1",
  useSearchParams: () => searchParams.current,
}));

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
  beforeEach(() => {
    fetchMock.mockReset();
    searchParams.current = new URLSearchParams();
  });
  afterEach(cleanup);

  const LOCALES = {
    en: {
      query: "",
      cta: /pay again/i,
      heading: {
        failed: "This checkout couldn't be completed.",
        expired: "This checkout session expired.",
      },
      safety:
        "If money has already left your account, contact us first: a consultant can verify your payment status before you try again. Otherwise you can start a new checkout from the same check.",
    },
    id: {
      query: "lang=id",
      cta: /bayar lagi/i,
      heading: {
        failed: "Pembayaran ini tidak dapat diselesaikan.",
        expired: "Sesi pembayaran ini telah kedaluwarsa.",
      },
      safety:
        "Jika uang sudah keluar dari rekening Anda, hubungi kami terlebih dahulu: konsultan dapat memeriksa status pembayaran Anda sebelum Anda mencoba lagi. Jika belum, Anda dapat memulai pembayaran baru dari pengecekan yang sama.",
    },
  } as const;

  describe.each(["en", "id"] as const)("in %s", (loc) => {
    it.each(["expired", "failed"] as const)(
      "%s: heading, FULL safety instruction and CTA in the journey's language",
      async (state) => {
        const L = LOCALES[loc];
        searchParams.current = new URLSearchParams(L.query);
        serve(state);
        render(<OrderTracker orderId="order-again-1" />);
        const link = await screen.findByRole("link", { name: L.cta });
        expect(link.getAttribute("href")).toBe(
          `/visa/voa/checkout/${encodeURIComponent(RESULT_ID)}`,
        );
        expect(screen.getByText(L.heading[state])).toBeInTheDocument();
        // The whole instruction, one paragraph: money-left AND contact-first
        // AND the way forward, never only the tail.
        expect(screen.getByText(L.safety)).toBeInTheDocument();
        expect(
          screen.getByRole("link", { name: /continue on whatsapp/i }),
        ).toBeInTheDocument();
      },
    );
  });

  it("does not leak the other language's CTA", async () => {
    searchParams.current = new URLSearchParams("lang=id");
    serve("failed");
    render(<OrderTracker orderId="order-again-1" />);
    await screen.findByRole("link", { name: /bayar lagi/i });
    expect(screen.queryByRole("link", { name: /pay again/i })).toBeNull();
  });

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
