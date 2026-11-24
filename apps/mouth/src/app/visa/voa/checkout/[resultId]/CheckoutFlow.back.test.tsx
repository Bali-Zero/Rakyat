import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CheckoutFlow } from "./CheckoutFlow";
import { writeCheckoutHandoff } from "../../checkoutHandoff";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}));

vi.mock("@balizero/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@balizero/core")>();
  return {
    ...actual,
    useFunnelApp: () => ({
      viewed: vi.fn(),
      formSubmitted: vi.fn(),
      formSubmitFailed: vi.fn(),
      ctaClicked: vi.fn(),
      whatsappHandoff: vi.fn(),
    }),
  };
});

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function pageshow(persisted: boolean) {
  const event = new Event("pageshow");
  Object.defineProperty(event, "persisted", { value: persisted });
  act(() => {
    window.dispatchEvent(event);
  });
}

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/email/i), "person.one@example.test");
  await user.type(screen.getByLabelText(/phone/i), "+620000000001");
  await user.click(
    screen.getByRole("button", { name: /continue to payment/i }),
  );
}

describe("CheckoutFlow — coming back from the payment provider", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    window.sessionStorage.clear();
    mocks.push.mockReset();
    mocks.replace.mockReset();
    // @ts-expect-error -- test-only stub
    delete window.location;
    // @ts-expect-error -- test-only stub
    window.location = { href: "" };
    writeCheckoutHandoff("result-1", {
      full_name: "Person One",
      passport_number: "X0000001",
    });
  });

  async function handOffToProvider() {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        order_id: "order-1",
        order_state: "awaiting_payment",
        price_idr: 850000,
        checkout_url: "https://checkout.xendit.co/web/abc",
      }),
    );
    const user = userEvent.setup();
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);
    await screen.findByText("Person One");
    await fillAndSubmit(user);
    await waitFor(() =>
      expect(window.location.href).toBe("https://checkout.xendit.co/web/abc"),
    );
  }

  it("a page restored from the back-forward cache after the hand-off goes to the order's tracker", async () => {
    await handOffToProvider();
    expect(mocks.replace).not.toHaveBeenCalled();

    pageshow(true);

    expect(mocks.replace).toHaveBeenCalledTimes(1);
    expect(mocks.replace).toHaveBeenCalledWith("/visa/voa/orders/order-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe("https://checkout.xendit.co/web/abc");
  });

  it("a pageshow that is not a cache restore changes nothing", async () => {
    await handOffToProvider();

    pageshow(false);

    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("a cache restore while the form is still idle changes nothing", () => {
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);

    pageshow(true);

    expect(mocks.replace).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stops listening once the page is unmounted", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(201, {
        order_id: "order-1",
        order_state: "awaiting_payment",
        price_idr: 850000,
        checkout_url: "https://checkout.xendit.co/web/abc",
      }),
    );
    const user = userEvent.setup();
    const { unmount } = render(
      <CheckoutFlow resultId="result-1" paymentsLive={true} />,
    );
    await screen.findByText("Person One");
    await fillAndSubmit(user);
    await waitFor(() =>
      expect(window.location.href).toBe("https://checkout.xendit.co/web/abc"),
    );
    unmount();

    pageshow(true);

    expect(mocks.replace).not.toHaveBeenCalled();
  });
});
