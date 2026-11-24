import { beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  getBaseUrl: vi.fn(() => "https://api.synthetic.test"),
  post: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

import {
  approveCommission,
  clawback,
  exportFinanceCsv,
  markPaid,
  waive,
} from "./partners";

beforeEach(() => vi.clearAllMocks());

describe("partners client", () => {
  it("matches the commission mutation response contracts", async () => {
    apiMock.post
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce({ id: "synthetic-clawback-id" })
      .mockResolvedValueOnce(undefined);

    expectTypeOf<ReturnType<typeof approveCommission>>().toEqualTypeOf<
      Promise<void>
    >();
    expectTypeOf<ReturnType<typeof markPaid>>().toEqualTypeOf<Promise<void>>();
    expectTypeOf<ReturnType<typeof clawback>>().toEqualTypeOf<
      Promise<{ id: string }>
    >();
    expectTypeOf<ReturnType<typeof waive>>().toEqualTypeOf<Promise<void>>();

    await expect(
      approveCommission("synthetic-commission"),
    ).resolves.toBeUndefined();
    await expect(
      markPaid("synthetic-commission", {
        paid_via: "synthetic-bank-transfer",
        payment_reference: "synthetic-payment-reference",
      }),
    ).resolves.toBeUndefined();
    await expect(
      clawback("synthetic-commission", {
        reason: "synthetic reason",
      }),
    ).resolves.toEqual({ id: "synthetic-clawback-id" });
    await expect(
      waive("synthetic-commission", { reason: "synthetic reason" }),
    ).resolves.toBeUndefined();
  });

  it("exports every finance status within the requested date range", () => {
    expect(exportFinanceCsv("2026-01-01", "2026-01-31")).toBe(
      "https://api.synthetic.test/api/partners/finance/export?from=2026-01-01&to=2026-01-31",
    );

    // @ts-expect-error Finance export requires both date bounds.
    exportFinanceCsv();
  });
});
