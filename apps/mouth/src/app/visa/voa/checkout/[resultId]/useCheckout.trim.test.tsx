import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCheckout } from "./useCheckout";

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const ORDER = {
  order_id: "order-1",
  order_state: "awaiting_payment",
  price_idr: 850000,
  checkout_url: "https://checkout.xendit.co/web/abc",
};

const CLEAN = {
  full_name: "Person One",
  email: "person.one@example.test",
  phone: "+620000000001",
  passport_number: "X0000001",
};

function sent(call: number) {
  const [, init] = fetchMock.mock.calls[call] as [string, RequestInit];
  const headers = new Headers(init.headers as HeadersInit);
  return {
    applicant: JSON.parse(init.body as string).applicant,
    key: headers.get("Idempotency-Key"),
  };
}

describe("useCheckout — the applicant is sent trimmed", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(jsonResponse(201, ORDER));
  });

  it("sends the four fields without surrounding spaces", async () => {
    const { result } = renderHook(() => useCheckout("result-1"));
    await act(async () => {
      await result.current.submit({
        full_name: "  Person One ",
        email: " person.one@example.test\t",
        phone: " +620000000001 ",
        passport_number: "\nX0000001  ",
      });
    });

    expect(sent(0).applicant).toEqual(CLEAN);
  });

  it("leaves case and inner spaces alone", async () => {
    const { result } = renderHook(() => useCheckout("result-1"));
    await act(async () => {
      await result.current.submit({
        ...CLEAN,
        full_name: " Person  One ",
        email: " Person.One@Example.test ",
      });
    });

    expect(sent(0).applicant.full_name).toBe("Person  One");
    expect(sent(0).applicant.email).toBe("Person.One@Example.test");
  });

  it("two submits that differ only by surrounding spaces reuse the idempotency key", async () => {
    const { result } = renderHook(() => useCheckout("result-1"));
    await act(async () => {
      await result.current.submit(CLEAN);
    });
    await act(async () => {
      await result.current.submit({ ...CLEAN, email: `  ${CLEAN.email} ` });
    });

    expect(sent(1).key).toBe(sent(0).key);
  });

  it("a different e-mail gets a new idempotency key", async () => {
    const { result } = renderHook(() => useCheckout("result-1"));
    await act(async () => {
      await result.current.submit(CLEAN);
    });
    await act(async () => {
      await result.current.submit({
        ...CLEAN,
        email: "person.two@example.test",
      });
    });

    expect(sent(1).key).not.toBe(sent(0).key);
  });
});
